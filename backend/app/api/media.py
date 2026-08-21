"""Media tracker API: the grid, the timeline, and quick item mutations.

Mutations are server-side note rewrites, so git history, the index, and live
updates all flow through the same machinery as ordinary note saves.
"""
from datetime import date as date_cls, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel

from pathlib import Path

from .. import media_items, media_lookup, media_theme
from ..index.db import content_hash
from ..vault import fm
from ..vault.internal import atomic_json, read_json
from ..vault.notes import NoteError, NoteStore

router = APIRouter(prefix="/api/media", tags=["media"])


# Include the newest progress entry with every grid/detail item.  The card can
# then say "Page 120" or "4/12 Watched" without loading the full timeline.
_ITEM_SELECT = """SELECT media_items.*,
    (SELECT value FROM media_log
     WHERE media_log.path = media_items.path AND media_log.kind = 'progress'
     ORDER BY media_log.at DESC, media_log.line DESC
     LIMIT 1) AS progress
    FROM media_items"""


def _today(request: Request) -> str:
    try:
        return datetime.now(ZoneInfo(request.app.state.timezone)).date().isoformat()
    except (ZoneInfoNotFoundError, AttributeError):
        return date_cls.today().isoformat()


async def _item(db, path: str) -> dict | None:
    rows = await db.execute_fetchall(
        f"{_ITEM_SELECT} WHERE media_items.path = ?", (path,)
    )
    return dict(rows[0]) if rows else None


# Sort key -> (column, default direction). NULLs always sink to the bottom:
# an unrated item below the worst rating, an unfinished one below the oldest.
_SORTS = {
    "recent": ("modified", "DESC"),
    "title": ("title COLLATE NOCASE", "ASC"),
    "year": ("year", "DESC"),
    "rating": ("rating", "DESC"),
    "finished": ("finished", "DESC"),
}


# NOTE: literal routes must stay above the /{path:path} routes or the
# catch-all swallows them (same footgun as api/notes.py).


@router.get("")
async def list_media(
    request: Request,
    media_type: str | None = Query(default=None, alias="type"),
    status: str | None = Query(default=None),
    sort: str = "recent",
    dir: str | None = None,
):
    clauses: list[str] = []
    params: list[object] = []
    if media_type:
        if media_type not in media_items.MEDIA_TYPES:
            raise NoteError(400, f"Unknown media type: {media_type}")
        clauses.append("media_type = ?")
        params.append(media_type)
    if status:
        if status not in media_items.STATUSES:
            raise NoteError(400, f"Unknown status: {status}")
        clauses.append("status = ?")
        params.append(status)
    if sort not in _SORTS:
        raise NoteError(400, f"Unknown sort: {sort}")
    column, direction = _SORTS[sort]
    if dir:
        if dir.lower() not in ("asc", "desc"):
            raise NoteError(400, f"Unknown direction: {dir}")
        direction = dir.upper()
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = await request.app.state.db.execute_fetchall(
        f"""{_ITEM_SELECT} {where}
            ORDER BY CASE WHEN {column} IS NULL OR {column} = '' THEN 1 ELSE 0 END,
                     {column} {direction},
                     title COLLATE NOCASE""",
        tuple(params),
    )
    return [dict(row) for row in rows]


@router.get("/timeline")
async def media_timeline(request: Request, limit: int = 200):
    """Consumption events newest first, ready for month grouping."""
    rows = await request.app.state.db.execute_fetchall(
        """SELECT l.at, l.kind, l.value, l.text, l.path,
                  COALESCE(NULLIF(i.alias, ''), i.title) AS title,
                  i.media_type, i.cover, i.year, i.rating, i.status
           FROM media_log l JOIN media_items i ON i.path = l.path
           ORDER BY l.at DESC, l.path, l.line DESC
           LIMIT ?""",
        (max(1, min(limit, 500)),),
    )
    return [dict(row) for row in rows]


@router.get("/lookup")
async def lookup_media(
    request: Request,
    media_type: str = Query(alias="type"),
    q: str = Query(default=""),
):
    if media_type not in media_items.MEDIA_TYPES:
        raise NoteError(400, f"Unknown media type: {media_type}")
    keys = media_lookup.load_media_keys(request.app.state.settings.data_dir)
    return await media_lookup.search_media(media_type, q, keys)


