from pathlib import Path
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from typing import Literal

from pydantic import BaseModel

from .. import task_boards
from ..vault import fm
from ..vault import templates as tpl
from ..index import parse as note_parse
from ..index.db import content_hash
from ..vault.notes import NoteError

router = APIRouter(prefix="/api/notes", tags=["notes"])


class CreateNote(BaseModel):
    path: str
    content: str | None = None
    template: str | None = None


class SaveNote(BaseModel):
    content: str
    base_revision: str | None = None
    # Identifies the editor that made the change, so it can ignore the echo of
    # its own save instead of merging against itself.
    client_id: str | None = None
    # What to do when the note moved on underneath this save. "copy" parks the
    # incoming version in a `(conflict …).md` note — the safe default for any
    # caller that cannot merge. "reject" just reports the newer content back,
    # so an editor that *can* three-way merge does that instead of littering
    # the vault with copies it will immediately have to reconcile by hand.
    on_conflict: Literal["copy", "reject"] = "copy"


class RenameNote(BaseModel):
    new_path: str
    new_title: str | None = None
    # Lets the renaming client recognise its own note_removed echo — without
    # it, the open note greets its own rename with "deleted on another device".
    client_id: str | None = None


class PinNote(BaseModel):
    pinned: bool


PIN_LIMIT = 3


async def ensure_pin_capacity(db, path: str) -> None:
    """Pinned notes are a small deliberate set — the sidebar section stays
    scannable at three. Re-pinning an already-pinned note always passes."""
    rows = await db.execute_fetchall(
        "SELECT COUNT(*) AS n FROM notes WHERE pinned = 1 AND path != ?", (path,)
    )
    if rows[0]["n"] >= PIN_LIMIT:
        raise NoteError(400, f"{PIN_LIMIT} notes are already pinned — unpin one first.")


class CreateFolder(BaseModel):
    path: str


@router.get("")
async def list_notes(request: Request):
    state = request.app.state
    # The sidebar refetches this on every change, so the pinned/board flags and
    # the created/modified dates (for its sort menu) all ride along in one query
    # rather than costing extra requests.
    rows = await state.db.execute_fetchall(
        "SELECT path, pinned, task_board, created, modified FROM notes"
    )
    meta = {r["path"]: r for r in rows}
    return [
        {
            "path": n.path,
            "name": n.name,
            "pinned": bool(meta[n.path]["pinned"]) if n.path in meta else False,
            "board": bool(meta[n.path]["task_board"]) if n.path in meta else False,
            "created": (meta[n.path]["created"] or None) if n.path in meta else None,
            "modified": (meta[n.path]["modified"] or None) if n.path in meta else None,
        }
        for n in state.store.list_notes()
    ]


@router.get("/folders")
async def list_folders(request: Request):
    return request.app.state.store.list_folders()


@router.get("/manifest")
async def manifest(request: Request):
    """Everything a sync client needs to diff against its local cache in one
    round trip: every note with its current revision, plus the folder list.
    Registered before GET /{path:path} so the catch-all doesn't swallow it."""
    state = request.app.state
    rows = await state.db.execute_fetchall("SELECT path, hash, pinned FROM notes")
    indexed = {r["path"]: r for r in rows}
    notes = []
    for n in state.store.list_notes():
        row = indexed.get(n.path)
        revision = row["hash"] if row and row["hash"] else None
        if revision is None:
            # Not indexed yet (or hash missing) — hash the file directly.
            revision = content_hash(state.store.read(n.path))
        notes.append(
            {
                "path": n.path,
                "name": n.name,
                "pinned": bool(row["pinned"]) if row else False,
                "revision": revision,
            }
        )
    return {"notes": notes, "folders": state.store.list_folders()}


@router.post("")
async def create_note(request: Request, body: CreateNote):
    state = request.app.state
    content = body.content
    if content is None and body.template:
        content = tpl.render(
            state.settings.vault_dir, body.template, title=Path(body.path).stem
        )
    content = state.store.create(body.path, content)
    content, added = note_parse.normalize_task_metadata(content)
    if added:
        state.store.resolve(body.path).write_text(content, encoding="utf-8")
    await state.indexer.index_note(body.path, content)
    state.autocommit.touch()
    state.live.note_changed(body.path, content_hash(content))
    meta, _ = fm.parse(content)
    return {
        "path": body.path,
        "content": content,
        "meta": meta,
        "revision": content_hash(content),
        "status": "saved",
    }


