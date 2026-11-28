"""The drop folder: copy music onto the laptop over the home network and it
joins the library.

The first, big import goes this way on purpose: through the browser it would
leave the house and come back through Cloudflare (MUSIC.md section 3).

A file is taken only once its size and modification time have held still
across two scans, so a copy in progress (scp, Finder, rsync's final rename)
is never read half-written. Hidden files are ignored, which also skips
rsync's temporary ``.name.XXXXXX`` files and macOS ``._`` droppings.

Subfolders become library folders, nested the same way; files at the top
land in Unsorted. A ``cover``/``folder``/``front`` image beside the songs
becomes the folder's picked cover. Each source is removed only after its
copy is verified; anything else the folder holds is left alone.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from . import music

log = logging.getLogger(__name__)

SCAN_SECONDS = 60


def inbox_dir(state) -> Path:
    configured = getattr(state.settings, "music_inbox", None)
    return Path(configured) if configured else Path(state.settings.data_dir) / "music-inbox"


class Inbox:
    def __init__(self) -> None:
        self.seen: dict[str, tuple[int, int]] = {}
        # Files already taken that could not be removed (copied in as root
        # into a root-owned folder): not offered again while unchanged.
        self.done: dict[str, tuple[int, int]] = {}
        self.last: dict | None = None
        self.running = asyncio.Lock()


def _inbox(state) -> Inbox:
    inbox = getattr(state, "music_inbox", None)
    if inbox is None:
        inbox = Inbox()
        state.music_inbox = inbox
    return inbox


def _walk(root: Path) -> tuple[list[tuple[Path, tuple[int, int]]], dict[tuple[str, ...], Path]]:
    audio: list[tuple[Path, tuple[int, int]]] = []
    art: dict[tuple[str, ...], Path] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        here = Path(dirpath)
        parts = here.relative_to(root).parts
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            path = here / name
            suffix = path.suffix.lower()
            try:
                stat = path.stat()
            except OSError:
                continue
            if suffix in music.AUDIO_TYPES:
                audio.append((path, (stat.st_size, stat.st_mtime_ns)))
            elif (
                parts
                and suffix in music.IMAGE_TYPES
                and path.stem.casefold() in music.FOLDER_ART_NAMES
                and parts not in art
            ):
                art[parts] = path
    return audio, art


def _prune_empty(root: Path) -> None:
    for dirpath, _dirnames, _filenames in os.walk(root, topdown=False):
        path = Path(dirpath)
        if path == root:
            continue
        leftovers = [p for p in path.iterdir() if p.name != ".DS_Store"]
        if not leftovers:
            for p in path.iterdir():
                p.unlink(missing_ok=True)
            try:
                path.rmdir()
            except OSError:
                pass


async def scan(state) -> dict:
    """One pass over the drop folder. Imports what has been still since the
    last pass and reports what it did."""
    inbox = _inbox(state)
    root = inbox_dir(state)
    if inbox.running.locked():
        return {"busy": True}
    async with inbox.running:
        if not root.is_dir():
            return {"inbox": str(root), "present": False}
        audio, art = await asyncio.to_thread(_walk, root)
        audio = [(p, sig) for p, sig in audio if inbox.done.get(str(p)) != sig]
        seen_now = {str(path): sig for path, sig in audio}
        ready = [path for path, sig in audio if inbox.seen.get(str(path)) == sig]
        inbox.seen = seen_now
        waiting = len(audio) - len(ready)
        report = music.ImportReport()
        if ready:
            items = [
                music.ImportItem(
                    source=path,
                    parts=path.parent.relative_to(root).parts,
                    name=path.name,
                    remove_source=True,
                )
                for path in ready
            ]
            ready_parts = {item.parts for item in items}
            report = await music.import_files(
                state, items, {p: a for p, a in art.items() if p in ready_parts}
            )
            for parts in ready_parts:
                image = art.get(parts)
                # The art has been read into the folder; take it away with
                # the songs so it does not linger alone.
                if image is not None and not any(
                    str(p).startswith(str(image.parent) + os.sep) for p, _ in audio if p not in ready
                ):
                    image.unlink(missing_ok=True)
            await asyncio.to_thread(_prune_empty, root)
            failed = {f["name"] for f in report.failed}
            for path in ready:
                inbox.seen.pop(str(path), None)
                if path.exists() and path.name not in failed:
                    try:
                        stat = path.stat()
                        inbox.done[str(path)] = (stat.st_size, stat.st_mtime_ns)
                    except OSError:
                        pass
                    log.warning("music drop folder: imported %s but cannot remove it (owned by another user)", path.name)
            log.info(
                "music drop folder: %d added, %d already there, %d failed",
                len(report.added), len(report.duplicates), len(report.failed),
            )
        inbox.last = {"present": True, "waiting": waiting, **report.as_dict()}
        return inbox.last


async def ticker(state) -> None:
    root = inbox_dir(state)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        log.warning("music drop folder %s cannot be created", root)
    while True:
        await asyncio.sleep(SCAN_SECONDS)
        if state.shutting_down.is_set():
            return
        try:
            await scan(state)
        except Exception:
            log.exception("music drop folder scan failed")