@router.get("/match")
async def match_media(request: Request, title: str = Query(default="")):
    """Is this title already tracked? Drives the add dialog's 'log a rewatch'
    branch instead of silently creating a duplicate. Declared before the
    {path:path} routes so the catch-all doesn't swallow it."""
    state = request.app.state
    needle = title.strip()
    if not needle:
        return {"tracked": False}
    slug_path = f"{media_items.MEDIA_DIR}/{media_items.media_slug(needle)}.md"
    rows = await state.db.execute_fetchall(
        "SELECT path FROM media_items WHERE path = ? "
        "OR lower(COALESCE(NULLIF(alias, ''), title)) = ? LIMIT 1",
        (slug_path, needle.lower()),
    )
    if not rows:
        return {"tracked": False}
    path = rows[0]["path"]
    plays = await state.db.execute_fetchall(
        "SELECT COUNT(*) c FROM media_log WHERE path = ? AND kind IN ('watched', 'finished')",
        (path,),
    )
    return {
        "tracked": True,
        "path": path,
        "item": await _item(state.db, path),
        "plays": plays[0]["c"] if plays else 0,
    }


class CoverBody(BaseModel):
    url: str
    name_hint: str | None = None


@router.post("/cover")
async def ingest_cover(request: Request, body: CoverBody):
    return await media_lookup.ingest_cover(
        request.app.state, body.url, body.name_hint or "cover"
    )


_KEY_NAMES = ("tmdb", "rawg", "igdb_id", "igdb_secret", "brave")


def _key_hint(key: str | None) -> dict:
    # The key itself never leaves the server.
    return {"set": bool(key), "hint": f"…{key[-4:]}" if key else None}


@router.get("/settings")
async def media_settings(request: Request):
    keys = media_lookup.load_media_keys(request.app.state.settings.data_dir)
    return {name: _key_hint(keys.get(name)) for name in _KEY_NAMES}


class MediaSettingsBody(BaseModel):
    # None = unchanged, "" = clear.
    tmdb: str | None = None
    rawg: str | None = None
    igdb_id: str | None = None
    igdb_secret: str | None = None
    brave: str | None = None


@router.put("/settings")
async def save_media_settings(request: Request, body: MediaSettingsBody):
    data_dir = request.app.state.settings.data_dir
    keys = media_lookup.load_media_keys(data_dir)
    for name in _KEY_NAMES:
        value = getattr(body, name)
        if value is None:
            continue
        value = value.strip()
        if value:
            keys[name] = value
        else:
            keys.pop(name, None)
    media_lookup.save_media_keys(data_dir, keys)
    return {name: _key_hint(keys.get(name)) for name in _KEY_NAMES}


# -- the hub: profile, stats, and curated rows --------------------------------
# Profile and favorites live in vault-internal config (synced, git-versioned),
# NOT in frontmatter — favorites are a curated ordered list across items.

_PROFILE_KEY = "media_profile"
MAX_FAVORITES = 4
_MAX_MEDIA_HISTORY = 30


def _config_path(state) -> Path:
    return state.settings.vault_dir / NoteStore.INTERNAL_DIR / "config.json"


def _profile_config(state) -> dict:
    config = read_json(_config_path(state), {}) or {}
    value = config.get(_PROFILE_KEY)
    return value if isinstance(value, dict) else {}


def _public_profile(state) -> dict:
    """Config profile with the login username as the display-name floor."""
    cfg = _profile_config(state)
    username = getattr(getattr(state, "auth", None), "username", None) or "owner"
    def _num(value, default, lo, hi):
        try:
            n = float(value)
        except (TypeError, ValueError):
            return default
        return max(lo, min(hi, n))

    def _history(key):
        seen: list[str] = []
        for url in cfg.get(key) or []:
            url = str(url)
            if url.startswith("/api/attachments/") and url not in seen:
                seen.append(url)
        return seen[:_MAX_MEDIA_HISTORY]

    return {
        "name": str(cfg.get("name") or "").strip() or username,
        "location": str(cfg.get("location") or "").strip(),
        "avatar": str(cfg.get("avatar") or "").strip(),
        "banner": str(cfg.get("banner") or "").strip(),
        "avatar_zoom": _num(cfg.get("avatar_zoom"), 1.0, 1.0, 4.0),
        "avatar_x": _num(cfg.get("avatar_x"), 50.0, 0.0, 100.0),
        "avatar_y": _num(cfg.get("avatar_y"), 50.0, 0.0, 100.0),
        "banner_zoom": _num(cfg.get("banner_zoom"), 1.0, 1.0, 4.0),
        "banner_x": _num(cfg.get("banner_x"), 50.0, 0.0, 100.0),
        "banner_y": _num(cfg.get("banner_y"), 50.0, 0.0, 100.0),
        "avatars": _history("avatars"),
        "banners": _history("banners"),
    }