@router.post("/folders")
async def create_folder(request: Request, body: CreateFolder):
    state = request.app.state
    state.store.create_folder(body.path)
    state.autocommit.touch()
    return {"path": body.path}


class CreateTaskBoard(BaseModel):
    folder: str


@router.post("/task-board")
async def create_task_board(request: Request, body: CreateTaskBoard):
    """Give a folder its own task board. Declared before the `{path:path}`
    routes below, which would otherwise swallow it."""
    state = request.app.state
    folder = body.folder.strip("/")
    if not folder:
        raise NoteError(400, "Pick a folder — the vault root already has the Tasks board")
    state.store.resolve_folder(folder)  # traversal / reserved-name guard
    # Canonicalize to the folder's on-disk casing. On a case-insensitive
    # filesystem a wrong-case request would write the note into the existing
    # directory while the index filed the board flag under the requested
    # spelling — orphaning the flag, since /api/notes matches DB paths against
    # filesystem paths exactly. Requiring the folder to exist also stops a
    # typo from silently minting a new folder.
    folders = state.store.list_folders()
    if folder not in folders:
        matches = [f for f in folders if f.lower() == folder.lower()]
        if len(matches) == 1:
            folder = matches[0]
        elif matches:
            raise NoteError(400, "Folder name is ambiguous — match its exact casing")
        else:
            raise NoteError(404, "No such folder")
    if await task_boards.board_for_folder(state.db, folder):
        raise NoteError(409, "That folder already has a task board")

    path = task_boards.next_board_path(
        folder, {n.path for n in state.store.list_notes()}
    )
    content = state.store.create(path, task_boards.BOARD_TEMPLATE)
    await state.indexer.index_note(path, content)
    state.autocommit.touch()
    state.live.note_changed(path, content_hash(content))
    return {"folder": folder, "path": path}


class RenameFolder(BaseModel):
    path: str
    new_path: str
    client_id: str | None = None


# Folders the app itself relies on (daily notes, OMNIA memory, templates,
# media tracker items).
PROTECTED_FOLDERS = {"daily", "omnia", "templates", "media"}


@router.post("/folders/rename")
async def rename_folder(request: Request, body: RenameFolder):
    state = request.app.state
    if body.path in PROTECTED_FOLDERS:
        raise NoteError(400, f"'{body.path}' is a built-in folder and cannot be renamed")
    moved = state.store.rename_folder(body.path, body.new_path)
    for old, new in moved:
        await state.indexer.rename_note(old, new)
    state.autocommit.touch()
    # A rename is a removal plus an appearance to anything watching live.
    for old, new in moved:
        state.live.note_removed(old, origin=body.client_id)
        state.live.note_changed(
            new, content_hash(state.store.read(new)), origin=body.client_id
        )
    return {"path": body.new_path, "moved": len(moved)}


@router.delete("/folders/{path:path}")
async def delete_folder(request: Request, path: str):
    state = request.app.state
    if path in PROTECTED_FOLDERS:
        raise NoteError(400, f"'{path}' is a built-in folder and cannot be deleted")
    _manifest, deleted = state.trash.trash_folder(path)
    for rel in deleted:
        await state.indexer.remove_note(rel)
    state.autocommit.touch()
    for rel in deleted:
        state.live.note_removed(rel)
    return {"ok": True, "deleted": len(deleted)}


class AddLink(BaseModel):
    target: str  # note path to link to


@router.post("/{path:path}/add-link")
async def add_link(request: Request, path: str, body: AddLink):
    """Append a wikilink to a note. A trusted user edit (explicit click in the
    Related Notes panel) — no review tray involved."""
    state = request.app.state
    target_name = Path(body.target).stem
    content = state.store.read(path)
    if not content.endswith("\n"):
        content += "\n"
    content += f"\n[[{target_name}]]\n"
    normalized = state.store.write(path, content)
    await state.indexer.index_note(path, normalized)
    state.autocommit.touch()
    return {"path": path, "linked": target_name}


