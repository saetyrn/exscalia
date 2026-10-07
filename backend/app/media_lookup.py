"""Media metadata lookup and cover ingestion.

Structured, provider-agnostic sources rather than model-side web search:
Open Library for books (keyless), TMDB for movies/TV, IGDB -> RAWG -> Steam
(keyless floor) for games, and Wikimedia Commons for concept-driven book
backdrops. Every provider result is normalized to {title, creator, year,
cover_url, total, source} so the caller never re-parses prose.

API keys live in data/media.json — deliberately OUTSIDE the vault git repo,
exactly like ai.json.
"""
import asyncio
import hashlib
import io
import ipaddress
import json
import logging
import re
import socket
import time
from pathlib import Path
from typing import Any

import httpx

from .api.attachments import _safe_stem
from .vault.internal import (
    atomic_secret_json,
    atomic_bytes,
    refuse_if_set_aside,
    refuse_if_unreadable,
    set_aside,
)
from .vault.notes import NoteError, NoteStore

log = logging.getLogger(__name__)

FILE_NAME = "media.json"
# Wikimedia requires automated clients to identify the project and provide a
# contact route; the same descriptive header is harmless for every provider.
USER_AGENT = "EXSCALIA-MediaBot/1.0 (https://notes.exscalia.art)"
LOOKUP_TIMEOUT = 10.0
MAX_CANDIDATES = 5
COVER_MAX_BYTES = 10 * 1024 * 1024
COVER_MAX_REDIRECTS = 3
COVER_TIMEOUT = 30.0
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
# Content-type decides the extension; the URL's suffix is never trusted.
COVER_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/avif": ".avif",
}
# Art is re-encoded on the way into the vault when it is bigger than it will
# ever be shown: the hero backdrop never needs more than ~2400 px, a cover
# ~1000 px. Commons originals run 4000-6000 px and git keeps every byte.
ART_MAX_EDGE = {"backdrop": 2400, "cover": 1000}
# GIF (animation) and AVIF are stored as they come.
SHRINKABLE_TYPES = {"image/jpeg", "image/png", "image/webp"}


# -- key storage (outside the vault) ------------------------------------------


def load_media_keys(data_dir: Path) -> dict:
    """Same policy as `load_ai_config`: a corrupt file is set aside as
    ``media.json.corrupt-<ts>`` and reads as {}, and saving refuses until the
    quarantine is dealt with; one that cannot be read stays put and reads as
    {}, and saving refuses until it can be."""
    p = data_dir / FILE_NAME
    if not p.is_file():
        return {}
    try:
        raw = p.read_bytes()
    except OSError as exc:
        log.error("%s cannot be read (%s); using no stored keys", p, exc)
        return {}
    try:
        keys = json.loads(raw.decode("utf-8"))
    except ValueError:
        keys = None
    if not isinstance(keys, dict):
        moved = set_aside(p)
        log.error("%s is unreadable; set aside as %s", p, moved.name)
        return {}
    return keys


def save_media_keys(data_dir: Path, keys: dict) -> None:
    p = data_dir / FILE_NAME
    refuse_if_set_aside(p)
    refuse_if_unreadable(p)
    atomic_secret_json(p, keys)  # the file holds API keys: 0600, never torn


# -- metadata search ----------------------------------------------------------


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=LOOKUP_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    )


