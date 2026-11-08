"""/api/paper: the contract the iPad app is built against.

Everything here is a thin shell over :mod:`app.paper_store`. The routes own the
things the store deliberately does not: reading multipart parts under a size
cap, deciding what a browser may cache, nudging the autocommitter, telling the
index, and broadcasting the change so the other device sees it.

Route order matters. Each literal family (`changes`, `search`, `trash`,
`folders`, `notebooks`, `pages`, `blob`, `history`, `assets`) owns its own
prefix so a `{notebook:path}` or `{folder:path}` can never swallow a sibling
route, and inside the notebook and folder families the POST sub-routes are
declared before the bare trio — the same footgun `api/media.py` and
`api/notes.py` carry a comment about.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
from urllib.parse import quote

from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from ..paper_store import (
    MAX_ASSET_BYTES,
    MAX_DRAWING_BYTES,
    MAX_RENDER_BYTES,
    MAX_TEXT_BYTES,
    PaperConflict,
    document_fields,
    file_etag,
    paper_page_url,
)
from ..overseer.vision import encode_image
from ..paper_folders import FOLDER_TRASH_KIND
from ..vault.internal import read_json
from ..vault.notes import NoteError

router = APIRouter(prefix="/api/paper", tags=["paper"])

IMMUTABLE_CACHE_CONTROL = "private, max-age=31536000, immutable"
PRIVATE_CACHE_CONTROL = "private, no-cache"

BLOB_MEDIA_TYPES = {
    "drawing.pkdrawing": "application/octet-stream",
    "render.png": "image/png",
    "text.json": "application/json",
}

# The quiet a Paper change asks of the autocommitter before a commit. The iPad
# saves a second after the last stroke, so the default debounce committed a
# fresh drawing.pkdrawing and render.png at every pause in writing; this
# leaves the autocommitter's max_wait to commit ink every couple of minutes.
PAPER_COMMIT_DELAY = 300.0
# Exactly paper_store.ASSET_EXTENSIONS. store_asset already refuses anything
# else; serving through this map means a file placed in assets/ by hand (an
# .svg, say) is never handed to a browser under a type guessed from its name.
ASSET_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
    ".heic": "image/heic",
}

# Lasso selections are already cropped by the native client. Keep the upload
# bounded and ephemeral: the bytes are base64 encoded into this one OMNIA turn
# and are never written into the vault or the source drawing.
MAX_SELECTION_IMAGE_BYTES = 12 * 1024 * 1024
SELECTION_ACTIONS = {
    "ask": "ask",
    "ask_omnia": "ask",
    "transcribe": "transcribe",
    "note": "note",
    "create_note": "note",
    "task": "task",
    "create_task": "task",
    "flashcards": "flashcards",
    "create_flashcards": "flashcards",
}


# -- glue --------------------------------------------------------------------


def _store(request: Request):
    store = getattr(request.app.state, "paper", None)
    if store is None:  # pragma: no cover - only if the router is mounted alone
        raise NoteError(503, "Paper is not available")
    return store


async def _index_page(state, page_dir: str) -> None:
    """Index hooks are optional until B2 builds ``state.paper_index``."""
    index = getattr(state, "paper_index", None)
    if index is not None:
        await index.index_page(page_dir)


async def _index_notebook(state, notebook: str) -> None:
    index = getattr(state, "paper_index", None)
    if index is not None:
        await index.index_notebook(notebook)


async def _reindex_notebook(state, notebook: str) -> None:
    """A notebook that arrived at a path with no rows under it (a rename, a
    Trash restore) needs its pages indexed too, not only its manifest."""
    index = getattr(state, "paper_index", None)
    if index is None:
        return
    reindex = getattr(index, "reindex_notebook", None)
    if reindex is not None:
        await reindex(notebook)
    else:
        await index.index_notebook(notebook)


async def _index_call(state, name: str, *args) -> None:
    """A folder hook on the index, when the index has one (test doubles and
    an index mid-boot may not)."""
    index = getattr(state, "paper_index", None)
    fn = getattr(index, name, None) if index is not None else None
    if fn is not None:
        await fn(*args)


async def _unindex(state, path: str, kind: str) -> None:
    index = getattr(state, "paper_index", None)
    if index is None:
        return
    if kind == "notebook":
        await index.remove_notebook(path)
    else:
        await index.remove_page(path)


def _touch(state) -> None:
    state.autocommit.touch(min_delay=PAPER_COMMIT_DELAY)


async def restore_paper_item(state, item_id: str) -> dict:
    """Put a trashed notebook or page back with everything that rides beside
    its files: the notebook's page list and tombstones (the store), the
    handwriting index, and a live event for the other device. The generic
    Trash route and OMNIA's restore_trash come through here too, so a restore
    reads the same wherever it was asked for."""
    store = getattr(state, "paper", None)
    if store is None:  # pragma: no cover - only if Paper is not mounted
        raise NoteError(503, "Paper is not available")
    # File moves and small JSON writes: on the loop, like every other write.
    async with _PaperWrite(state) as write:
        async with write.store():
            restored = store.restore_trash(item_id)
        _touch(state)
        if restored["kind"] == "notebook":
            await _reindex_notebook(state, restored["path"])
            await _index_call(state, "index_folder_tree", restored["path"])
        elif restored["kind"] == FOLDER_TRASH_KIND:
            await _index_call(state, "reindex_folder_tree", restored["path"])
        else:
            await _index_page(state, restored["path"])
    state.live.paper_changed(restored["path"], 0)
    return restored


def _write_lock(state):
    """Held around every synchronous PaperStore write.

    The autocommitter stages the whole vault from a worker thread under this
    lock, so holding it means a commit never records half a page. Store calls
    stay on the event loop, where no two of them interleave. Only the store
    call goes inside: never await index work while holding it.
    """
    return state.vault._git_lock


class _PaperWrite:
    """The two locks around a write that takes a feed seq, hand over hand.

    The git lock (:func:`_write_lock`) first, then the index's feed lock
    while holding it; the git lock goes as soon as the store call is done,
    the feed lock once the index hooks have run. The store takes the change's
    seq synchronously and the index learns of it only after awaited queries;
    ``PaperIndex.changes_since`` takes the feed lock, so the feed never hands
    out a cursor past a row it has not seen.

    In this order because the git lock can be held for minutes (a commit of
    the whole vault, ``git gc --auto``): a write that waited for it while
    holding the feed lock would hang every ``/changes`` read behind git work
    the read never needed. Nothing holding the feed lock waits for git.

    ``async with _PaperWrite(state) as write`` takes the git lock;
    ``async with write.store()`` takes the feed lock around the store call and
    releases the git lock after it. Test doubles for the index carry no feed
    lock, and without an index the walker has nothing to wait for.
    """

    def __init__(self, state):
        self._git = _write_lock(state)
        self._feed = getattr(getattr(state, "paper_index", None), "feed_lock", None)
        self._git_held = False
        self._feed_held = False

    async def __aenter__(self) -> "_PaperWrite":
        await self._git.acquire()
        self._git_held = True
        return self

    @contextlib.asynccontextmanager
    async def store(self):
        if self._feed is not None:
            await self._feed.acquire()
            self._feed_held = True
        try:
            yield
        finally:
            self._release_git()

    def _release_git(self) -> None:
        if self._git_held:
            self._git_held = False
            self._git.release()

    async def __aexit__(self, *exc_info) -> None:
        self._release_git()
        if self._feed_held:
            self._feed_held = False
            self._feed.release()


async def _read_part(part: UploadFile | None, cap: int, what: str) -> bytes | None:
    """Read a multipart part, refusing anything over the cap.

    ``cap + 1`` and a length check rather than a stream count: the same idiom
    `api/attachments.py` uses. It is the inner guard, per part, behind
    ``app/body_limit.py`` and Caddy's ``request_body max_size``, which refuse
    an oversized request as a whole.
    """
    if part is None:
        return None
    data = await part.read(cap + 1)
    if len(data) > cap:
        raise NoteError(413, f"Page {what} is larger than {cap // (1024 * 1024)} MB")
    return data


async def _form_value(request: Request, name: str, cap: int) -> bytes | None:
    """One multipart value by name, whether it came as a field or a part.
    The form is the one FastAPI already parsed for the declared parameters."""
    value = (await request.form()).get(name)
    if value is None:
        return None
    if isinstance(value, str):
        data = value.encode("utf-8")
        if len(data) > cap:
            raise NoteError(413, f"The {name} field is too large")
        return data
    return await _read_part(value, cap, name)


def _json_part(data: bytes | None, what: str) -> dict | None:
    if data is None:
        return None
    if not data.strip():
        return None
    try:
        value = json.loads(data.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise NoteError(400, f"The {what} part is not valid JSON") from exc
    if not isinstance(value, dict):
        raise NoteError(400, f"The {what} part must be a JSON object")
    return value


# -- change feed -------------------------------------------------------------


def changes_since(store, epoch: str | None = None, since: int = 0) -> dict:
    """Everything that moved after ``since``, by walking the store.

    The truth of the feed is the ``seq`` stamped into each JSON file, so this
    answer is correct without an index at all — it is just O(pages). B2 swaps
    the body for an indexed query while keeping this signature.
    """
    state = store.seq_state()
    current_epoch = state["epoch"]
    seq = int(state.get("seq") or 0)
    pruned = int(state.get("pruned") or 0)
    since = max(0, int(since or 0))
    # A cursor from another epoch, a first sync, or one older than the deletes
    # we still remember: the client cannot be brought up to date incrementally.
    full = bool(epoch and epoch != current_epoch) or since <= 0 or since < pruned

    notebooks: list[dict] = []
    pages: list[dict] = []
    for rel in store.notebook_dirs():
        manifest = read_json(store.dir / rel / "notebook.json", None, skip_corrupt=True)
        if not isinstance(manifest, dict):
            continue
        if full or int(manifest.get("seq") or 0) > since:
            notebooks.append(store.summary(rel, manifest))
        pages_dir = store.dir / rel / "pages"
        if not pages_dir.is_dir():
            continue
        for page_dir in sorted(p for p in pages_dir.iterdir() if p.is_dir()):
            page = read_json(page_dir / "page.json", None, skip_corrupt=True)
            if not isinstance(page, dict):
                continue
            if not (full or int(page.get("seq") or 0) > since):
                continue
            pages.append(
                {
                    "path": f"{rel}/pages/{page_dir.name}",
                    "notebook": rel,
                    "id": page.get("id") or page_dir.name,
                    "revision": int(page.get("revision") or 0),
                    "render_revision": int(page.get("render_revision") or 0),
                    "text_revision": int(page.get("text_revision") or 0),
                    "modified": page.get("modified"),
                    "seq": int(page.get("seq") or 0),
                }
            )
    pages.sort(key=lambda row: row["seq"])
    return {
        "epoch": current_epoch,
        "seq": seq,
        "full": full,
        "notebooks": notebooks,
        "pages": pages,
        "deleted": [] if full else store.tombstones_since(since),
        **store.folder_changes(since, full),
    }


# -- literal families --------------------------------------------------------


@router.get("")
async def library(request: Request):
    return _store(request).list_library()


@router.get("/changes")
async def changes(
    request: Request,
    since: int = Query(default=0, ge=0),
    epoch: str | None = Query(default=None),
):
    """Indexed when the index is up, walked when it is not.

    Both paths read the same ``seq`` numbers and answer identically; the walker
    stays as the fallback so the feed survives an index that is missing or mid
    rebuild.
    """
    index = getattr(request.app.state, "paper_index", None)
    if index is not None:
        return await index.changes_since(epoch, since)
    return changes_since(_store(request), epoch, since)


@router.get("/search")
async def search(
    request: Request,
    q: str = Query(default="", alias="q"),
    notebook: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
):
    """Handwriting search over the recognised text of every page.

    An empty answer explains itself rather than looking like a broken query:
    nothing is transcribed yet, the notebook filter excluded the hits, or the
    words genuinely are not on any page.
    """
    index = getattr(request.app.state, "paper_index", None)
    query = q.strip()
    if index is None:
        return {
            "results": [],
            "query": q,
            "note": "The Paper index is not available yet — try again shortly.",
        }
    if not query:
        return {"results": [], "query": q, "note": "Enter something to search for."}
    results = await index.search(query, limit=limit, notebook=notebook)
    if results:
        return {"results": results, "query": q}
    return {"results": [], "query": q, "note": await _empty_search_note(index, notebook, query)}


async def _empty_search_note(index, notebook: str | None, query: str) -> str:
    """Which of the three ways a handwriting search comes back empty this was."""
    transcribed = await index.count_transcribed()
    if not transcribed:
        pages = await index.count_pages()
        if not pages:
            return "There are no Paper pages yet — notebooks are written on the iPad."
        return (
            f"None of the {pages} Paper page(s) have been transcribed yet, so there is "
            "nothing to search. Handwriting is recognised on the iPad after a page "
            "settles."
        )
    if notebook:
        elsewhere = await index.search(query, limit=1)
        if elsewhere:
            return (
                f"No match inside {notebook}, but this text appears in another "
                "notebook — drop the notebook filter to see it."
            )
        return f"No page in {notebook} mentions that, and none elsewhere either."
    return (
        f"No transcribed page mentions that ({transcribed} page(s) searched). "
        "Try fewer or different words."
    )


def _region_query(value: str | None) -> str | None:
    """Validate a lasso rectangle and return a compact URL-safe form.

    Native clients may send JSON ({x,y,width,height}) or four comma-separated
    page-point values. Coordinates are metadata only; the uploaded image is
    already cropped, so this endpoint never reads or changes ink.
    """
    raw = str(value or "").strip()
    if not raw:
        return None
    parsed = None
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        pass
    if isinstance(parsed, dict):
        values = [parsed.get(key) for key in ("x", "y", "width", "height")]
    else:
        values = [part.strip() for part in raw.split(",")]
    if len(values) != 4:
        raise NoteError(400, "region must contain x,y,width,height")
    try:
        numbers = [float(item) for item in values]
    except (TypeError, ValueError) as exc:
        raise NoteError(400, "region must contain numeric coordinates") from exc
    if not all(math.isfinite(number) for number in numbers):
        raise NoteError(400, "region coordinates must be finite")
    if numbers[2] <= 0 or numbers[3] <= 0:
        raise NoteError(400, "region width and height must be positive")
    return ",".join(f"{number:.4f}".rstrip("0").rstrip(".") for number in numbers)


@router.get("/resolve/{notebook_id}/{page_id}")
@router.get("/id/{notebook_id}/{page_id}")
async def resolve_page_id(request: Request, notebook_id: str, page_id: str):
    """Resolve a durable Paper link after a notebook has moved or been renamed."""
    store = _store(request)
    notebook, manifest, page = store.resolve_page_by_ids(notebook_id, page_id)
    region = _region_query(request.query_params.get("region"))
    return {
        "notebook": notebook,
        "notebook_id": manifest.get("id"),
        "page": store.page_public(notebook, page["id"]),
        "url": paper_page_url(manifest.get("id") or notebook_id, page["id"], region),
        "region": region,
    }


@router.post("/selection-action")
async def selection_action(
    request: Request,
    notebook_id: str = Form(...),
    page_id: str = Form(...),
    action: str = Form(...),
    region: str | None = Form(default=None),
    prompt: str | None = Form(default=None),
    thread_id: str | None = Form(default=None),
    image: UploadFile = File(...),
):
    """Run one OMNIA action against a native lasso crop.

    The image is inline-only and the page is resolved by stable ids. All
    mutations therefore pass through the ordinary OMNIA executor, which keeps
    confirmations, scoped commits, and Undo identical to a web chat turn.
    """
    state = request.app.state
    store = _store(request)
    notebook, manifest, page = store.resolve_page_by_ids(notebook_id, page_id)
    action_name = SELECTION_ACTIONS.get(str(action or "").strip().lower())
    if action_name is None:
        raise NoteError(
            400,
            "action must be ask, transcribe, note, task, or flashcards",
        )
    if not image.content_type or not image.content_type.startswith("image/"):
        raise NoteError(400, "selection image must be an image")
    image_bytes = await image.read(MAX_SELECTION_IMAGE_BYTES + 1)
    if not image_bytes:
        raise NoteError(400, "selection image is empty")
    if len(image_bytes) > MAX_SELECTION_IMAGE_BYTES:
        raise NoteError(413, "selection image is larger than 12 MB")
    region_value = _region_query(region)

    prompts = {
        "ask": "Look at this selected region from the Paper page and answer the user's request.",
        "transcribe": (
            "Transcribe only this selected region from the Paper page. Keep the "
            "writer's wording and use [?] for anything unreadable."
        ),
        "note": (
            "The user explicitly chose Make note for this selected Paper region. "
            "Prepare a normal markdown note and use the ordinary confirmation "
            "policy before saving it."
        ),
        "task": (
            "The user explicitly chose Create task for this selected Paper region. "
            "Prepare the task and use the ordinary confirmation policy before "
            "creating it."
        ),
        "flashcards": (
            "The user explicitly chose Make flashcards for this selected Paper "
            "region. Prepare the cards and use the ordinary confirmation policy "
            "before creating them."
        ),
    }
    user_prompt = str(prompt or "").strip()
    if len(user_prompt) > 8000:
        raise NoteError(400, "prompt is too long")
    durable = paper_page_url(manifest.get("id") or notebook_id, page_id, region_value)
    source = (
        f"Source Paper page: {durable}. "
        "That link uses stable ids and must be retained in any note or task source."
    )
    if region_value:
        source += f" Selected rectangle in page points: {region_value}."
    message = "\n\n".join(
        part for part in (prompts[action_name], source, user_prompt) if part
    ).strip()
    # The iPad names a new action's thread up front, so a turn it stops
    # waiting for still has a thread to open; the first request under a name
    # creates it, and a name that exists joins it.
    thread_id = state.threads.create(title="Paper selection", thread_id=thread_id or None)["id"]
    # Import lazily to avoid api.paper <-> api.overseer import ordering at boot.
    from .overseer import _auth_session_id, _run_thread_chat

    # Decoding and downscaling is CPU work: off the event loop.
    encoded = await asyncio.to_thread(
        encode_image,
        image_bytes,
        image.content_type,
        label=f"Paper page {page_id} selection",
    )
    result = await _run_thread_chat(
        state,
        thread_id,
        message,
        None,
        auth_session_id=_auth_session_id(request),
        inline_images=[encoded],
        ui_context={
            "view": (
                f"Paper · {manifest.get('title') or notebook} · page "
                f"{page_id} ({notebook}/pages/{page_id})"
            )
        },
    )
    confirmations = result.get("confirmations") or []
    actions = result.get("actions") or []
    return {
        "action": action_name,
        "thread_id": thread_id,
        "navigate": f"/omnia?t={quote(thread_id, safe='')}",
        "page": {
            "notebook_id": manifest.get("id"),
            "page_id": page_id,
            "notebook": notebook,
            "url": durable,
        },
        "region": region_value,
        "reply": result.get("reply") or "",
        "message": result.get("assistant_message"),
        "confirmations": confirmations,
        "confirmation": confirmations[0] if confirmations else None,
        "actions": actions,
        "action_record": actions[0] if len(actions) == 1 else None,
        "pending_count": result.get("pending_count", 0),
        "error": result.get("error"),
    }


@router.get("/trash")
async def list_trash(request: Request):
    store = _store(request)
    items = [
        item
        for item in request.app.state.trash.list()
        if item.get("kind") in store.trash.PAPER_KINDS
    ]
    return {"items": items}


@router.post("/trash/{item_id}/restore")
async def restore_trash(request: Request, item_id: str):
    return await restore_paper_item(request.app.state, item_id)


# -- folders -----------------------------------------------------------------


class NewFolder(BaseModel):
    parent: str | None = None
    name: str = ""
    id: str | None = None
    color: str | None = None
    icon: str | None = None
    favorite: bool = False
    client_id: str | None = None


class RenameFolder(BaseModel):
    new_path: str
    # The verbatim name when this is a rename; a move sends none and the
    # folder keeps its own.
    name: str | None = None
    client_id: str | None = None


class PatchFolder(BaseModel):
    color: str | None = None
    # "" (or null) clears it.
    icon: str | None = None
    favorite: bool | None = None
    client_id: str | None = None


@router.get("/folders")
async def list_folders(request: Request):
    return {"folders": _store(request).folder_records()}


@router.post("/folders")
async def create_folder(request: Request, body: NewFolder):
    state = request.app.state
    async with _PaperWrite(state) as write:
        async with write.store():
            record = _store(request).create_folder(
                parent=body.parent,
                name=body.name,
                folder_id=body.id,
                color=body.color,
                icon=body.icon,
                favorite=body.favorite,
            )
        _touch(state)
        await _index_call(state, "index_folder_tree", record["path"])
    state.live.paper_changed(record["path"], 0, origin=body.client_id)
    return record


# NOTE: above the bare /folders/{folder:path} routes, which would otherwise
# take "…/rename" as part of the folder.
@router.post("/folders/{folder:path}/rename")
async def rename_folder(request: Request, folder: str, body: RenameFolder):
    state = request.app.state
    async with _PaperWrite(state) as write:
        async with write.store():
            result = _store(request).rename_folder(folder, body.new_path, body.name)
        _touch(state)
        if result["path"] != result["from"]:
            await _index_call(state, "move_prefix", result["from"], result["path"])
        await _index_call(state, "index_folder_tree", result["path"])
    state.live.paper_changed(result["path"], 0, origin=body.client_id)
    return result


@router.patch("/folders/{folder:path}")
async def patch_folder(request: Request, folder: str, body: PatchFolder):
    state = request.app.state
    patch = body.model_dump(exclude_unset=True)
    patch.pop("client_id", None)
    async with _PaperWrite(state) as write:
        async with write.store():
            record = _store(request).patch_folder(folder, patch)
        _touch(state)
        await _index_call(state, "index_folders", [record["path"]])
    state.live.paper_changed(record["path"], 0, origin=body.client_id)
    return record


@router.delete("/folders/{folder:path}")
async def delete_folder(request: Request, folder: str):
    state = request.app.state
    async with _PaperWrite(state) as write:
        async with write.store():
            item = _store(request).delete_folder(folder)
        _touch(state)
        await _index_call(state, "remove_folder_tree", item["original_path"])
    state.live.paper_changed(item["original_path"], 0)
    return item


# -- notebooks ---------------------------------------------------------------


class NewNotebook(BaseModel):
    title: str
    folder: str | None = None
    template: str | None = None
    paperSize: str | None = None
    id: str | None = None
    client_id: str | None = None
    # `notebook` or `whiteboard`; a whiteboard's paperSize is always `board`.
    kind: str | None = None
    # For the pages it makes next: `portrait`/`landscape`, a paper colour.
    orientation: str | None = None
    paperColor: str | None = None


class RenameNotebook(BaseModel):
    new_path: str
    # The verbatim title when this is a retitle; a move sends none and the
    # notebook keeps its own.
    title: str | None = None
    client_id: str | None = None


class NewPage(BaseModel):
    id: str | None = None
    after: str | None = None
    template: str | None = None
    size: str | dict | None = None
    size_name: str | None = None
    title: str | None = None
    bookmarked: bool = False
    client_id: str | None = None


class PatchNotebook(BaseModel):
    title: str | None = None
    # Accepted from older clients and ignored: the cover is the first page.
    cover: str | None = None
    tags: list[str] | None = None
    favorite: bool | None = None
    template: str | None = None
    paperSize: str | None = None
    pages: list[str] | None = None
    client_id: str | None = None
    kind: str | None = None
    orientation: str | None = None
    paperColor: str | None = None


@router.post("/notebooks")
async def create_notebook(request: Request, body: NewNotebook):
    state = request.app.state
    async with _PaperWrite(state) as write:
        async with write.store():
            manifest = _store(request).create_notebook(
                title=body.title,
                folder=body.folder,
                template=body.template,
                paper_size=body.paperSize,
                notebook_id=body.id,
                kind=body.kind,
                orientation=body.orientation,
                paper_color=body.paperColor,
            )
        _touch(state)
        await _index_notebook(state, manifest["path"])
        await _index_call(state, "index_folder_tree", manifest["path"])
    state.live.paper_changed(manifest["path"], 0, origin=body.client_id)
    return manifest


# NOTE: these two must stay above the bare /notebooks/{notebook:path} routes —
# the path converter would otherwise take "…/rename" as part of the notebook.
@router.post("/notebooks/{notebook:path}/rename")
async def rename_notebook(request: Request, notebook: str, body: RenameNotebook):
    state = request.app.state
    store = _store(request)
    old_path = store.notebook_path(notebook)
    async with _PaperWrite(state) as write:
        async with write.store():
            manifest = store.rename_notebook(notebook, body.new_path, body.title)
        _touch(state)
        await _unindex(state, old_path, "notebook")
        await _reindex_notebook(state, manifest["path"])
        await _index_call(state, "index_folder_tree", manifest["path"])
    state.live.paper_changed(manifest["path"], 0, origin=body.client_id)
    return manifest


@router.post("/notebooks/{notebook:path}/pages")
async def create_page(request: Request, notebook: str, body: NewPage):
    state = request.app.state
    async with _PaperWrite(state) as write:
        async with write.store():
            page = _store(request).create_page(
                notebook,
                page_id=body.id,
                after=body.after,
                template=body.template,
                size=body.size,
                size_name=body.size_name,
                title=body.title,
                bookmarked=body.bookmarked,
                client_id=body.client_id,
            )
        _touch(state)
        await _index_page(state, page["path"])
    state.live.paper_changed(page["path"], page["revision"], origin=body.client_id)
    return _store(request).page_public(notebook, page["id"])


@router.get("/notebooks/{notebook:path}")
async def read_notebook(request: Request, notebook: str):
    """Manifest plus `pages_detail` — every page's public record in `pages[]`
    order — so the web viewer paints a whole notebook from one request.
    `kind`, `orientation` and `paperColor` are always present, defaulted, as
    in the library summary."""
    store = _store(request)
    manifest = store.read_notebook(notebook)
    detail = []
    for page_id in manifest.get("pages") or []:
        try:
            detail.append(store.page_public(manifest["path"], page_id))
        except NoteError:
            continue  # a page dir missing on disk is not the reader's problem
    return {**manifest, **document_fields(manifest), "pages_detail": detail}


@router.patch("/notebooks/{notebook:path}")
async def patch_notebook(request: Request, notebook: str, body: PatchNotebook):
    state = request.app.state
    patch = body.model_dump(exclude_unset=True, exclude_none=True)
    patch.pop("client_id", None)
    async with _PaperWrite(state) as write:
        async with write.store():
            manifest = _store(request).update_notebook(notebook, patch)
        _touch(state)
        await _index_notebook(state, manifest["path"])
    state.live.paper_changed(manifest["path"], 0, origin=body.client_id)
    return manifest


@router.delete("/notebooks/{notebook:path}")
async def delete_notebook(request: Request, notebook: str):
    state = request.app.state
    store = _store(request)
    path = store.notebook_path(notebook)
    async with _PaperWrite(state) as write:
        async with write.store():
            item = store.delete_notebook(notebook)
        _touch(state)
        await _unindex(state, path, "notebook")
    state.live.paper_changed(path, 0)
    return item


# -- pages -------------------------------------------------------------------


@router.get("/pages/{notebook:path}/{page_id}")
async def read_page(request: Request, notebook: str, page_id: str):
    return _store(request).page_public(notebook, page_id)


@router.put("/pages/{notebook:path}/{page_id}")
async def save_page(
    request: Request,
    notebook: str,
    page_id: str,
    base_revision: int = Form(...),
    for_revision: int | None = Form(default=None),
    client_id: str | None = Form(default=None),
    after: str | None = Form(default=None),
    page: UploadFile | None = File(default=None),
    drawing: UploadFile | None = File(default=None),
    render: UploadFile | None = File(default=None),
    text: UploadFile | None = File(default=None),
):
    """The only write path for ink. Multipart so the source and its derived
    render and transcript arrive in one round trip, and either all of them
    land on the new revision or none of them do.

    ``client_id`` makes a retry safe: the same save sent again after its
    response was lost is answered like the original instead of forking.

    ``render_rect`` is a JSON object ``{x, y, width, height}`` in page points:
    the area the ``render`` part shows (a board's content, not the board). It
    is stored with the render; a render sent without one shows the whole
    page. Read from the parsed form rather than declared, so it is taken as
    a plain field or as a JSON part alike."""
    state = request.app.state
    store = _store(request)
    rect_json = _json_part(
        await _form_value(request, "render_rect", MAX_TEXT_BYTES), "render_rect"
    )
    page_json = _json_part(
        await _read_part(page, MAX_TEXT_BYTES, "metadata"), "page"
    )
    text_json = _json_part(await _read_part(text, MAX_TEXT_BYTES, "text"), "text")
    drawing_bytes = await _read_part(drawing, MAX_DRAWING_BYTES, "drawing")
    render_bytes = await _read_part(render, MAX_RENDER_BYTES, "render")
    async with _PaperWrite(state) as write:
        try:
            async with write.store():
                result = store.save_page(
                    notebook,
                    page_id,
                    base_revision=base_revision,
                    page=page_json,
                    drawing=drawing_bytes,
                    render=render_bytes,
                    text=text_json,
                    for_revision=for_revision,
                    after=after,
                    client_id=client_id,
                    render_rect=rect_json,
                )
        except PaperConflict as exc:
            # Both halves are on disk; the client picks which one to keep looking
            # at. Reported rather than raised so the body can carry them.
            _touch(state)
            sibling = exc.conflict["sibling"]
            await _index_page(state, sibling["path"])
            state.live.paper_changed(
                sibling["path"], sibling["revision"], origin=client_id
            )
            return JSONResponse(
                status_code=409,
                content={"detail": exc.detail, "conflict": exc.conflict},
            )
        _touch(state)
        await _index_page(state, result["page"]["path"])
    state.live.paper_changed(
        result["page"]["path"], result["revision"], origin=client_id
    )
    return {
        "page": store.page_public(notebook, page_id),
        "revision": result["revision"],
        "stale": result["stale"],
    }


@router.delete("/pages/{notebook:path}/{page_id}")
async def delete_page(request: Request, notebook: str, page_id: str):
    state = request.app.state
    store = _store(request)
    path = store.page_dir_path(notebook, page_id)
    async with _PaperWrite(state) as write:
        async with write.store():
            item = store.delete_page(notebook, page_id)
        _touch(state)
        await _unindex(state, path, "page")
        # The manifest lost the page: page_count, cover and seq moved with it.
        await _index_notebook(state, store.notebook_path(notebook))
    state.live.paper_changed(path, 0)
    return item


# -- blobs -------------------------------------------------------------------


def _etag_matches(header: str | None, etag: str) -> bool:
    """If-None-Match against one quoted ETag, by weak comparison (RFC 9110)."""
    for candidate in (header or "").split(","):
        candidate = candidate.strip()
        if candidate == "*" or candidate.removeprefix("W/") == etag:
            return True
    return False


@router.get("/blob/{notebook:path}/{page_id}/{name}")
async def read_blob(
    request: Request,
    notebook: str,
    page_id: str,
    name: str,
    r: int | None = Query(default=None),
    v: str | None = Query(default=None),
):
    """Bytes of a page file.

    Cached forever only when the URL names both the current revision and the
    hash of the bytes (``?r=&v=``, as ``page_public`` writes it): a render or
    transcript is rewritten at an unchanged revision by a correction or an AI
    reading, so the revision alone does not pin what is in the file. Anything
    else revalidates, and a matching If-None-Match costs a 304, not the bytes.

    The ETag carries the revision as well as the hash. The iPad sends the one
    from its last fetch even after replacing that file locally, so bytes that
    come back at a later revision (a history restore) must not match it.
    """
    store = _store(request)
    path, page = store.read_blob(notebook, page_id, name)
    current = store.blob_revision(page, name)
    stored = store.blob_etag(page, name)
    # The drawing and pages written before derived blobs were hashed have no
    # stored hash; only this route pays for one, never a listing.
    digest = stored or await asyncio.to_thread(file_etag, path)
    fresh = r is not None and int(r) == current and v is not None and v == stored
    headers = {
        "Cache-Control": IMMUTABLE_CACHE_CONTROL if fresh else PRIVATE_CACHE_CONTROL,
        "ETag": f'"{current}-{digest}"',
        "X-Content-Type-Options": "nosniff",
    }
    if _etag_matches(request.headers.get("if-none-match"), headers["ETag"]):
        return Response(status_code=304, headers=headers)
    return FileResponse(
        path,
        media_type=BLOB_MEDIA_TYPES.get(name, "application/octet-stream"),
        headers=headers,
    )


# -- history -----------------------------------------------------------------


class RestoreVersion(BaseModel):
    sha: str
    client_id: str | None = None


# Declared before the bare history route so "…/restore" is not read as a page.
@router.post("/history/{notebook:path}/{page_id}/restore")
async def restore_version(
    request: Request, notebook: str, page_id: str, body: RestoreVersion
):
    state = request.app.state
    store = _store(request)
    async with _PaperWrite(state) as write:
        # Git reads in a thread under the git lock alone, the save on the loop
        # with every other write. No client id: a restore must never pass for
        # a device's own retry.
        parts = await asyncio.to_thread(
            store.read_page_version, state.vault, notebook, page_id, body.sha
        )
        async with write.store():
            result = store.apply_page_version(notebook, page_id, parts)
        _touch(state)
        await _index_page(state, result["page"]["path"])
    state.live.paper_changed(
        result["page"]["path"], result["revision"], origin=body.client_id
    )
    return {
        "page": store.page_public(notebook, page_id),
        "revision": result["revision"],
    }


@router.get("/history/{notebook:path}/{page_id}")
async def page_history(
    request: Request,
    notebook: str,
    page_id: str,
    limit: int = Query(default=50, ge=1, le=300),
):
    state = request.app.state
    path = _store(request).page_dir_path(notebook, page_id)
    async with state.vault._git_lock:
        versions = await asyncio.to_thread(state.vault.dir_history_sync, path, limit)
    return {"path": path, "versions": versions}


# -- assets ------------------------------------------------------------------


@router.post("/assets/{notebook:path}")
async def upload_asset(
    request: Request, notebook: str, file: UploadFile = File(...)
):
    state = request.app.state
    data = await _read_part(file, MAX_ASSET_BYTES, "asset")
    async with _write_lock(state):
        asset = _store(request).store_asset(notebook, file.filename or "", data or b"")
    _touch(state)
    return asset


@router.get("/assets/{notebook:path}/{filename}")
async def read_asset(request: Request, notebook: str, filename: str):
    path = _store(request).resolve_asset(notebook, filename)
    media_type = ASSET_MEDIA_TYPES.get(path.suffix.lower())
    if media_type is None:
        raise NoteError(404, f"Asset not found: {filename}")
    return FileResponse(
        path,
        media_type=media_type,
        headers={
            # The name carries a digest of the bytes, so this URL and its
            # content are welded together.
            "Cache-Control": IMMUTABLE_CACHE_CONTROL,
            "Content-Disposition": "inline",
            "X-Content-Type-Options": "nosniff",
        },
    )
