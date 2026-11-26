"""The Media job: sourcing each media item's backdrop still.

After an item is logged, this module finds wide scene imagery (provider
stills for film/TV/games, concept-driven Wikimedia Commons photographs for
books) and has the configured Media model (Settings → OMNIA → Media model;
falls back to the chat model) choose the one that best fits the work. The
choice lands in the note's frontmatter as `backdrop:`. The page itself wears
one standard look — black surface, white type — so no palette is generated.

No model configured, or the call fails? The provider's best-ranked still is
used directly; re-running enrichment later upgrades the pick.
"""
import asyncio
import base64
import json
import logging
from pathlib import Path

from . import media_items, media_lookup
from .index.db import content_hash
from .overseer import vision
from .overseer.provider import build_provider, image_block
from .overseer.runtime_config import (
    effective_ai,
    effective_media_model,
    load_ai_config,
    role_reasoning,
)
from .vault import fm
from .vault.notes import NoteError

log = logging.getLogger(__name__)

MAX_CANDIDATES = 3
# Media jobs that run at once. A book job holds up to three full-size Commons
# originals and makes two model calls; a bulk import queues behind this gate
# instead of starting every pipeline together.
ENRICH_CONCURRENCY = 2

ART_DIRECTION_SYSTEM = (
    "You are the art director for a personal media library. Each request hands "
    "you the artwork for one work; you answer with production-ready choices as "
    "strict JSON and nothing else."
)

BOOK_RESEARCH_SYSTEM = (
    "You are the visual researcher for a personal book library. Ground your "
    "brief in the supplied catalog metadata, then answer as strict JSON only."
)

ART_DIRECTION_PROMPT = """\
This page is for "{title}"{year_part} — a {type_word}{creator_part}.

{image_note}
{concept_note}

Choose the page's backdrop. It sits behind white type on a black page, so it
must read as cinema — wide, atmospheric, one clear subject, room for a title
to breathe. Prefer a scene from the work over poster-like key art. Reject
busy collages, images with marketing text, and tight face crops. For a book,
choose the photograph that best expresses its subject, setting, or central
idea — never a book cover or adaptation poster. For a biography or memoir,
the principal person must be visibly present; a photo showing only their
company, product, building, or event is too indirect. Prefer polished
editorial, event, workplace, or environmental photography that represents
their life as a whole. Reject screenshots, accident aftermath, and
sensational one-off incidents unless that event is the book's central
subject. The choice should feel inevitable for this work, the way a
Criterion cover fits its film.

Answer with ONLY this JSON:
{{"backdrop": <image number of the chosen still>}}
"""

# Appended for films where both names are known. The reply's "credit" must be
# one of the two verbatim — anything else is discarded.
ATTRIBUTION_ADDENDUM = """\

One more choice. This film's director is {director}; its lead studio is
{studio}. Decide whose name a knowledgeable film fan would file it under:
the director when they are a distinctive authorial voice (a Nolan,
Villeneuve, or Miyazaki film), the studio when the studio is the identity
(a Disney, Pixar, or Ghibli picture). Add that name to the SAME JSON,
verbatim from the two above:
{{"backdrop": <image number>, "credit": "{director}" or "{studio}"}}
"""

BOOK_CONCEPT_PROMPT = """\
Build a visual research brief for the book "{title}"{year_part}{creator_part}.

Open Library metadata:
{metadata}

First distill what the book is actually about. Then write three concrete
search phrases for high-quality landscape photographs on Wikimedia Commons.

Rules:
- Biography or memoir: center the principal person in a defining environment
  or activity, not a generic headshot. Example: "Elon Musk Tesla factory".
- Fiction: use its real-world setting, historical period, landscape, objects,
  or central motifs. Never use actors, screen adaptations, fan art, or covers.
- Other nonfiction: favor the defining person, place, event, object, or field.
- Queries must be 3–8 words, visually concrete, and use proper nouns when
  useful. Do not include "book", "cover", "poster", or the author's name
  unless the author is also the book's subject.

Answer with ONLY this JSON:
{{"concept": "one concise sentence", "queries": ["...", "...", "..."]}}
"""


