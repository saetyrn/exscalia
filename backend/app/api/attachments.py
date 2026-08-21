"""Binary attachments (images, PDFs) stored inside the vault.

Attachments are versioned and backed up exactly like notes, but they never
appear in the note or folder listings. Filenames are content-hashed so the same
image pasted twice costs one blob and the URL is safe to cache forever.
"""

import hashlib
import re
from pathlib import Path

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import FileResponse

from ..vault.notes import NoteError, NoteStore

router = APIRouter(prefix="/api/attachments", tags=["attachments"])

MAX_BYTES = 25 * 1024 * 1024

# SVGs can carry script; serving them inline would be same-origin XSS.
INLINE_SAFE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".pdf": "application/pdf",
}


# Every writer — the upload route, the cover downloader, OMNIA's vision tool —
# names files "<stem>-<16 hex of sha256><ext>". The digest is of the bytes, so a
# URL and its content are welded together: that response can be cached by
# anything, forever — but the vault is auth-protected personal content, so the
# grant stays `private`: the browser caches hard (which is all the poster
# warming relies on) and shared intermediaries stay out. Names that don't
# match (a file dropped into the vault by hand) can be rewritten with new
# bytes, so they must revalidate every time.
_HASHED_NAME = re.compile(r"-[0-9a-f]{16}\.[A-Za-z0-9]+$")

IMMUTABLE_CACHE_CONTROL = "private, max-age=31536000, immutable"
PRIVATE_CACHE_CONTROL = "private, no-cache"


def _safe_stem(name: str) -> str:
    """Keep a readable hint of the original filename without trusting it."""
    stem = Path(name or "").stem
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-._")
    return (cleaned[:60] or "file").lower()


@router.post("")
async def upload_attachment(request: Request, file: UploadFile = File(...)):
    state = request.app.state
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in NoteStore.ATTACHMENT_EXTENSIONS:
        raise NoteError(400, f"Unsupported attachment type: {suffix or 'none'}")

    data = await file.read(MAX_BYTES + 1)
    if not data:
        raise NoteError(400, "Empty file")
    if len(data) > MAX_BYTES:
        raise NoteError(413, "Attachment is larger than 25 MB")

    digest = hashlib.sha256(data).hexdigest()[:16]
    name = f"{_safe_stem(file.filename)}-{digest}{suffix}"
    rel_path = f"{NoteStore.ATTACHMENT_DIR}/{name}"
    abs_path = state.store.resolve_attachment(rel_path)
    if not abs_path.exists():
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_bytes(data)
        state.autocommit.touch()
    # `url` is what goes into the note's markdown — it must be the API route,
    # not the vault path, or the SPA catch-all would swallow the request.
    return {"path": rel_path, "size": len(data), "url": f"/api/attachments/{name}"}


@router.delete("/{path:path}")
async def delete_attachment(request: Request, path: str):
    """Remove an attachment file from the working tree.

    Unlike notes there is no trash step — the blob stays recoverable from git
    history (and was pushed off-site by the backup), but nothing in the
    working tree or future commits carries it."""
    state = request.app.state
    rel_path = path if path.startswith(NoteStore.ATTACHMENT_DIR) else (
        f"{NoteStore.ATTACHMENT_DIR}/{path}"
    )
    abs_path = state.store.resolve_attachment(rel_path)
    if not abs_path.is_file():
        raise NoteError(404, f"Attachment not found: {rel_path}")
    abs_path.unlink()
    state.autocommit.touch()
    return {"ok": True, "path": rel_path}


@router.get("/{path:path}")
async def read_attachment(request: Request, path: str):
    # The prefix is part of the stored path; accept it with or without.
    rel_path = path if path.startswith(NoteStore.ATTACHMENT_DIR) else (
        f"{NoteStore.ATTACHMENT_DIR}/{path}"
    )
    abs_path = request.app.state.store.resolve_attachment(rel_path)
    if not abs_path.is_file():
        raise NoteError(404, f"Attachment not found: {rel_path}")
    media_type = INLINE_SAFE_TYPES.get(abs_path.suffix.lower())
    hashed = bool(_HASHED_NAME.search(abs_path.name))
    return FileResponse(
        abs_path,
        # Content-hashed names never change meaning, so cache hard. Anything not
        # known-inline-safe (e.g. SVG) is forced to download rather than render.
        media_type=media_type or "application/octet-stream",
        headers={
            "Cache-Control": (
                IMMUTABLE_CACHE_CONTROL if hashed else PRIVATE_CACHE_CONTROL
            ),
            "Content-Disposition": (
                "inline" if media_type else f'attachment; filename="{abs_path.name}"'
            ),
            "X-Content-Type-Options": "nosniff",
        },
    )
