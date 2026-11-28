"""Music: resumable uploads from the browser, in raw 8 MB pieces.

Cloudflare refuses a request body over 100 MB and drops a request that takes
longer than its timeouts, so a song goes up in pieces small enough to finish
quickly even from a phone (MUSIC.md section 3). Each piece is the raw bytes
(``application/octet-stream``), read straight from the request stream into a
file under /data: never multipart, which Starlette would spool to /tmp, and
/tmp in the container is memory.

An upload is opened with its name, size and destination, filled piece by
piece at the offset the server says it has, and finished, which hands the
file to the same import the drop folder uses. A dropped connection resumes
from ``received``. Unfinished uploads older than a day are cleared at boot.
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from pathlib import Path, PurePosixPath

from . import music
from .music import MusicError
from .vault.internal import atomic_json, read_json

MB = 1024 * 1024
PIECE = 8 * MB
MAX_FILE = 500 * MB
STALE_SECONDS = 24 * 60 * 60
_LOCKS: dict[str, asyncio.Lock] = {}


def _dir(state) -> Path:
    return Path(state.settings.data_dir) / "music" / "uploads"


def _paths(state, upload_id: str) -> tuple[Path, Path]:
    if not upload_id.isalnum() or len(upload_id) > 40:
        raise MusicError(404, "No such upload")
    base = _dir(state)
    return base / f"{upload_id}.part", base / f"{upload_id}.json"


def _meta(state, upload_id: str) -> dict:
    _part, meta_path = _paths(state, upload_id)
    meta = read_json(meta_path, None, skip_corrupt=True)
    if not isinstance(meta, dict):
        raise MusicError(404, "No such upload")
    return meta


def _clean_parts(parts: list[str] | None) -> list[str]:
    """Folder names from a dropped folder's relative path."""
    out = []
    for part in parts or []:
        text = str(part).replace("\x00", "").strip()
        if not text or text in (".", "..") or "/" in text or "\\" in text:
            raise MusicError(400, "Bad folder name in the upload")
        out.append(text[:120])
    if len(out) > 12:
        raise MusicError(400, "Folders nested too deep")
    return out


def open_upload(state, name: str, size: int, parent: str | None, parts: list[str] | None) -> dict:
    file_name = PurePosixPath(str(name or "")).name.strip()
    suffix = PurePosixPath(file_name).suffix.lower()
    if suffix not in music.AUDIO_TYPES:
        raise MusicError(400, f"Not a song EXSCALIA plays: {file_name or 'no name'}")
    if not isinstance(size, int) or size <= 0:
        raise MusicError(400, "Empty file")
    if size > MAX_FILE:
        raise MusicError(413, "Songs can be up to 500 MB")
    lib = music.library(state)
    if parent is not None and parent not in lib.folders:
        raise MusicError(404, "No such folder")
    upload_id = uuid.uuid4().hex
    part, meta_path = _paths(state, upload_id)
    part.parent.mkdir(parents=True, exist_ok=True)
    part.touch()
    meta = {
        "id": upload_id,
        "name": file_name,
        "size": size,
        "received": 0,
        "parent": parent,
        "parts": _clean_parts(parts),
        "created": time.time(),
    }
    atomic_json(meta_path, meta, fsync=False)
    return {"id": upload_id, "received": 0, "piece": PIECE}


def upload_status(state, upload_id: str) -> dict:
    meta = _meta(state, upload_id)
    return {"id": upload_id, "received": meta["received"], "size": meta["size"], "piece": PIECE}


async def write_piece(state, upload_id: str, offset: int, chunks) -> dict:
    """Append one piece. ``offset`` must be what the server already has, so a
    retried piece after a lost answer is refused with the real position
    instead of being written twice."""
    lock = _LOCKS.setdefault(upload_id, asyncio.Lock())
    async with lock:
        meta = _meta(state, upload_id)
        part, meta_path = _paths(state, upload_id)
        if offset != meta["received"]:
            raise MusicError(409, f"expected offset {meta['received']}")
        written = 0
        with part.open("r+b") as handle:
            handle.seek(offset)
            async for chunk in chunks:
                written += len(chunk)
                if written > PIECE or offset + written > meta["size"]:
                    handle.truncate(offset)
                    raise MusicError(413, "Piece too large")
                await asyncio.to_thread(handle.write, chunk)
            handle.flush()
            os.fsync(handle.fileno())
        meta["received"] = offset + written
        atomic_json(meta_path, meta, fsync=False)
        return {"id": upload_id, "received": meta["received"], "size": meta["size"]}


async def finish(state, upload_id: str) -> dict:
    lock = _LOCKS.setdefault(upload_id, asyncio.Lock())
    async with lock:
        meta = _meta(state, upload_id)
        part, meta_path = _paths(state, upload_id)
        if meta["received"] != meta["size"] or part.stat().st_size != meta["size"]:
            raise MusicError(409, f"only {meta['received']} of {meta['size']} bytes arrived")
        report = await music.import_files(
            state,
            [
                music.ImportItem(
                    source=part,
                    parts=tuple(meta["parts"]),
                    name=meta["name"],
                    remove_source=True,
                    parent=meta["parent"],
                )
            ],
        )
        if not report.failed:
            part.unlink(missing_ok=True)
            meta_path.unlink(missing_ok=True)
            _LOCKS.pop(upload_id, None)
        return report.as_dict()


def cancel(state, upload_id: str) -> None:
    part, meta_path = _paths(state, upload_id)
    part.unlink(missing_ok=True)
    meta_path.unlink(missing_ok=True)
    _LOCKS.pop(upload_id, None)


def clear_stale(state) -> None:
    base = _dir(state)
    if not base.is_dir():
        return
    cutoff = time.time() - STALE_SECONDS
    for path in base.iterdir():
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
        except OSError:
            continue