def _parse_reply(text: str) -> dict | None:
    """The strict-JSON ask, defensively parsed anyway."""
    if not text:
        return None
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _book_plan(parsed: dict | None) -> tuple[str, list[str]]:
    parsed = parsed or {}
    concept = parsed.get("concept")
    concept = concept.strip()[:500] if isinstance(concept, str) else ""
    queries: list[str] = []
    for value in parsed.get("queries") or []:
        if not isinstance(value, str):
            continue
        query = " ".join(value.split()).strip()
        if 3 <= len(query) <= 100 and query.casefold() not in {q.casefold() for q in queries}:
            queries.append(query)
        if len(queries) == 3:
            break
    return concept, queries


def _book_metadata_text(context: dict) -> str:
    rows = []
    if context.get("description"):
        rows.append(f"Description: {context['description']}")
    for key, label in (
        ("subjects", "Subjects"),
        ("people", "People"),
        ("places", "Places"),
        ("times", "Times"),
    ):
        values = context.get(key) or []
        if values:
            rows.append(f"{label}: {', '.join(str(value) for value in values)}")
    return "\n".join(rows) or "No detailed record is available; infer carefully from the title and author."


def _ground_book_queries(context: dict, queries: list[str]) -> list[str]:
    """Biography searches must actually retrieve the person, not merely the
    organizations and objects associated with them."""
    subjects = " ".join(str(value) for value in context.get("subjects") or []).casefold()
    people = [str(value).strip() for value in context.get("people") or [] if str(value).strip()]
    is_life_story = any(word in subjects for word in ("biograph", "memoir"))
    if not (is_life_story and people):
        return queries
    person = people[0]
    grounded = []
    for query in queries:
        value = query if person.casefold() in query.casefold() else f"{person} {query}"
        grounded.append(" ".join(value.split())[:100])
    return grounded


def _record_usage(state, reply) -> None:
    ledger = getattr(state, "usage", None)
    if ledger is not None:
        ledger.record("media", getattr(reply, "usage", None))


