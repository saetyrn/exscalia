"""Turn vault attachments into image blocks the models can actually see.

Notes can hold pasted slides and whiteboard photos; without this OMNIA only
ever sees the literal markdown `![alt](/api/attachments/photo-a1b2.png)` and
has no idea whether that is a lecture slide or a cat.
"""

from __future__ import annotations

import base64
import hashlib
import io
from collections import OrderedDict
from pathlib import Path

from ..vault.notes import NoteError, NoteStore

# Anthropic's guidance is ~1568px on the long edge; beyond that costs tokens
# without adding legibility. The same size is comfortable for OpenAI and Gemini.
MAX_EDGE = 1568
# Hard ceiling on what we will hand a provider, after downscaling.
MAX_ENCODED_BYTES = 4 * 1024 * 1024

RASTER_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

DOCUMENT_TYPES = {".pdf": "application/pdf"}
# A slide deck is the realistic case. Beyond this the request itself gets
# unwieldy and the provider starts rejecting it.
MAX_DOCUMENT_BYTES = 8 * 1024 * 1024
MAX_DOCUMENT_PAGES = 50


def is_viewable(rel_path: str) -> bool:
    return Path(rel_path).suffix.lower() in RASTER_TYPES


def is_document(rel_path: str) -> bool:
    return Path(rel_path).suffix.lower() in DOCUMENT_TYPES


def is_attachable(rel_path: str) -> bool:
    """Anything OMNIA can be handed directly: images and PDFs."""
    return is_viewable(rel_path) or is_document(rel_path)


def load_document(store: NoteStore, rel_path: str) -> dict[str, str]:
    """Read a PDF as base64 for providers that accept documents natively."""
    if not is_document(rel_path):
        raise NoteError(400, f"{rel_path} is not a PDF")
    abs_path = store.resolve_attachment(rel_path)
    if not abs_path.is_file():
        raise NoteError(404, f"Attachment not found: {rel_path}")

    # Check the size before reading, not after — a 2 GB file used to be pulled
    # into memory in full just to be rejected on the next line.
    size = abs_path.stat().st_size
    if size > MAX_DOCUMENT_BYTES:
        raise NoteError(
            413,
            f"{rel_path} is {size // (1024 * 1024)} MB — too large to send. "
            f"The limit is {MAX_DOCUMENT_BYTES // (1024 * 1024)} MB.",
        )
    raw = abs_path.read_bytes()
    pages = page_count(raw)
    if pages and pages > MAX_DOCUMENT_PAGES:
        raise NoteError(
            413,
            f"{rel_path} has {pages} pages — the limit is {MAX_DOCUMENT_PAGES}. "
            "Split it, or ask about a specific section.",
        )
    return {
        "media_type": DOCUMENT_TYPES[abs_path.suffix.lower()],
        "data": base64.b64encode(raw).decode("ascii"),
        "name": abs_path.name,
    }


def page_count(raw: bytes) -> int:
    try:
        from pypdf import PdfReader

        return len(PdfReader(io.BytesIO(raw)).pages)
    except Exception:
        return 0


# Extraction is synchronous and blocks the event loop, and the message list is
# rebuilt on every one of up to 15 agent rounds — so the same 50-page PDF was
# parsed 15 times per turn. Hashing the bytes is orders of magnitude cheaper.
_PDF_TEXT_CACHE: OrderedDict[str, str] = OrderedDict()
_PDF_TEXT_CACHE_MAX = 8


def extract_pdf_text(raw: bytes, limit: int = 60_000) -> str:
    """Plain text from a PDF, for providers that cannot take documents.

    Loses layout, figures and handwriting — a real downgrade, so callers say so
    rather than pretending the model saw the document.
    """
    key = f"{hashlib.sha256(raw).hexdigest()}:{limit}"
    if key in _PDF_TEXT_CACHE:
        _PDF_TEXT_CACHE.move_to_end(key)
        return _PDF_TEXT_CACHE[key]
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(raw))
        parts = []
        for page in reader.pages[:MAX_DOCUMENT_PAGES]:
            parts.append(page.extract_text() or "")
            if sum(len(p) for p in parts) > limit:
                break
        text = "\n\n".join(parts).strip()
    except Exception:
        return ""
    result = text[:limit]
    _PDF_TEXT_CACHE[key] = result
    while len(_PDF_TEXT_CACHE) > _PDF_TEXT_CACHE_MAX:
        _PDF_TEXT_CACHE.popitem(last=False)
    return result


def load_image(store: NoteStore, rel_path: str) -> dict[str, str]:
    """Read an attachment and return `{media_type, data}` (base64), downscaled
    so a phone photo doesn't blow the request size."""
    if not is_viewable(rel_path):
        raise NoteError(
            400,
            f"{rel_path} is not a viewable image. Supported: "
            + ", ".join(sorted(RASTER_TYPES)),
        )
    abs_path = store.resolve_attachment(rel_path)
    if not abs_path.is_file():
        raise NoteError(404, f"Attachment not found: {rel_path}")

    raw = abs_path.read_bytes()
    media_type = RASTER_TYPES[abs_path.suffix.lower()]
    data, media_type = _downscale(raw, media_type)
    if len(data) > MAX_ENCODED_BYTES:
        raise NoteError(413, f"{rel_path} is too large to send even after resizing")
    return {"media_type": media_type, "data": base64.b64encode(data).decode("ascii")}


def _downscale(raw: bytes, media_type: str) -> tuple[bytes, str]:
    """Shrink to MAX_EDGE if needed. Returns the original bytes untouched when
    Pillow can't read it — better a large image than none."""
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow is a hard dependency
        return raw, media_type
    try:
        with Image.open(io.BytesIO(raw)) as image:
            if max(image.size) <= MAX_EDGE and len(raw) <= MAX_ENCODED_BYTES:
                return raw, media_type
            image = image.convert("RGB")
            image.thumbnail((MAX_EDGE, MAX_EDGE))
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=85, optimize=True)
            return buffer.getvalue(), "image/jpeg"
    except Exception:
        return raw, media_type


def attachments_in(content: str) -> list[str]:
    """Vault-relative attachment paths referenced by a note body."""
    import re

    found: list[str] = []
    for match in re.finditer(r"!?\[[^\]]*\]\(([^)\s]+)\)", content):
        url = match.group(1)
        # Links are written as /api/attachments/<name> by the editor.
        marker = f"/api/attachments/"
        if marker in url:
            name = url.split(marker, 1)[1]
            rel = f"{NoteStore.ATTACHMENT_DIR}/{name}"
            if rel not in found:
                found.append(rel)
    return found