async def _resolved_favorites(state) -> list[dict]:
    """Config paths resolved to items, order kept, deleted items filtered."""
    favorites = []
    for value in (_profile_config(state).get("favorites") or [])[:MAX_FAVORITES]:
        item = await _item(state.db, str(value))
        if item:
            favorites.append(item)
    return favorites


@router.get("/hub")
async def media_hub(request: Request):
    """Everything the hub landing page needs in one call."""
    state = request.app.state
    db = state.db

    async def count(where: str = "", params: tuple = ()) -> int:
        rows = await db.execute_fetchall(
            f"SELECT COUNT(*) AS n FROM media_items {where}", params
        )
        return rows[0]["n"]

    year = _today(request)[:4]

    async def finished_count(media_type: str, this_year: bool) -> int:
        where = "WHERE status = 'finished' AND media_type = ?"
        params: list = [media_type]
        if this_year:
            where += " AND finished LIKE ?"
            params.append(f"{year}-%")
        return await count(where, tuple(params))

    async def pages_read(this_year: bool) -> int:
        # Pages read = pages of the books you finished (their `total`) plus the
        # current page of the books you're still reading.
        where = "WHERE status = 'finished' AND media_type = 'book' AND total IS NOT NULL"
        params: list = []
        if this_year:
            where += " AND finished LIKE ?"
            params.append(f"{year}-%")
        rows = await db.execute_fetchall(
            f"SELECT COALESCE(SUM(total), 0) AS n FROM media_items {where}", tuple(params)
        )
        total = rows[0]["n"]

        # In-progress books count at their latest logged page, in either period.
        active = await db.execute_fetchall(
            """SELECT (SELECT value FROM media_log
                        WHERE media_log.path = media_items.path
                          AND media_log.kind = 'progress'
                        ORDER BY media_log.at DESC, media_log.line DESC
                        LIMIT 1) AS progress
               FROM media_items
               WHERE media_type = 'book' AND status = 'active'"""
        )
        for row in active:
            match = media_items._PAGE_RE.match(str(row["progress"] or ""))
            if match:
                total += int(match.group(1))
        return total

    async def period_block(this_year: bool) -> dict:
        return {
            "pages": await pages_read(this_year),
            "movies": await finished_count("movie", this_year),
            "shows": await finished_count("tv", this_year),
            "games": await finished_count("game", this_year),
        }

    stats = {
        "watchlist": await count("WHERE status = 'planned'"),
        "this_year": await period_block(True),
        "lifetime": await period_block(False),
    }

    watchlist = await db.execute_fetchall(
        f"""{_ITEM_SELECT} WHERE status = 'planned'
            ORDER BY CASE WHEN modified IS NULL OR modified = '' THEN 1 ELSE 0 END,
                     modified DESC, title COLLATE NOCASE
            LIMIT 8"""
    )

    # Recent activity: the newest log event per item, four distinct items.
    events = await db.execute_fetchall(
        """SELECT l.at, l.kind, l.value, l.text, l.path
           FROM media_log l JOIN media_items i ON i.path = l.path
           ORDER BY l.at DESC, l.line DESC LIMIT 60"""
    )
    recent = []
    seen: set[str] = set()
    for event in events:
        if event["path"] in seen:
            continue
        seen.add(event["path"])
        item = await _item(db, event["path"])
        if item:
            recent.append(
                {
                    "at": event["at"],
                    "kind": event["kind"],
                    "value": event["value"],
                    "text": event["text"],
                    "item": item,
                }
            )
        if len(recent) == 4:
            break

    diary = await db.execute_fetchall(
        """SELECT l.at, l.kind, l.value, l.text, l.path,
                  COALESCE(NULLIF(i.alias, ''), i.title) AS title, i.media_type
           FROM media_log l JOIN media_items i ON i.path = l.path
           ORDER BY l.at DESC, l.line DESC LIMIT 10"""
    )

    # Rating distribution for the Letterboxd-style histogram: ten buckets.
    rating_rows = await db.execute_fetchall(
        "SELECT rating, COUNT(*) AS n FROM media_items"
        " WHERE rating IS NOT NULL GROUP BY rating"
    )
    counts = [0] * 10
    for row in rating_rows:
        if isinstance(row["rating"], int) and 1 <= row["rating"] <= 10:
            counts[row["rating"] - 1] = row["n"]

    return {
        "profile": _public_profile(state),
        "stats": stats,
        "ratings": {"counts": counts, "total": sum(counts)},
        "favorites": await _resolved_favorites(state),
        "watchlist": [dict(row) for row in watchlist],
        "recent": recent,
        "diary": [dict(row) for row in diary],
    }