async def _plan_book_backdrop(state, provider, meta: dict, title: str) -> tuple[str, list[str]]:
    context = await media_lookup.book_context(
        str(meta.get("source") or ""), title, str(meta.get("creator") or "")
    )
    prompt = BOOK_CONCEPT_PROMPT.format(
        title=title,
        year_part=f" ({meta['year']})" if meta.get("year") else "",
        creator_part=f" by {meta['creator']}" if meta.get("creator") else "",
        metadata=_book_metadata_text(context),
    )
    try:
        reply = await provider.chat(
            BOOK_RESEARCH_SYSTEM,
            [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
            [],
            None,
        )
        _record_usage(state, reply)
        concept, queries = _book_plan(_parse_reply(reply.text))
        return concept, _ground_book_queries(context, queries)
    except Exception:
        log.warning("Book concept planning failed for %s", title, exc_info=True)
        return "", []


async def _fetch_image(url: str) -> tuple[bytes, str, dict] | None:
    """Download a candidate still and shape it into a provider image block.

    Returns (raw bytes, content type, block): the chosen still is stored from
    these bytes, never downloaded twice.
    """
    try:
        # The checked cover download: https hops, public hosts, 10 MB cap.
        raw, ctype = await media_lookup.fetch_image_bytes(url)
        # A Commons original is a 12-30 MP decode; keep it off the event loop.
        data, media_type = await asyncio.to_thread(vision._downscale, raw, ctype)
        return raw, ctype, image_block(media_type, base64.b64encode(data).decode("ascii"))
    except Exception:
        log.debug("backdrop candidate fetch failed: %s", url, exc_info=True)
        return None


def build_media_provider(state):
    """The provider for the Media model — also what Paper transcription rides on,
    since reading handwriting off a render is the same kind of cheap vision call."""
    overseer = getattr(state, "overseer", None)
    config = getattr(state, "ai_config", None) or load_ai_config(state.settings.data_dir)
    model = effective_media_model(state.settings, config)
    if overseer is not None and overseer.provider.model == model:
        return overseer.provider
    provider_id, key, _model = effective_ai(state.settings, config)
    try:
        built = build_provider(
            state.settings,
            provider_id=provider_id,
            api_key=key,
            model=model,
            reasoning=role_reasoning(config, "media"),
        )
    except Exception:
        log.warning("Media model %s unavailable", model, exc_info=True)
        built = None
    if built is not None:
        return built
    return overseer.provider if overseer is not None else None


# Kept so existing callers (and the tests that monkeypatch them) keep working.
_build_media_provider = build_media_provider

_TYPE_WORDS = {"book": "book", "movie": "film", "tv": "television series", "game": "video game"}
_CREATOR_LABELS = {"book": "by", "movie": "directed by", "tv": "created by", "game": "by"}


async def enrich_item(state, path: str, *, force: bool = False) -> dict:
    """Source the backdrop for one media note. Returns a summary dict;
    raises NoteError only for caller mistakes (missing/non-media note)."""
    content = state.store.read(path)
    meta, _body = fm.parse(content)
    if not media_items.is_media_item(meta):
        raise NoteError(400, "Not a media note")
    if meta.get("backdrop") and not force:
        return {"path": path, "skipped": "already has a backdrop"}

    media_type = str(meta.get(media_items.MEDIA_KEY))
    title = str(meta.get("title") or Path(path).stem)

    cover_block = None
    cover_url = str(meta.get("cover") or "")
    if cover_url.startswith("/api/attachments/"):
        rel = cover_url.removeprefix("/api/")
        try:
            loaded = await asyncio.to_thread(vision.load_image, state.store, rel)
            cover_block = image_block(loaded["media_type"], loaded["data"])
        except Exception:
            log.debug("cover unreadable for %s", path, exc_info=True)

    provider = _build_media_provider(state)
    keys = media_lookup.load_media_keys(state.settings.data_dir)
    concept = ""
    attribution: dict = {}
    if media_type == "movie" and provider is not None:
        attribution = await media_lookup.movie_attribution(
            str(meta.get("source") or ""), keys
        )
    if media_type == "book":
        queries: list[str] = []
        if provider is not None:
            concept, queries = await _plan_book_backdrop(state, provider, meta, title)
        if not queries:
            queries = [" ".join(filter(None, (title, str(meta.get("creator") or ""))))]
        candidate_urls = await media_lookup.book_backdrop_candidates(queries)
    else:
        candidate_urls = await media_lookup.backdrop_candidates(
            media_type, str(meta.get("source") or ""), keys
        )

    urls = candidate_urls[:MAX_CANDIDATES]
    fetched = await asyncio.gather(*(_fetch_image(url) for url in urls))
    # (url, raw bytes, content type, image block), still in the provider's rank order.
    candidates: list[tuple[str, bytes, str, dict]] = [
        (url, *got) for url, got in zip(urls, fetched) if got
    ]

    if not candidates:
        return {"path": path, "skipped": "no stills found"}

    # No model, or a missing/invalid selection: the provider's best-ranked
    # still is a resilient floor — the page never goes without a scene image
    # when one exists.
    pick = 0
    # The model's attribution pick; applied to the fresh note at write time.
    credit: str | None = None
    if provider is not None:
        # The cover rides along as Image 1 so the model can match the work's
        # look; the stills follow it.
        blocks = ([cover_block] if cover_block else []) + [c[3] for c in candidates]
        offset = 1 if cover_block else 0
        first, last = offset + 1, offset + len(candidates)
        candidate_kind = (
            "background photographs" if media_type == "book" else "backdrop stills"
        )
        note = (
            f"Image 1 is the cover, for reference only. Images {first}–{last} are "
            f"candidate {candidate_kind} for the page header."
            if cover_block
            else f"Images 1–{last} are candidate {candidate_kind} for the page header."
        )
        prompt = ART_DIRECTION_PROMPT.format(
            title=title,
            year_part=f" ({meta['year']})" if meta.get("year") else "",
            type_word=_TYPE_WORDS.get(media_type, media_type),
            creator_part=(
                f" {_CREATOR_LABELS.get(media_type, 'by')} {meta['creator']}"
                if meta.get("creator")
                else ""
            ),
            image_note=note,
            concept_note=f"OMNIA's research brief: {concept}" if concept else "",
        )
        ask_credit = bool(attribution.get("director") and attribution.get("studio"))
        if ask_credit:
            prompt += ATTRIBUTION_ADDENDUM.format(
                director=attribution["director"], studio=attribution["studio"]
            )
        try:
            reply = await provider.chat(
                ART_DIRECTION_SYSTEM,
                [{"role": "user", "content": blocks + [{"type": "text", "text": prompt}]}],
                [],
                None,
            )
            _record_usage(state, reply)
            parsed = _parse_reply(reply.text) or {}
            chosen = parsed.get("backdrop")
            if isinstance(chosen, int):
                index = chosen - offset - 1
                if 0 <= index < len(candidates):
                    pick = index
            if ask_credit:
                picked = str(parsed.get("credit") or "").strip()
                credit = next(
                    (
                        name
                        for name in (attribution["director"], attribution["studio"])
                        if picked.casefold() == name.casefold()
                    ),
                    None,
                )
        except Exception:
            log.warning("Media art direction call failed for %s", path, exc_info=True)

    # The chosen still's bytes are already in memory; store them as they are.
    _url, raw, ctype, _block = candidates[pick]
    try:
        ingested = await media_lookup.store_image_bytes(
            state, raw, ctype, f"{media_items.media_slug(title)}-backdrop", kind="backdrop"
        )
    except NoteError:
        log.warning("backdrop store failed for %s", path, exc_info=True)
        return {"path": path, "skipped": "backdrop store failed"}

    # Everything above awaited for seconds against the snapshot read at the
    # start; a rating, log line, review or hand edit may have landed since.
    # Patch the note as it is NOW — no await between this read and the write.
    try:
        fresh = state.store.read(path)
    except NoteError:
        log.info("media note %s vanished during enrichment", path)
        return {"path": path, "skipped": "note vanished during enrichment"}
    fmeta, fbody = fm.parse(fresh)
    if not media_items.is_media_item(fmeta):
        return {"path": path, "skipped": "no longer a media note"}
    if fmeta.get("backdrop") and not force:
        # A parallel run (or a hand-picked backdrop) got there first.
        return {"path": path, "skipped": "already has a backdrop"}
    fmeta["backdrop"] = ingested["url"]
    # Pages wear one standard look now; retire any palette an older run left.
    fmeta.pop("theme", None)
    # Apply only over the mechanical director fill (or a blank) — a hand-edited
    # creator like "Disney" typed by the user stays, even one typed mid-job.
    if credit and str(fmeta.get("creator") or "") in ("", attribution["director"]):
        fmeta["creator"] = credit
    written = state.store.write(path, fm.serialize(fmeta, fbody))
    await state.indexer.index_note(path, written)
    state.autocommit.touch()
    # No origin: any open view of this note should merge the rewrite.
    state.live.note_changed(path, content_hash(written))
    return {
        "path": path,
        "revision": content_hash(written),
        "backdrop": fmeta.get("backdrop"),
    }


# Fire-and-forget enrichment after an item is created: the page appears
# instantly and the backdrop fills in live. Strong refs keep the tasks alive.
_TASKS: set[asyncio.Task] = set()


def _enrich_gate(state) -> asyncio.Semaphore:
    # Per app state and created on first use: a Semaphore that has once made
    # a caller wait is bound to that event loop.
    gate = getattr(state, "media_enrich_gate", None)
    if gate is None:
        gate = state.media_enrich_gate = asyncio.Semaphore(ENRICH_CONCURRENCY)
    return gate


def enrich_soon(state, path: str) -> None:
    async def run():
        try:
            async with _enrich_gate(state):
                await enrich_item(state, path)
        except Exception:
            log.warning("media enrichment failed for %s", path, exc_info=True)

    task = asyncio.create_task(run())
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


async def shutdown() -> None:
    """Cancel every queued or running Media job; the app calls this before it
    closes the database the jobs write through."""
    loop = asyncio.get_running_loop()
    tasks = [task for task in _TASKS if task.get_loop() is loop]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
