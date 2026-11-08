"""Gallery API: boards, the picture grid, one picture, related pictures,
search, uploads, links, thumbnails and descriptions.

Thin by design: every write goes through the service layer in
``app/gallery.py``, which the OMNIA tools share, so a picture saved from chat
and one dropped on a board take one path to the note, the index, the LiveBus
and the background describer.
"""
from __future__ import annotations

import asyncio
import io
import logging
from pathlib import Path, PurePosixPath

import httpx
from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import gallery, gallery_jobs, gallery_search, media_lookup, recipe_import
from ..recipe_import import RecipeImportError
from ..vault.internal import atomic_bytes
from ..vault.notes import NoteError

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/gallery", tags=["gallery"])

MAX_UPLOAD = 25 * 1024 * 1024
# Thumbnails: wide enough for a grid column on a 2x screen, as WebP.
THUMB_WIDTH = 480
THUMB_MAX_HEIGHT = THUMB_WIDTH * 4
THUMB_DIR = "gallery-thumbs"
IMMUTABLE = "private, max-age=31536000, immutable"
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def _origin(request: Request, client_id: str | None = None) -> str | None:
    return client_id or request.headers.get("x-client-id")


def _with_status(state, pin: gallery.Pin) -> dict:
    return {**pin.model_dump(), "status": gallery_jobs.status(state, pin.path)}


# NOTE: literal routes stay above the /item/{path:path} family.


@router.get("")
async def overview(request: Request):
    state = request.app.state
    newest, total = await gallery.list_pins(state, None, limit=3)
    return {
        "boards": [b.model_dump() for b in await gallery.list_boards(state)],
        "total": total,
        # The "All pictures" card's collage.
        "covers": [card.thumb for card in newest],
        "status": gallery_jobs.summary(state),
    }


@router.get("/pins")
async def pins(
    request: Request,
    board: str | None = None,
    q: str = "",
    offset: int = 0,
    limit: int = 60,
):
    """The grid. `board` absent is every picture; `q` searches instead of
    listing (best first, one page)."""
    state = request.app.state
    if q.strip():
        found = await gallery_search.search(state, q, board=board)
        return {"pins": [c.model_dump() for c in found], "total": len(found), "next": None}
    cards, total = await gallery.list_pins(state, board, offset=offset, limit=limit)
    after = offset + len(cards)
    return {
        "pins": [c.model_dump() for c in cards],
        "total": total,
        "next": after if after < total else None,
    }


class BoardBody(BaseModel):
    name: str


class RenameBoardBody(BaseModel):
    name: str
    new_name: str
    client_id: str | None = None


@router.post("/boards", status_code=201)
async def create_board(request: Request, body: BoardBody):
    return {"name": await gallery.create_board(request.app.state, body.name)}


@router.post("/boards/rename")
async def rename_board(request: Request, body: RenameBoardBody):
    name = await gallery.rename_board(
        request.app.state, body.name, body.new_name, origin=_origin(request, body.client_id)
    )
    return {"name": name}


@router.post("/boards/delete")
async def delete_board(request: Request, body: BoardBody):
    deleted = await gallery.delete_board(request.app.state, body.name, origin=_origin(request))
    return {"ok": True, "deleted": deleted}


@router.post("/upload", status_code=201)
async def upload(
    request: Request,
    file: UploadFile = File(...),
    board: str = Form(default=""),
    title: str = Form(default=""),
    client_id: str | None = Form(default=None),
):
    """One picture per request: a drop of twenty sends twenty, so no single
    body nears the site-wide cap."""
    state = request.app.state
    data = await file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise NoteError(413, "A picture is at most 25 MB")
    hint = Path(file.filename or "").stem if file.filename not in (None, "blob") else ""
    pin, created = await gallery.add_picture(
        state,
        data,
        board=board,
        title=title,
        name_hint=hint,
        origin=_origin(request, client_id),
    )
    return {"pin": _with_status(state, pin), "created": created}


class LinkBody(BaseModel):
    url: str
    board: str = ""
    title: str = ""
    client_id: str | None = None


async def picture_from_link(state, url: str) -> tuple[bytes, str, str | None]:
    """(image bytes, source link, page title) for a link to an image or to a
    page that names one (og:image). Raises NoteError."""
    raw = (url or "").strip()
    if not raw:
        raise NoteError(400, "Paste a link")
    if "://" not in raw:
        raw = "https://" + raw
    try:
        suffix = PurePosixPath(httpx.URL(raw).path).suffix.lower()
    except (httpx.InvalidURL, UnicodeError):
        raise NoteError(400, "Not a valid link") from None
    page = None
    if suffix not in IMAGE_SUFFIXES:
        try:
            page = await recipe_import.fetch(raw)
        except RecipeImportError as exc:
            # 415: the link is not a page; it may well be an image.
            if exc.status != 415:
                raise NoteError(exc.status, exc.detail) from None
    if page is not None:
        if not page.image:
            raise NoteError(422, "That page names no picture. Copy the image address instead.")
        image_url, source, title = page.image, page.url, page.og_title or page.title
    else:
        image_url, source, title = raw, raw, None
    try:
        data, _ctype = await media_lookup.fetch_image_bytes(image_url)
    except NoteError as exc:
        detail = exc.detail.replace("Cover download", "The download").replace("Cover image", "The picture")
        raise NoteError(exc.status, detail) from None
    return data, source, title


