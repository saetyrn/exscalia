"""Background work for each picture saved to the Gallery.

Two steps, in order: the picture's CLIP vector (local, `gallery_vectors`),
then its description (the AI, `gallery_ai`, written by
`gallery.store_description`). `gallery.add_picture` queues both, so every way
in (an upload, a link, OMNIA) gets them without knowing; the picture page can
ask for a new description.

One job per picture at a time. Embeddings run one after another (the laptop
serves requests too); descriptions a few side by side. The last failure per
picture is kept, as one short sentence, until the next attempt. A boot pass
(`backfill`) catches up on whatever a restart interrupted. Nothing here may
take the server down: a step that cannot run is skipped, and a step that
fails says so in `status` and the log.
"""
from __future__ import annotations

import asyncio
import logging

from . import gallery, gallery_ai
from .vault.notes import NoteError

log = logging.getLogger(__name__)

DESCRIBE_CONCURRENCY = 2
_TASKS: set[asyncio.Task] = set()

MEDIA_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".gif": "image/gif", ".webp": "image/webp"}


class _Jobs:
    def __init__(self) -> None:
        self.running: dict[str, asyncio.Task] = {}
        self.errors: dict[str, str] = {}
        self.describe_gate = asyncio.Semaphore(DESCRIBE_CONCURRENCY)
        self.embed_gate = asyncio.Semaphore(1)


def _jobs(state) -> _Jobs:
    jobs = getattr(state, "gallery_jobs", None)
    if jobs is None:
        jobs = state.gallery_jobs = _Jobs()
    return jobs


def _stopping(state) -> bool:
    stopping = getattr(state, "shutting_down", None)
    return stopping is not None and stopping.is_set()


def can_describe(state) -> bool:
    try:
        return gallery_ai.available(state)
    except Exception:
        log.warning("could not tell whether gallery descriptions can run", exc_info=True)
        return False


def _image(state, pin: gallery.Pin) -> tuple[bytes, str]:
    name = pin.image[len(gallery.ATTACHMENT_PREFIX):]
    abs_path = state.store.resolve_attachment(f"attachments/{name}")
    suffix = abs_path.suffix.lower()
    return abs_path.read_bytes(), MEDIA_TYPES.get(suffix, "image/jpeg")


def _context(pin: gallery.Pin) -> str:
    lines = []
    if pin.title:
        lines.append(f"Title: {pin.title}")
    if pin.board:
        lines.append(f"Board: {pin.board}")
    if pin.source_url:
        lines.append(f"Saved from: {pin.source_url[:300]}")
    return "\n".join(lines)


async def _embed(state, pin: gallery.Pin) -> None:
    vectors = getattr(state, "gallery_vectors", None)
    if vectors is None or not vectors.available:
        return
    digest = gallery.image_digest(pin.image[len(gallery.ATTACHMENT_PREFIX):])
    if await vectors.has(digest):
        return
    async with _jobs(state).embed_gate:
        data, _type = await asyncio.to_thread(_image, state, pin)
        await vectors.embed_image(digest, data)


async def _describe(state, path: str, force: bool) -> None:
    jobs = _jobs(state)
    async with jobs.describe_gate:
        try:
            pin = await gallery.get_pin(state, path)
        except NoteError:
            return  # deleted or moved while it waited
        if pin.described and not force:
            return
        data, media_type = await asyncio.to_thread(_image, state, pin)
        result = await gallery_ai.describe(state, data, media_type, context=_context(pin))
        await gallery.store_description(state, path, result.model_dump(), force=force)


async def _embed_reading(state, path: str) -> None:
    """The reading's text vector, once there is a reading."""
    vectors = getattr(state, "gallery_vectors", None)
    if vectors is None or not vectors.reads_text:
        return
    try:
        reading = await gallery.get_reading(state, path)
    except NoteError:
        return
    text = gallery.meaning_text(reading)
    if text:
        await vectors.embed_text(gallery.meaning_key(text), text)


async def _process(state, path: str, describe: bool, force: bool) -> None:
    jobs = _jobs(state)
    try:
        try:
            pin = await gallery.get_pin(state, path)
        except NoteError:
            return
        await _embed(state, pin)
        if describe and (force or not pin.described) and can_describe(state):
            await _describe(state, path, force)
        await _embed_reading(state, path)
    except gallery_ai.GalleryAIError as exc:
        jobs.errors[path] = str(exc)
        log.warning("gallery description for %s failed: %s", path, exc)
    except asyncio.CancelledError:
        raise
    except Exception:
        jobs.errors[path] = gallery_ai.FAILED
        log.warning("gallery work for %s failed", path, exc_info=True)


def process_soon(state, path: str, *, describe: bool = True, force: bool = False) -> bool:
    """Start one picture's work in the background; True when work is now in
    progress for it. A second call while one runs starts nothing more."""
    if _stopping(state):
        return False
    jobs = _jobs(state)
    if path in jobs.running:
        return True
    jobs.errors.pop(path, None)
    task = asyncio.create_task(_process(state, path, describe, force))
    jobs.running[path] = task

    def done(finished: asyncio.Task) -> None:
        if jobs.running.get(path) is finished:
            del jobs.running[path]
        _TASKS.discard(finished)

    _TASKS.add(task)
    task.add_done_callback(done)
    return True


def status(state, path: str) -> dict:
    jobs = _jobs(state)
    return {"working": path in jobs.running, "error": jobs.errors.get(path)}


def summary(state) -> dict:
    jobs = _jobs(state)
    vectors = getattr(state, "gallery_vectors", None)
    return {
        "working": len(jobs.running),
        "describer": can_describe(state),
        "visual": bool(vectors is not None and vectors.available),
        "visual_error": vectors.error if vectors is not None else None,
    }


async def backfill(state) -> int:
    """Embed and describe every picture that is missing either, one after
    another. Returns how many pictures were visited."""
    rows = await state.db.execute_fetchall(
        "SELECT path, digest, meaning, described FROM gallery ORDER BY created DESC"
    )
    vectors = getattr(state, "gallery_vectors", None)
    known = await vectors.known() if vectors is not None and vectors.available else None
    texts = await vectors.known_texts() if vectors is not None and vectors.reads_text else None
    describing = can_describe(state)
    visited = 0
    for row in rows:
        if _stopping(state):
            break
        needs_vector = known is not None and row["digest"] not in known
        needs_text = describing and not row["described"]
        needs_meaning = texts is not None and bool(row["meaning"]) and row["meaning"] not in texts
        if not (needs_vector or needs_text or needs_meaning):
            continue
        if row["path"] in _jobs(state).running:
            continue
        await _process(state, row["path"], describe=needs_text, force=False)
        visited += 1
    if visited:
        log.info("gallery backfill visited %d picture(s)", visited)
    return visited


async def shutdown() -> None:
    loop = asyncio.get_running_loop()
    tasks = [task for task in _TASKS if task.get_loop() is loop]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