def _safe_error(exc: BaseException) -> str:
    """Failure text that is safe to return or log: status and host only.

    httpx messages carry the full request URL, and TMDB/RAWG keys ride in its
    query string, so str(exc) must never leave this module.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        try:
            return f"HTTP {exc.response.status_code} from {exc.request.url.host}"
        except (AttributeError, RuntimeError):
            pass
    return type(exc).__name__


def _year(date_str: Any) -> int | None:
    text = str(date_str or "")
    return int(text[:4]) if text[:4].isdigit() else None


async def search_media(media_type: str, query: str, keys: dict) -> dict:
    """Never raises: every failure comes back as {candidates: [], error}."""
    query = (query or "").strip()
    if not query:
        return {"candidates": [], "error": "Empty query"}
    try:
        async with _client() as client:
            if media_type == "book":
                return {"candidates": await _openlibrary(client, query)}
            if media_type in ("movie", "tv"):
                key = (keys or {}).get("tmdb")
                if not key:
                    return {
                        "candidates": [],
                        "error": "TMDB API key not set — add it in Settings, Media lookup",
                    }
                return {"candidates": await _tmdb(client, media_type, query, key)}
            if media_type == "game":
                return await _game_search(client, query, keys or {})
            return {"candidates": [], "error": f"Unknown media type: {media_type}"}
    except Exception as exc:
        safe = _safe_error(exc)
        log.warning("media lookup failed for %s %r: %s", media_type, query, safe)
        return {"candidates": [], "error": f"Lookup failed: {safe}"}


async def _openlibrary(client: httpx.AsyncClient, query: str) -> list[dict]:
    resp = await client.get(
        "https://openlibrary.org/search.json",
        params={
            "q": query,
            "limit": MAX_CANDIDATES,
            "fields": "key,title,author_name,first_publish_year,cover_i,number_of_pages_median",
        },
    )
    resp.raise_for_status()
    docs = (resp.json() or {}).get("docs") or []
    candidates = []
    for doc in docs[:MAX_CANDIDATES]:
        authors = doc.get("author_name") or []
        cover_id = doc.get("cover_i")
        work = str(doc.get("key") or "").removeprefix("/works/")
        candidates.append(
            {
                "title": doc.get("title"),
                "creator": authors[0] if authors else None,
                "year": doc.get("first_publish_year"),
                "cover_url": (
                    f"https://covers.openlibrary.org/b/id/{cover_id}-L.jpg"
                    if cover_id
                    else None
                ),
                "total": doc.get("number_of_pages_median"),
                "source": f"openlibrary:{work}" if work else None,
            }
        )
    return candidates


async def _tmdb(
    client: httpx.AsyncClient, media_type: str, query: str, key: str
) -> list[dict]:
    kind = "movie" if media_type == "movie" else "tv"
    resp = await client.get(
        f"https://api.themoviedb.org/3/search/{kind}",
        params={"query": query, "api_key": key},
    )
    resp.raise_for_status()
    results = (resp.json() or {}).get("results") or []
    candidates = []
    for r in results[:MAX_CANDIDATES]:
        poster = r.get("poster_path")
        candidates.append(
            {
                "title": r.get("title") or r.get("name"),
                "creator": None,
                "year": _year(r.get("release_date") or r.get("first_air_date")),
                "cover_url": f"https://image.tmdb.org/t/p/w500{poster}" if poster else None,
                "total": None,
                "source": f"tmdb:{kind}:{r.get('id')}",
            }
        )
    # Best-effort director/creator (and episode count for TV) on the top hit
    # only — one extra call; the search result stands on its own if it fails.
    if candidates:
        try:
            top_id = results[0]["id"]
            if kind == "movie":
                detail = await client.get(
                    f"https://api.themoviedb.org/3/movie/{top_id}",
                    params={"api_key": key, "append_to_response": "credits"},
                )
                detail.raise_for_status()
                data = detail.json() or {}
                crew = ((data.get("credits") or {}).get("crew")) or []
                directors = [c.get("name") for c in crew if c.get("job") == "Director"]
                if directors:
                    candidates[0]["creator"] = directors[0]
                companies = data.get("production_companies") or []
                if companies:
                    # The Media job later chooses director vs studio attribution.
                    candidates[0]["studio"] = companies[0].get("name")
            else:
                detail = await client.get(
                    f"https://api.themoviedb.org/3/tv/{top_id}",
                    params={"api_key": key},
                )
                detail.raise_for_status()
                data = detail.json() or {}
                created_by = data.get("created_by") or []
                if created_by:
                    candidates[0]["creator"] = created_by[0].get("name")
                if data.get("number_of_episodes"):
                    candidates[0]["total"] = data["number_of_episodes"]
        except Exception as exc:
            log.debug("TMDB detail call failed; keeping bare search hit: %s", _safe_error(exc))
    return candidates


async def movie_attribution(source: str | None, keys: dict) -> dict:
    """Director + lead studio for a tmdb:movie source, for the Media job's
    attribution choice. Never raises; {} means no data."""
    parts = (source or "").split(":")
    key = (keys or {}).get("tmdb")
    if len(parts) != 3 or parts[0] != "tmdb" or parts[1] != "movie" or not key:
        return {}
    try:
        async with _client() as client:
            resp = await client.get(
                f"https://api.themoviedb.org/3/movie/{parts[2]}",
                params={"api_key": key, "append_to_response": "credits"},
            )
            resp.raise_for_status()
            data = resp.json() or {}
    except Exception as exc:
        log.warning("movie attribution lookup failed for %s: %s", source, _safe_error(exc))
        return {}
    crew = ((data.get("credits") or {}).get("crew")) or []
    directors = [c.get("name") for c in crew if c.get("job") == "Director"]
    companies = data.get("production_companies") or []
    out = {}
    if directors and directors[0]:
        out["director"] = str(directors[0])
    if companies and companies[0].get("name"):
        out["studio"] = str(companies[0]["name"])
    return out


async def _tmdb_images(
    client: httpx.AsyncClient,
    kind_path: str,
    tmdb_id: str,
    key: str,
    *,
    want: str = "backdrops",
    limit: int = 8,
) -> list[dict]:
    """TMDB stills or posters as gallery dicts, best-voted first."""
    resp = await client.get(
        f"https://api.themoviedb.org/3/{kind_path}/{tmdb_id}/images",
        # Textless backdrops read as cinema; localized ones carry baked-in
        # title art. Posters keep en for legible cover text.
        params={"api_key": key, "include_image_language": "null,en"},
    )
    resp.raise_for_status()
    entries = (resp.json() or {}).get(want) or []
    entries.sort(key=lambda e: e.get("vote_count") or 0, reverse=True)
    full = "w1280" if want == "backdrops" else "w780"
    thumb = "w500" if want == "backdrops" else "w342"
    return [
        {
            "url": f"https://image.tmdb.org/t/p/{full}{e['file_path']}",
            "thumb": f"https://image.tmdb.org/t/p/{thumb}{e['file_path']}",
            "width": e.get("width") or 0,
            "height": e.get("height") or 0,
            "origin": "tmdb",
        }
        for e in entries[:limit]
        if e.get("file_path")
    ]


async def backdrop_candidates(media_type: str, source: str | None, keys: dict) -> list[str]:
    """Wide, scene-like images for film, TV, and game heroes by provider id.

    Books use the separate concept-driven Commons search below. Never raises;
    no candidates is a normal answer.
    """
    parts = (source or "").split(":")
    try:
        async with _client() as client:
            if media_type in ("movie", "tv") and len(parts) == 3 and parts[0] == "tmdb":
                key = (keys or {}).get("tmdb")
                if not key:
                    return []
                stills = await _tmdb_images(
                    client, parts[1], parts[2], key, want="backdrops", limit=4
                )
                return [s["url"] for s in stills]
            if media_type == "game" and len(parts) == 2 and parts[0] == "rawg":
                key = (keys or {}).get("rawg")
                if not key:
                    return []
                resp = await client.get(
                    f"https://api.rawg.io/api/games/{parts[1]}/screenshots",
                    params={"key": key, "page_size": 4},
                )
                resp.raise_for_status()
                shots = (resp.json() or {}).get("results") or []
                return [s["image"] for s in shots[:4] if s.get("image")]
            if media_type == "game" and len(parts) == 2 and parts[0] == "igdb":
                if not ((keys or {}).get("igdb_id") and (keys or {}).get("igdb_secret")):
                    return []
                body = (
                    "fields artworks.image_id, screenshots.image_id; "
                    f"where id = {int(parts[1])};"
                )
                resp = await _igdb_post(client, keys, body)
                games = resp.json() or []
                urls: list[str] = []
                if games:
                    # Artworks are promo art — more cinematic than raw
                    # screenshots, so they go first.
                    for group in ("artworks", "screenshots"):
                        for entry in games[0].get(group) or []:
                            if entry.get("image_id"):
                                urls.append(f"{IGDB_IMG}/t_1080p/{entry['image_id']}.jpg")
                return urls[:4]
            if media_type == "game" and len(parts) == 2 and parts[0] == "steam":
                resp = await client.get(
                    "https://store.steampowered.com/api/appdetails",
                    params={"appids": parts[1], "cc": "us", "l": "en"},
                )
                resp.raise_for_status()
                data = ((resp.json() or {}).get(parts[1]) or {}).get("data") or {}
                shots = data.get("screenshots") or []
                return [s["path_full"] for s in shots[:4] if s.get("path_full")]
    except Exception as exc:
        log.warning("backdrop lookup failed for %s: %s", source, _safe_error(exc))
    return []


async def book_context(
    source: str | None, title: str = "", creator: str = ""
) -> dict:
    """Fetch the concise work-level brief OMNIA uses to understand a book.

    Open Library work records commonly carry a description and subject facets;
    an empty dict is a normal result for manually entered or sparse books.
    """
    parts = (source or "").split(":")
    work = parts[1] if len(parts) == 2 and parts[0] == "openlibrary" else ""
    try:
        async with _client() as client:
            if not work and title.strip():
                params = {
                    "title": title.strip(),
                    "limit": 1,
                    "fields": "key",
                }
                if creator.strip():
                    params["author"] = creator.strip()
                search = await client.get(
                    "https://openlibrary.org/search.json",
                    params=params,
                )
                search.raise_for_status()
                docs = (search.json() or {}).get("docs") or []
                if docs:
                    work = str(docs[0].get("key") or "").removeprefix("/works/")
            if not work:
                return {}
            resp = await client.get(f"https://openlibrary.org/works/{work}.json")
            resp.raise_for_status()
            data = resp.json() or {}
    except Exception:
        log.warning("book context lookup failed for %s", source, exc_info=True)
        return {}

    description = data.get("description")
    if isinstance(description, dict):
        description = description.get("value")
    if not isinstance(description, str):
        description = ""

    def names(key: str, limit: int) -> list[str]:
        values = data.get(key) or []
        return [str(value).strip() for value in values if str(value).strip()][:limit]

    return {
        "description": description.strip()[:1800],
        "subjects": names("subjects", 16),
        "people": names("subject_people", 8),
        "places": names("subject_places", 8),
        "times": names("subject_times", 6),
    }


async def _commons_images(
    client: httpx.AsyncClient, query: str, *, landscape_only: bool = True, limit: int = 12
) -> list[dict]:
    """Wikimedia Commons file search as gallery dicts. Keyless."""
    resp = await client.get(
        COMMONS_API,
        params={
            "action": "query",
            "generator": "search",
            "gsrsearch": query,
            "gsrnamespace": 6,
            "gsrlimit": max(12, limit),
            "prop": "imageinfo",
            "iiprop": "url|mime|size",
            "iiurlwidth": 640,
            "format": "json",
        },
    )
    resp.raise_for_status()
    pages = list(((resp.json() or {}).get("query") or {}).get("pages", {}).values())
    pages.sort(key=lambda page: page.get("index") or 10_000)
    images: list[dict] = []
    seen: set[str] = set()
    for page in pages:
        infos = page.get("imageinfo") or []
        if not infos:
            continue
        info = infos[0]
        width = int(info.get("width") or 0)
        height = int(info.get("height") or 0)
        size = int(info.get("size") or 0)
        if (
            info.get("mime") not in {"image/jpeg", "image/png", "image/webp"}
            or size > COVER_MAX_BYTES
        ):
            continue
        if landscape_only:
            if width < 1200 or height < 600 or width / max(height, 1) < 1.3:
                continue
        elif width < 400 or height < 400:
            continue
        # Wikimedia's thumbnail CDN can return 429 to server-side clients even
        # when the canonical original is available — the FULL url is what gets
        # ingested server-side; the thumb is only ever fetched by the browser.
        url = info.get("url")
        if isinstance(url, str):
            url = url.split("?", 1)[0]
        if not (isinstance(url, str) and url.startswith("https://")) or url in seen:
            continue
        seen.add(url)
        thumb = info.get("thumburl")
        images.append(
            {
                "url": url,
                "thumb": thumb if isinstance(thumb, str) and thumb.startswith("https://") else url,
                "width": width,
                "height": height,
                "origin": "commons",
            }
        )
        if len(images) == limit:
            break
    return images


async def _brave_images(
    client: httpx.AsyncClient, query: str, key: str, *, limit: int = 8
) -> list[dict]:
    """Brave image search — the broad web tier; needs the free API key."""
    resp = await client.get(
        "https://api.search.brave.com/res/v1/images/search",
        params={"q": query, "count": min(20, max(limit * 2, 10)), "safesearch": "strict"},
        headers={"X-Subscription-Token": key, "Accept": "application/json"},
    )
    resp.raise_for_status()
    images: list[dict] = []
    seen: set[str] = set()
    for result in (resp.json() or {}).get("results") or []:
        props = result.get("properties") or {}
        url = props.get("url")
        if not (isinstance(url, str) and url.startswith("https://")) or url in seen:
            continue
        seen.add(url)
        thumb = (result.get("thumbnail") or {}).get("src")
        images.append(
            {
                "url": url,
                "thumb": thumb if isinstance(thumb, str) and thumb.startswith("https://") else url,
                "width": int(props.get("width") or 0),
                "height": int(props.get("height") or 0),
                "origin": "brave",
            }
        )
        if len(images) == limit:
            break
    return images


async def image_candidates(
    media_type: str,
    source: str | None,
    query: str,
    keys: dict,
    *,
    kind: str = "backdrop",
    limit: int = 8,
) -> list[dict]:
    """Gallery candidates for the chat: provider art first (best quality and
    rights), then Commons, then the Brave web tier. Never raises."""
    limit = max(1, min(int(limit or 8), 12))
    query = (query or "").strip()
    out: list[dict] = []
    seen: set[str] = set()

    def add(items: list[dict]) -> None:
        for item in items:
            url = item.get("url")
            if url and url not in seen:
                seen.add(url)
                out.append(item)

    try:
        async with _client() as client:
            parts = (source or "").split(":")
            try:
                if (
                    media_type in ("movie", "tv")
                    and len(parts) == 3
                    and parts[0] == "tmdb"
                    and (keys or {}).get("tmdb")
                ):
                    want = "posters" if kind == "cover" else "backdrops"
                    add(
                        await _tmdb_images(
                            client, parts[1], parts[2], keys["tmdb"], want=want, limit=limit
                        )
                    )
            except Exception as exc:
                log.debug("TMDB image tier failed for %s: %s", source, _safe_error(exc))
            if media_type == "game" and source and len(out) < limit:
                urls = await backdrop_candidates(media_type, source, keys)
                add(
                    [
                        {"url": u, "thumb": u, "width": 0, "height": 0, "origin": parts[0]}
                        for u in urls
                    ]
                )
            if len(out) < limit and query:
                try:
                    add(
                        await _commons_images(
                            client, query, landscape_only=(kind != "cover"), limit=limit
                        )
                    )
                except Exception:
                    log.debug("Commons image tier failed for %r", query, exc_info=True)
            brave = (keys or {}).get("brave")
            if len(out) < limit and query and brave:
                try:
                    add(await _brave_images(client, query, brave, limit=limit))
                except Exception:
                    log.debug("Brave image tier failed for %r", query, exc_info=True)
    except Exception:
        log.warning("image search failed for %s %r", source, query, exc_info=True)
    return out[:limit]


async def media_details(
    media_type: str, source: str | None, query: str | None, keys: dict
) -> dict:
    """Rich work-level detail for chat: synopsis, genres, length, top cast.

    Never raises; failures come back as {"error": ...}.
    """
    parts = (source or "").split(":")
    query = (query or "").strip()
    try:
        if media_type == "book":
            context = await book_context(source, query, "")
            if not context or not any(context.values()):
                return {"error": "No detailed record found for that book."}
            return {
                "overview": context.get("description") or "",
                "subjects": context.get("subjects") or [],
                "people": context.get("people") or [],
                "places": context.get("places") or [],
                "times": context.get("times") or [],
            }
        async with _client() as client:
            if media_type in ("movie", "tv"):
                key = (keys or {}).get("tmdb")
                if not key:
                    return {"error": "TMDB API key not set — add it in Settings, Media lookup"}
                kind = "movie" if media_type == "movie" else "tv"
                tmdb_id = None
                if len(parts) == 3 and parts[0] == "tmdb":
                    kind, tmdb_id = parts[1], parts[2]
                elif query:
                    search = await client.get(
                        f"https://api.themoviedb.org/3/search/{kind}",
                        params={"query": query, "api_key": key},
                    )
                    search.raise_for_status()
                    results = (search.json() or {}).get("results") or []
                    if results:
                        tmdb_id = results[0].get("id")
                if not tmdb_id:
                    return {"error": "Nothing found — pass the item's source id or a title."}
                resp = await client.get(
                    f"https://api.themoviedb.org/3/{kind}/{tmdb_id}",
                    params={"api_key": key, "append_to_response": "credits"},
                )
                resp.raise_for_status()
                data = resp.json() or {}
                cast = ((data.get("credits") or {}).get("cast")) or []
                return {
                    "title": data.get("title") or data.get("name"),
                    "year": _year(data.get("release_date") or data.get("first_air_date")),
                    "overview": (data.get("overview") or "")[:1200],
                    "genres": [g.get("name") for g in (data.get("genres") or [])[:6]],
                    "runtime_minutes": data.get("runtime"),
                    "episodes": data.get("number_of_episodes"),
                    "cast": [c.get("name") for c in cast[:5]],
                    "source": f"tmdb:{kind}:{tmdb_id}",
                }
            if media_type == "game":
                if (keys or {}).get("igdb_id") and (keys or {}).get("igdb_secret"):
                    if len(parts) == 2 and parts[0] == "igdb":
                        body = (
                            "fields name, summary, genres.name, first_release_date; "
                            f"where id = {int(parts[1])};"
                        )
                    elif query:
                        safe = query.replace('"', "")
                        body = (
                            f'search "{safe}"; '
                            "fields name, summary, genres.name, first_release_date; limit 1;"
                        )
                    else:
                        return {"error": "Pass the item's source id or a title."}
                    resp = await _igdb_post(client, keys, body)
                    games = resp.json() or []
                    if games:
                        game = games[0]
                        return {
                            "title": game.get("name"),
                            "overview": (game.get("summary") or "")[:1200],
                            "genres": [
                                g.get("name") for g in (game.get("genres") or [])[:6]
                            ],
                        }
                if len(parts) == 2 and parts[0] == "steam":
                    resp = await client.get(
                        "https://store.steampowered.com/api/appdetails",
                        params={"appids": parts[1], "cc": "us", "l": "en"},
                    )
                    resp.raise_for_status()
                    data = ((resp.json() or {}).get(parts[1]) or {}).get("data") or {}
                    if data:
                        return {
                            "title": data.get("name"),
                            "overview": (data.get("short_description") or "")[:1200],
                            "genres": [
                                g.get("description")
                                for g in (data.get("genres") or [])[:6]
                            ],
                        }
                return {"error": "No detailed record found for that game."}
            return {"error": f"Unknown media type: {media_type}"}
    except Exception as exc:
        safe = _safe_error(exc)
        log.warning("media details failed for %s %r: %s", source, query, safe)
        return {"error": f"Details lookup failed: {safe}"}


async def similar_media(media_type: str, source: str | None, keys: dict) -> dict:
    """TMDB recommendations for films and TV. Other types return a note so
    the model reasons from the library instead of failing silently."""
    parts = (source or "").split(":")
    if media_type not in ("movie", "tv") or len(parts) != 3 or parts[0] != "tmdb":
        return {
            "candidates": [],
            "note": "No recommendation provider for this type — reason from the "
            "user's library (media_stats, their ratings) and your own knowledge.",
        }
    key = (keys or {}).get("tmdb")
    if not key:
        return {"candidates": [], "error": "TMDB API key not set — add it in Settings"}
    try:
        async with _client() as client:
            results: list[dict] = []
            for endpoint in ("recommendations", "similar"):
                resp = await client.get(
                    f"https://api.themoviedb.org/3/{parts[1]}/{parts[2]}/{endpoint}",
                    params={"api_key": key},
                )
                resp.raise_for_status()
                results = (resp.json() or {}).get("results") or []
                if results:
                    break
            return {
                "candidates": [
                    {
                        "title": r.get("title") or r.get("name"),
                        "year": _year(r.get("release_date") or r.get("first_air_date")),
                        "overview": (r.get("overview") or "")[:220],
                        "source": f"tmdb:{parts[1]}:{r.get('id')}",
                    }
                    for r in results[:8]
                ]
            }
    except Exception as exc:
        safe = _safe_error(exc)
        log.warning("similar lookup failed for %s: %s", source, safe)
        return {"candidates": [], "error": f"Recommendation lookup failed: {safe}"}


async def book_backdrop_candidates(queries: list[str]) -> list[str]:
    """Find high-resolution landscape photographs for OMNIA's book concepts.

    Commons search is keyless. Results are interleaved across the concept
    queries so the vision pass sees distinct interpretations rather than three
    near-duplicates from the first phrase.
    """
    searches = [q.strip() for q in queries if isinstance(q, str) and q.strip()][:3]
    if not searches:
        return []
    groups: list[list[str]] = []
    async with _client() as client:
        for query in searches:
            # Per query: a 429 on the third phrase must not discard the
            # photographs the first two already found.
            try:
                images = await _commons_images(client, query, landscape_only=True, limit=4)
            except Exception as exc:
                log.warning("book backdrop search failed for %r: %s", query, _safe_error(exc))
                continue
            groups.append([image["url"] for image in images])

    interleaved: list[str] = []
    for rank in range(4):
        for group in groups:
            if rank < len(group) and group[rank] not in interleaved:
                interleaved.append(group[rank])
    return interleaved[:8]


async def _game_search(client: httpx.AsyncClient, query: str, keys: dict) -> dict:
    """Game providers in quality order, falling through outages: IGDB (Twitch
    credentials), RAWG (key, historically flaky), then Steam — keyless, so
    game lookups always have a working floor."""
    errors: list[str] = []
    if keys.get("igdb_id") and keys.get("igdb_secret"):
        try:
            candidates = await _igdb(client, query, keys)
            if candidates:
                return {"candidates": candidates}
            errors.append("IGDB: no matches")
        except Exception as exc:
            safe = _safe_error(exc)
            log.warning("IGDB lookup failed: %s", safe)
            errors.append(f"IGDB: {safe}")
    if keys.get("rawg"):
        try:
            candidates = await _rawg(client, query, keys["rawg"])
            if candidates:
                return {"candidates": candidates}
            errors.append("RAWG: no matches")
        except Exception as exc:
            safe = _safe_error(exc)
            log.warning("RAWG lookup failed: %s", safe)
            errors.append(f"RAWG: {safe}")
    try:
        candidates = await _steam(client, query)
        if candidates:
            return {"candidates": candidates}
        errors.append("Steam: no matches")
    except Exception as exc:
        safe = _safe_error(exc)
        log.warning("Steam lookup failed: %s", safe)
        errors.append(f"Steam: {safe}")
    return {"candidates": [], "error": "; ".join(errors)}


# IGDB app tokens outlive any request by weeks; cache per client id AND
# secret, so rotating the secret in Settings misses the old entry.
_IGDB_TOKENS: dict[str, tuple[str, float]] = {}
IGDB_IMG = "https://images.igdb.com/igdb/image/upload"


def _igdb_cache_key(keys: dict) -> str:
    secret = hashlib.sha256(keys["igdb_secret"].encode()).hexdigest()[:8]
    return f"{keys['igdb_id']}:{secret}"


async def _igdb_token(client: httpx.AsyncClient, keys: dict) -> str:
    client_id = keys["igdb_id"]
    cache_key = _igdb_cache_key(keys)
    cached = _IGDB_TOKENS.get(cache_key)
    if cached and cached[1] > time.time() + 60:
        return cached[0]
    # Form body, not query string: a URL with the secret in it ends up in
    # httpx error text and proxy logs.
    resp = await client.post(
        "https://id.twitch.tv/oauth2/token",
        data={
            "client_id": client_id,
            "client_secret": keys["igdb_secret"],
            "grant_type": "client_credentials",
        },
    )
    resp.raise_for_status()
    data = resp.json() or {}
    token = data.get("access_token")
    if not token:
        raise RuntimeError("Twitch token exchange returned no access_token")
    _IGDB_TOKENS[cache_key] = (token, time.time() + float(data.get("expires_in") or 3600))
    return token


def _igdb_headers(keys: dict, token: str) -> dict:
    return {"Client-ID": keys["igdb_id"], "Authorization": f"Bearer {token}"}


async def _igdb_post(client: httpx.AsyncClient, keys: dict, body: str) -> httpx.Response:
    """POST an IGDB games query. A 401 means Twitch revoked the cached token
    (or the app's secret was regenerated): drop it, exchange once more, and
    retry once. Anything else propagates to the caller's fallback."""

    async def post(token: str) -> httpx.Response:
        resp = await client.post(
            "https://api.igdb.com/v4/games", content=body, headers=_igdb_headers(keys, token)
        )
        resp.raise_for_status()
        return resp

    token = await _igdb_token(client, keys)
    try:
        return await post(token)
    except httpx.HTTPStatusError as exc:
        if exc.response is None or exc.response.status_code != 401:
            raise
    _IGDB_TOKENS.pop(_igdb_cache_key(keys), None)
    return await post(await _igdb_token(client, keys))


async def _igdb(client: httpx.AsyncClient, query: str, keys: dict) -> list[dict]:
    safe = query.replace("\\", " ").replace('"', '\\"')
    body = (
        f'search "{safe}"; fields name, first_release_date, cover.image_id, '
        "involved_companies.company.name, involved_companies.developer; "
        f"limit {MAX_CANDIDATES};"
    )
    resp = await _igdb_post(client, keys, body)
    candidates = []
    for game in (resp.json() or [])[:MAX_CANDIDATES]:
        year = None
        if game.get("first_release_date"):
            try:
                year = time.gmtime(int(game["first_release_date"])).tm_year
            except (ValueError, OverflowError, OSError):
                year = None
        developer = None
        for involved in game.get("involved_companies") or []:
            name = (involved.get("company") or {}).get("name")
            if involved.get("developer") and name:
                developer = name
                break
        image_id = (game.get("cover") or {}).get("image_id")
        candidates.append(
            {
                "title": game.get("name"),
                "creator": developer,
                "year": year,
                "cover_url": f"{IGDB_IMG}/t_cover_big_2x/{image_id}.jpg" if image_id else None,
                "total": None,
                "source": f"igdb:{game.get('id')}",
            }
        )
    return candidates


async def _steam(client: httpx.AsyncClient, query: str) -> list[dict]:
    resp = await client.get(
        "https://store.steampowered.com/api/storesearch/",
        params={"term": query, "cc": "us", "l": "en"},
    )
    resp.raise_for_status()
    items = (resp.json() or {}).get("items") or []
    candidates = []
    for item in items[:MAX_CANDIDATES]:
        appid = item.get("id")
        candidates.append(
            {
                "title": item.get("name"),
                "creator": None,
                "year": None,
                # The library capsule is Steam's real 2:3 cover; the few games
                # without one just fail cover ingest and stay coverless.
                "cover_url": (
                    f"https://steamcdn-a.akamaihd.net/steam/apps/{appid}/library_600x900_2x.jpg"
                    if appid
                    else None
                ),
                "total": None,
                "source": f"steam:{appid}",
            }
        )
    # Developer + year for the top hit only — one extra call, same pattern as
    # the TMDB credits lookup.
    if candidates and items:
        try:
            appid = items[0]["id"]
            detail = await client.get(
                "https://store.steampowered.com/api/appdetails",
                params={"appids": appid, "cc": "us", "l": "en"},
            )
            detail.raise_for_status()
            data = ((detail.json() or {}).get(str(appid)) or {}).get("data") or {}
            developers = data.get("developers") or []
            if developers:
                candidates[0]["creator"] = developers[0]
            released = (data.get("release_date") or {}).get("date") or ""
            match = re.search(r"(19|20)\d{2}", released)
            if match:
                candidates[0]["year"] = int(match.group(0))
        except Exception:
            log.debug("Steam appdetails failed; keeping bare search hit", exc_info=True)
    return candidates


async def _rawg(client: httpx.AsyncClient, query: str, key: str) -> list[dict]:
    resp = await client.get(
        "https://api.rawg.io/api/games",
        params={"search": query, "key": key, "page_size": MAX_CANDIDATES},
    )
    resp.raise_for_status()
    results = (resp.json() or {}).get("results") or []
    return [
        {
            "title": r.get("name"),
            "creator": None,
            "year": _year(r.get("released")),
            "cover_url": r.get("background_image"),
            "total": None,
            "source": f"rawg:{r.get('id')}",
        }
        for r in results[:MAX_CANDIDATES]
    ]


# -- cover ingestion ----------------------------------------------------------


async def _resolve_addresses(host: str) -> list[str]:
    """Every address `host` resolves to; tests replace this to stay offline."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return [str(info[4][0]).split("%", 1)[0] for info in infos]


async def public_https_refusal(url: httpx.URL) -> str | None:
    """Why one hop of a server-side fetch may not go ahead, or None.

    Only https to a host that resolves solely to public addresses passes, so a
    redirect cannot reach loopback, the LAN, or the cloud metadata endpoint.
    The reason is "scheme", "url", "dns" or "private"; each caller words it.
    Shared with recipe_import.fetch.
    """
    if url.scheme != "https":
        return "scheme"
    try:
        host = url.host
    except UnicodeError:  # not IDNA ("xn--localhost"): httpx raises reading it
        return "url"
    if not host:
        return "url"
    try:
        addresses = [str(ipaddress.ip_address(host))]
    except ValueError:
        try:
            addresses = await _resolve_addresses(url.raw_host.decode("ascii"))
        except (OSError, UnicodeError):
            return "dns"
    try:
        public = bool(addresses) and all(ipaddress.ip_address(a).is_global for a in addresses)
    except ValueError:
        public = False
    return None if public else "private"


_HOP_REFUSALS = {
    "scheme": (400, "Cover URLs must be https"),
    "url": (400, "Not a valid image URL"),
    "dns": (502, "Cover download failed: DNS"),
    "private": (400, "Image host is not public"),
}


async def _require_public_https(url: httpx.URL) -> None:
    """One hop of an image download (public_https_refusal)."""
    refusal = await public_https_refusal(url)
    if refusal:
        raise NoteError(*_HOP_REFUSALS[refusal])


def stacked_encoding(resp) -> bool:
    """Whether a response names more than one Content-Encoding. httpx decodes
    every layer of a chunk in one call, before a size cap can count it, so a
    few KB of gzip inside gzip is gigabytes in memory. One layer is bounded
    (about 1,000 to 1 per 64 KB read); real servers send one. Shared with
    recipe_import.fetch."""
    return "," in (resp.headers.get("content-encoding") or "")


async def _read_image(resp) -> tuple[bytes, str]:
    resp.raise_for_status()
    ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
    if ctype not in COVER_TYPES:
        raise NoteError(400, f"Not an image: {ctype or 'unknown content type'}")
    if stacked_encoding(resp):
        raise NoteError(502, "Cover download failed: encoding")
    size = 0
    chunks = []
    async for chunk in resp.aiter_bytes():
        size += len(chunk)
        if size > COVER_MAX_BYTES:
            raise NoteError(413, "Cover image is larger than 10 MB")
        chunks.append(chunk)
    data = b"".join(chunks)
    if not data:
        raise NoteError(400, "Empty image")
    return data, ctype


async def fetch_image_bytes(url: str) -> tuple[bytes, str]:
    """Download one image and return (bytes, content type).

    Redirects are followed by hand so every hop is checked; the whole download
    shares one deadline and the 10 MB cap. Errors carry status and host only.
    """
    try:
        target = httpx.URL((url or "").strip())
    except httpx.InvalidURL:
        raise NoteError(400, "Not a valid image URL")
    try:
        async with asyncio.timeout(COVER_TIMEOUT), _client() as client:
            for _hop in range(COVER_MAX_REDIRECTS + 1):
                await _require_public_https(target)
                async with client.stream("GET", str(target), follow_redirects=False) as resp:
                    location = resp.headers.get("location")
                    if 300 <= resp.status_code < 400 and location:
                        target = target.join(location)
                        continue
                    return await _read_image(resp)
            raise NoteError(502, "Too many redirects")
    except TimeoutError:
        raise NoteError(504, "Cover download timed out")
    # UnicodeError: a redirect to a host that is not IDNA (recipe_import.fetch).
    except (httpx.HTTPError, httpx.InvalidURL, UnicodeError) as exc:
        raise NoteError(502, f"Cover download failed: {_safe_error(exc)}")


# The most pixels an image may declare before it is decoded to be shrunk. A
# few hundred KB of PNG can declare 170 megapixels, which is half a gigabyte
# once decoded, and a recipe import hands this the image URL of whatever page
# the owner pasted. No cover or dish photo comes near 50.
MAX_ART_PIXELS = 50_000_000


def _shrink_for_storage(data: bytes, ctype: str, max_edge: int) -> tuple[bytes, str]:
    """Downscale oversized art to `max_edge` as JPEG q85. Returns the bytes
    untouched when they are small enough, GIF/AVIF, or unreadable by Pillow.
    Raises NoteError for an image that declares more than MAX_ART_PIXELS:
    neither decoded here nor stored for a browser to decode."""
    if ctype not in SHRINKABLE_TYPES:
        return data, ctype
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow is a hard dependency
        return data, ctype
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.size[0] * image.size[1] <= MAX_ART_PIXELS:
                if max(image.size) <= max_edge:
                    return data, ctype
                image = image.convert("RGB")
                image.thumbnail((max_edge, max_edge))
                buffer = io.BytesIO()
                image.save(buffer, format="JPEG", quality=85, optimize=True)
                return buffer.getvalue(), "image/jpeg"
    except Image.DecompressionBombError:
        pass  # Pillow's own refusal, past 178 megapixels
    except Exception:
        return data, ctype
    raise NoteError(400, "Image has too many pixels")


async def store_image_bytes(
    state, data: bytes, ctype: str, name_hint: str, *, kind: str = "cover"
) -> dict:
    """Write image bytes into the vault's attachments/ with content-hash naming,
    re-encoded first when larger than ART_MAX_EDGE[kind].

    Returns {path, url} where url is the /api/attachments/... form — that
    prefix is what the desktop blob-swap keys on.
    """
    if ctype not in COVER_TYPES:
        raise NoteError(400, f"Not an image: {ctype or 'unknown content type'}")
    if not data:
        raise NoteError(400, "Empty image")
    data, ctype = await asyncio.to_thread(
        _shrink_for_storage, data, ctype, ART_MAX_EDGE[kind]
    )
    ext = COVER_TYPES[ctype]
    digest = hashlib.sha256(data).hexdigest()[:16]
    name = f"{_safe_stem(name_hint)}-{digest}{ext}"
    rel_path = f"{NoteStore.ATTACHMENT_DIR}/{name}"
    abs_path = state.store.resolve_attachment(rel_path)
    # Same rule as the upload route: a short file is a killed write to repair.
    if not abs_path.exists() or abs_path.stat().st_size != len(data):
        atomic_bytes(abs_path, data)
        state.autocommit.touch()
    return {"path": rel_path, "url": f"/api/attachments/{name}"}


async def ingest_cover(
    state, url: str, name_hint: str = "cover", *, kind: str = "cover"
) -> dict:
    """Download an image and store it as a vault attachment (store_image_bytes)."""
    data, ctype = await fetch_image_bytes(url)
    return await store_image_bytes(state, data, ctype, name_hint, kind=kind)
