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

from ..vault.internal import atomic_bytes
from ..vault.notes import NoteError, NoteStore, safe_stem

router = APIRouter(prefix="/api/attachments", tags=["attachments"])

MAX_BYTES = 25 * 1024 * 1024

# Served inline under their real type. Anything else downloads.
INLINE_SAFE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".pdf": "application/pdf",
}

# SVGs can carry script, so they are inline only under a sandboxing CSP. Inside
# <img> (the only way a note embeds one) an SVG never runs script anyway; the
# sandbox is what neuters opening the URL directly, which would otherwise be
# same-origin XSS. The desktop app re-serves images as blob: URLs, which drop
# this header, and stays safe on the <img> rule alone.
SVG_MEDIA_TYPE = "image/svg+xml"
SVG_CONTENT_SECURITY_POLICY = "sandbox; default-src 'none'; style-src 'unsafe-inline'"


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


# Lifted to vault.notes so Paper assets are named by the same rule.
_safe_stem = safe_stem


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
    # A short file under the hashed name is a write that was killed part-way;
    # the same bytes arriving again are the only chance to repair it.
    if not abs_path.exists() or abs_path.stat().st_size != len(data):
        atomic_bytes(abs_path, data)
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
    suffix = abs_path.suffix.lower()
    svg = suffix == ".svg"
    media_type = SVG_MEDIA_TYPE if svg else INLINE_SAFE_TYPES.get(suffix)
    hashed = bool(_HASHED_NAME.search(abs_path.name))
    headers = {
        # Content-hashed names never change meaning, so cache hard.
        "Cache-Control": IMMUTABLE_CACHE_CONTROL if hashed else PRIVATE_CACHE_CONTROL,
        "X-Content-Type-Options": "nosniff",
    }
    if svg:
        headers["Content-Security-Policy"] = SVG_CONTENT_SECURITY_POLICY
    if media_type:
        return FileResponse(
            abs_path,
            media_type=media_type,
            headers={**headers, "Content-Disposition": "inline"},
        )
    # Anything not known-inline-safe (e.g. DOCX) is forced to download rather
    # than render. Starlette writes the name itself, as RFC 5987 `filename*`
    # whenever it needs escaping: header values go out as latin-1, so a
    # hand-placed "日本.docx" was a 500 and a '"' in a name broke the header.
    return FileResponse(
        abs_path,
        media_type="application/octet-stream",
        filename=abs_path.name,
        content_disposition_type="attachment",
        headers=headers,
    )