class ProfileBody(BaseModel):
    # None = unchanged; empty string (or empty list) clears.
    name: str | None = None
    location: str | None = None
    avatar: str | None = None
    banner: str | None = None
    avatar_zoom: float | None = None
    avatar_x: float | None = None
    avatar_y: float | None = None
    banner_zoom: float | None = None
    banner_x: float | None = None
    banner_y: float | None = None
    favorites: list[str] | None = None


@router.put("/profile")
async def save_profile(request: Request, body: ProfileBody):
    state = request.app.state
    config_path = _config_path(state)
    config = read_json(config_path, {}) or {}
    profile = dict(config.get(_PROFILE_KEY) or {})

    for field, cap in (("name", 80), ("location", 120)):
        value = getattr(body, field)
        if value is None:
            continue
        value = " ".join(value.split())[:cap]
        if value:
            profile[field] = value
        else:
            profile.pop(field, None)

    for field in ("avatar", "banner"):
        value = getattr(body, field)
        if value is None:
            continue
        value = value.strip()
        if value and not value.startswith("/api/attachments/"):
            raise NoteError(400, f"{field.capitalize()} must be an uploaded attachment URL")
        if value:
            profile[field] = value
            # Keep it in the picker's history — most-recently-used first.
            hist_key = f"{field}s"
            history = [u for u in (profile.get(hist_key) or []) if str(u) != value]
            history.insert(0, value)
            profile[hist_key] = history[:_MAX_MEDIA_HISTORY]
        else:
            profile.pop(field, None)

    # Framing (zoom + focal point) for however the images are cropped.
    for field, lo, hi in (
        ("avatar_zoom", 1.0, 4.0),
        ("avatar_x", 0.0, 100.0),
        ("avatar_y", 0.0, 100.0),
        ("banner_zoom", 1.0, 4.0),
        ("banner_x", 0.0, 100.0),
        ("banner_y", 0.0, 100.0),
    ):
        value = getattr(body, field)
        if value is None:
            continue
        profile[field] = max(lo, min(hi, float(value)))

    if body.favorites is not None:
        if len(body.favorites) > MAX_FAVORITES:
            raise NoteError(400, f"Favorites are limited to {MAX_FAVORITES} items")
        cleaned: list[str] = []
        for value in body.favorites:
            path = str(value)
            if path in cleaned:
                continue
            if await _item(state.db, path) is None:
                raise NoteError(400, f"Not a tracked media item: {path}")
            cleaned.append(path)
        if cleaned:
            profile["favorites"] = cleaned
        else:
            profile.pop("favorites", None)

    config[_PROFILE_KEY] = profile
    atomic_json(config_path, config)
    state.autocommit.touch()
    return {
        "profile": _public_profile(state),
        "favorites": await _resolved_favorites(state),
    }


class CreateMedia(BaseModel):
    title: str
    media_type: str
    status: str = "planned"
    creator: str | None = None
    year: int | None = None
    total: int | None = None
    rating: int | None = None
    # Either an already-uploaded /api/attachments/... URL, or a remote https
    # URL to ingest. A failed ingest degrades to a coverless item.
    cover: str | None = None
    cover_url: str | None = None
    source: str | None = None
    review: str | None = None
    client_id: str | None = None


@router.post("")
async def create_media(request: Request, body: CreateMedia):
    state = request.app.state
    title = body.title.strip()
    if not title:
        raise NoteError(400, "A title is required")
    cover = body.cover
    cover_error = None
    if not cover and body.cover_url:
        try:
            ingested = await media_lookup.ingest_cover(
                state, body.cover_url, media_items.media_slug(title)
            )
            cover = ingested["url"]
        except NoteError as exc:
            cover_error = exc.detail
    try:
        content = media_items.build_item_content(
            title=title,
            media_type=body.media_type,
            status=body.status,
            creator=body.creator,
            year=body.year,
            total=body.total,
            rating=body.rating,
            cover=cover,
            source=body.source,
            review=body.review,
            today=_today(request),
        )
    except ValueError as exc:
        raise NoteError(400, str(exc))
    taken = {note.path for note in state.store.list_notes()}
    path = media_items.next_media_path(title, taken)
    written = state.store.create(path, content)
    await state.indexer.index_note(path, written)
    state.autocommit.touch()
    state.live.note_changed(path, content_hash(written), origin=body.client_id)
    # The page shows up instantly; the Media job sources its backdrop in the
    # background and the live note event fills the result in.
    media_theme.enrich_soon(state, path)
    result = {"path": path, "item": await _item(state.db, path)}
    if cover_error:
        result["cover_error"] = cover_error
    return result