@router.post("/link", status_code=201)
async def from_link(request: Request, body: LinkBody):
    state = request.app.state
    data, source, page_title = await picture_from_link(state, body.url)
    pin, created = await gallery.add_picture(
        state,
        data,
        board=body.board,
        title=body.title or (page_title or "")[: gallery.MAX_TITLE],
        source_url=source,
        name_hint=page_title or "picture",
        origin=_origin(request, body.client_id),
    )
    return {"pin": _with_status(state, pin), "created": created}


def _thumb_dir(state) -> Path:
    index_dir = getattr(state.settings, "index_dir", None)
    return Path(index_dir or Path(state.settings.data_dir) / "index") / THUMB_DIR


def _make_thumb(source: Path, target: Path) -> bool:
    try:
        from PIL import Image

        with Image.open(source) as image:
            if image.size[0] * image.size[1] > media_lookup.MAX_ART_PIXELS:
                return False
            image.draft("RGB", (THUMB_WIDTH, THUMB_MAX_HEIGHT))
            mode = "RGBA" if image.mode in ("RGBA", "LA", "P") else "RGB"
            small = image.convert(mode)
        if small.width > THUMB_WIDTH or small.height > THUMB_MAX_HEIGHT:
            small.thumbnail((THUMB_WIDTH, THUMB_MAX_HEIGHT))
        buffer = io.BytesIO()
        small.save(buffer, format="WEBP", quality=80, method=4)
        atomic_bytes(target, buffer.getvalue())
        return True
    except Exception:
        log.warning("no thumbnail for %s", source.name, exc_info=True)
        return False


@router.get("/thumb/{name}")
async def thumb(request: Request, name: str):
    """A grid-sized WebP of a stored picture, made on first ask and kept with
    the index (rebuildable). Falls back to the picture itself."""
    state = request.app.state
    if gallery.image_name(f"{gallery.ATTACHMENT_PREFIX}{name}") is None:
        raise NoteError(404, "Not a picture")
    source = state.store.resolve_attachment(f"attachments/{name}")
    if not source.is_file():
        raise NoteError(404, "Picture not found")
    target = _thumb_dir(state) / f"{name}.webp"
    if not target.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        if not await asyncio.to_thread(_make_thumb, source, target):
            target = source
    media_type = "image/webp" if target.suffix == ".webp" else None
    return FileResponse(
        target,
        media_type=media_type,
        headers={"Cache-Control": IMMUTABLE, "X-Content-Type-Options": "nosniff"},
    )


@router.get("/status")
async def status(request: Request):
    return gallery_jobs.summary(request.app.state)


@router.get("/item/{path:path}")
async def get_item(request: Request, path: str):
    state = request.app.state
    return _with_status(state, await gallery.get_pin(state, path))


class UpdateBody(BaseModel):
    title: str | None = None
    description: str | None = None
    tags: list[str] | None = None
    source_url: str | None = None
    board: str | None = None
    base_revision: str | None = None
    client_id: str | None = None


@router.put("/item/{path:path}")
async def update_item(request: Request, path: str, body: UpdateBody):
    """Fields left out keep their values. `board` moves the picture, after
    the other fields are written; the answer carries its new path."""
    state = request.app.state
    origin = _origin(request, body.client_id)
    pin = await gallery.get_pin(state, path)
    if any(v is not None for v in (body.title, body.description, body.tags, body.source_url)):
        pin = await gallery.update_pin(
            state,
            path,
            title=body.title,
            description=body.description,
            tags=body.tags,
            source_url=body.source_url,
            base_revision=body.base_revision,
            origin=origin,
        )
    elif body.base_revision is not None and body.base_revision != pin.revision:
        raise NoteError(409, "The picture changed elsewhere — reload and retry")
    if body.board is not None:
        pin = await gallery.move_pin(state, path, body.board, origin=origin)
    return _with_status(state, pin)


@router.delete("/item/{path:path}")
async def delete_item(request: Request, path: str):
    await gallery.delete_pin(request.app.state, path, origin=_origin(request))
    return {"ok": True}


@router.get("/related/{path:path}")
async def related(request: Request, path: str, limit: int = 60):
    found = await gallery_search.related(request.app.state, path, limit=max(1, min(limit, 200)))
    return {"pins": [c.model_dump() for c in found]}


@router.post("/describe/{path:path}")
async def describe(request: Request, path: str):
    """Describe the picture again, replacing the description and tags (the
    title stays unless it is empty)."""
    state = request.app.state
    await gallery.get_pin(state, path)
    if not gallery_jobs.can_describe(state):
        raise NoteError(503, "No AI provider is configured.")
    gallery_jobs.process_soon(state, path, describe=True, force=True)
    return gallery_jobs.status(state, path)