@router.post("/{path:path}/pin")
async def pin_note(request: Request, path: str, body: PinNote):
    state = request.app.state
    if body.pinned:
        await ensure_pin_capacity(state.db, path)
    content = state.store.set_pinned(path, body.pinned)
    await state.indexer.index_note(path, content)
    state.autocommit.touch()
    return {"path": path, "pinned": body.pinned}


@router.get("/{path:path}")
async def read_note(request: Request, path: str):
    content = request.app.state.store.read(path)
    meta, _ = fm.parse(content)
    return {
        "path": path,
        "content": content,
        "meta": meta,
        "revision": content_hash(content),
        "status": "saved",
    }


def _conflict_path(state, path: str) -> str:
    original = Path(path)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H%M%S")
    stem = f"{original.stem} (conflict {stamp})"
    candidate = str(original.with_name(stem + ".md"))
    counter = 2
    while state.store.exists(candidate):
        candidate = str(original.with_name(f"{stem} {counter}.md"))
        counter += 1
    return candidate


@router.put("/{path:path}")
async def save_note(request: Request, path: str, body: SaveNote):
    state = request.app.state
    current = state.store.read(path)
    current_revision = content_hash(current)
    if body.base_revision is not None and body.base_revision != current_revision:
        if body.on_conflict == "reject":
            return {
                "status": "stale",
                "path": path,
                "content": current,
                "meta": fm.parse(current)[0],
                "revision": current_revision,
            }
        conflict_path = _conflict_path(state, path)
        conflict_content, _ = note_parse.normalize_task_metadata(
            body.content, previous_content=current
        )
        # The copy is a new note; reusing the original's task ids would break
        # the tasks index unique constraint when the copy gets indexed.
        conflict_content = note_parse.regenerate_task_ids(conflict_content)
        conflict_content = state.store.create(conflict_path, conflict_content)
        await state.indexer.index_note(conflict_path, conflict_content)
        state.autocommit.touch()
        return {
            "status": "conflict_copy",
            "path": path,
            "content": current,
            "meta": fm.parse(current)[0],
            "revision": current_revision,
            "conflict_path": conflict_path,
            "conflict_revision": content_hash(conflict_content),
        }

    prepared, _ = note_parse.normalize_task_metadata(
        body.content, previous_content=current
    )
    normalized = state.store.write(path, prepared)
    await state.indexer.index_note(path, normalized)
    state.autocommit.touch()
    meta, _ = fm.parse(normalized)
    revision = content_hash(normalized)
    # Tell every other open editor, so the same note on another device merges
    # this in rather than discovering it later as a conflict copy.
    state.live.note_changed(path, revision, origin=body.client_id)
    return {
        "status": "saved",
        "path": path,
        "content": normalized,
        "meta": meta,
        "revision": revision,
    }


@router.post("/{path:path}/rename")
async def rename_note(request: Request, path: str, body: RenameNote):
    state = request.app.state
    target = body.new_path
    title = body.new_title.strip() if body.new_title is not None else None
    if body.new_title is not None and not title:
        raise NoteError(400, "Note title cannot be empty")
    expected_title = state.store.title_from_path(target)
    if (
        title is not None
        and state.store.title_follows_path(target)
        and title != expected_title
    ):
        raise NoteError(400, "Note title must match its Markdown filename")
    if target != path:
        state.store.rename(path, target)
        await state.indexer.rename_note(path, target)
        # Carry a staged proofread across, so renaming mid-review doesn't
        # strand the suggestions on a path that no longer exists.
        state.proofread.rename(path, target)
        content = state.store.read(target)
        state.live.note_removed(path, origin=body.client_id)
        state.live.note_changed(
            target, content_hash(content), origin=body.client_id
        )
    elif title is not None:
        content = state.store.set_title(target, title)
        await state.indexer.index_note(target, content)
        state.live.note_changed(target, content_hash(content))
    else:
        content = state.store.read(target)
    state.autocommit.touch()
    # Content and revision ride along so the client can rebind the open note
    # in place — align_title may have rewritten the frontmatter title, and
    # without the fresh revision the editor would need a full reload.
    return {"path": target, "revision": content_hash(content), "content": content}


@router.delete("/{path:path}")
async def delete_note(request: Request, path: str):
    state = request.app.state
    state.trash.trash_note(path)
    await state.indexer.remove_note(path)
    state.proofread.clear(path)
    state.autocommit.touch()
    state.live.note_removed(path)
    return {"ok": True}