class PatchMedia(BaseModel):
    rating: int | None = None
    status: str | None = None
    alias: str | None = None
    base_revision: str | None = None


class LogMedia(BaseModel):
    kind: str
    value: str | int | None = None
    text: str | None = None
    date: str | None = None
    # Lets one "finished" click carry the rating into the same log line.
    rating: int | None = None
    base_revision: str | None = None


async def _rewrite(
    request: Request,
    path: str,
    *,
    rating: int | None = None,
    status: str | None = None,
    entry: dict | None = None,
    alias: str | None = None,
    review: str | None = None,
    remove_entry: dict | None = None,
    base_revision: str | None,
) -> dict:
    state = request.app.state
    content = state.store.read(path)
    if base_revision is not None and base_revision != content_hash(content):
        raise NoteError(409, "Note changed elsewhere — reload and retry")
    try:
        if remove_entry is not None:
            updated, summary = media_items.remove_log_entry(
                content, str(remove_entry.get("at")), str(remove_entry.get("text"))
            )
        elif review is not None:
            if not media_items.is_media_item(fm.parse(content)[0]):
                raise ValueError("Not a media note")
            updated, summary = media_items.set_review(content, review), "Review updated"
        else:
            updated, summary = media_items.apply_update(
                content,
                rating=rating,
                status=status,
                entry=entry,
                alias=alias,
                today=_today(request),
            )
    except ValueError as exc:
        raise NoteError(400, str(exc))
    written = state.store.write(path, updated)
    await state.indexer.index_note(path, written)
    state.autocommit.touch()
    # No origin: the open editor must receive its own note's change and merge
    # the server-side rewrite rather than ignoring an origin-tagged echo.
    state.live.note_changed(path, content_hash(written))
    return {
        "path": path,
        "item": await _item(state.db, path),
        "revision": content_hash(written),
        "content": written,
        "summary": summary,
    }


@router.get("/{path:path}")
async def get_media(request: Request, path: str):
    """Everything the media page needs in one call: the indexed item, its log
    events, and the note itself (meta carries the backdrop)."""
    state = request.app.state
    content = state.store.read(path)
    meta, _body = fm.parse(content)
    if not media_items.is_media_item(meta):
        raise NoteError(400, f"Not a media note: {path}")
    events = await state.db.execute_fetchall(
        "SELECT at, kind, value, text FROM media_log WHERE path = ?"
        " ORDER BY at DESC, line DESC",
        (path,),
    )
    return {
        "path": path,
        "item": await _item(state.db, path),
        "events": [dict(row) for row in events],
        "content": content,
        "meta": meta,
        "revision": content_hash(content),
    }


@router.post("/{path:path}/enrich")
async def enrich_media(request: Request, path: str):
    """Re-run the Media job for one item — the page's New backdrop button."""
    result = await media_theme.enrich_item(request.app.state, path, force=True)
    result["item"] = await _item(request.app.state.db, path)
    return result


@router.patch("/{path:path}")
async def patch_media(request: Request, path: str, body: PatchMedia):
    return await _rewrite(
        request,
        path,
        rating=body.rating,
        status=body.status,
        alias=body.alias,
        base_revision=body.base_revision,
    )


class ReviewBody(BaseModel):
    review: str
    base_revision: str | None = None


@router.put("/{path:path}/review")
async def set_review(request: Request, path: str, body: ReviewBody):
    """The media page's inline review editor. Replaces everything before
    ## Log; the log itself is untouchable from here."""
    return await _rewrite(
        request, path, review=body.review, base_revision=body.base_revision
    )


@router.post("/{path:path}/log")
async def log_media(request: Request, path: str, body: LogMedia):
    entry = {"kind": body.kind, "value": body.value, "text": body.text, "date": body.date}
    return await _rewrite(
        request,
        path,
        rating=body.rating,
        status=None,
        entry=entry,
        base_revision=body.base_revision,
    )


class DeleteLog(BaseModel):
    at: str
    text: str
    base_revision: str | None = None


@router.post("/{path:path}/log/delete")
async def delete_log_entry(request: Request, path: str, body: DeleteLog):
    """Remove one ``## Log`` line (matched by its date + text), e.g. a watch."""
    return await _rewrite(
        request,
        path,
        remove_entry={"at": body.at, "text": body.text},
        base_revision=body.base_revision,
    )
