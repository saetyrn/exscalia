import logging
from pathlib import Path
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from typing import Literal

from pydantic import BaseModel

from .. import sidebar_order, task_boards
from ..vault import fm, names
from ..vault import templates as tpl
from ..index import parse as note_parse
from ..index.db import content_hash
from ..index.links import qualified_target, rewrite_inbound_links
from ..overseer.threads import carry_note_threads
from ..vault.notes import NoteError

router = APIRouter(prefix="/api/notes", tags=["notes"])
log = logging.getLogger(__name__)


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
    # Lets the renaming client recognise its own note_moved echo, which it has
    # already applied in place.
    client_id: str | None = None


class PinNote(BaseModel):
    pinned: bool
    client_id: str | None = None


PIN_LIMIT = 3


def _origin(request: Request, client_id: str | None = None) -> str | None:
    """The editor behind a change, so it can skip its own live echo. A body's
    client_id wins; the app also sends X-Client-Id on every request, which is
    all a delete (no body) carries — without it, trashing the open note greets
    the user with "deleted on another device"."""
    if client_id:
        return client_id
    # Test stubs pass a bare namespace as the request.
    headers = getattr(request, "headers", None)
    return headers.get("x-client-id") if headers is not None else None


async def ensure_pin_capacity(db, path: str) -> None:
    """Pinned notes are a small deliberate set — the sidebar section stays
    scannable at three. Re-pinning an already-pinned note always passes."""
    rows = await db.execute_fetchall(
        "SELECT COUNT(*) AS n FROM notes WHERE pinned = 1 AND path != ?", (path,)
    )
    if rows[0]["n"] >= PIN_LIMIT:
        raise NoteError(400, f"{PIN_LIMIT} notes are already pinned — unpin one first.")


# Bound variables per query, under SQLite's historical default limit of 999.
_ID_CHUNK = 500


async def claim_task_ids(db, path: str, content: str) -> str:
    """Reissue the ids of tasks another note already owns.

    A checklist pasted from another note carries that note's concealed ids;
    indexed as they are, the copies lose to the rows already there and never
    reach the board. A task moved by cut-and-paste keeps its id once the
    source note's save has dropped it from the index."""
    ids = sorted(note_parse.task_ids(content))
    owned: set[str] = set()
    for start in range(0, len(ids), _ID_CHUNK):
        chunk = ids[start : start + _ID_CHUNK]
        rows = await db.execute_fetchall(
            f"SELECT id FROM tasks WHERE id IN ({','.join('?' * len(chunk))}) AND path != ?",
            (*chunk, path),
        )
        owned.update(row["id"] for row in rows)
    return note_parse.replace_task_ids(content, owned) if owned else content


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
            try:
                revision = content_hash(state.store.read(n.path))
            except NoteError as exc:
                # One unreadable file (not UTF-8) must not fail the whole
                # manifest: the desktop engine throws on a non-200 and stops
                # syncing every note. It stays listed with a null revision,
                # which matches no cached base_revision, so the engine pulls
                # it, gets the 422 and keeps its local copy; leaving it out
                # would read as a server delete and drop that copy.
                log.warning("manifest: %s", exc.detail)
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
            state.settings.vault_dir,
            body.template,
            title=Path(body.path).stem,
            timezone=getattr(state, "timezone", None),
        )
    content = state.store.create(body.path, content)
    content, added = note_parse.normalize_task_metadata(content)
    claimed = await claim_task_ids(state.db, body.path, content)
    if added or claimed != content:
        state.store.write_raw(body.path, claimed)
    content = claimed
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


@router.post("/folders/rename")
async def rename_folder(request: Request, body: RenameFolder):
    """The built-in folders (names.PROTECTED_FOLDERS) are refused by the
    store itself, after it normalizes the path."""
    state = request.app.state
    moved = state.store.rename_folder(body.path, body.new_path)
    for old, new in moved:
        await state.indexer.rename_note(old, new)
    rewired = await rewrite_inbound_links(state, moved)
    sidebar_order.rename_paths(
        state.settings.vault_dir, folders=[(body.path, body.new_path)], notes=moved
    )
    carry_note_threads(state, moved)
    state.autocommit.touch()
    # One move per note, so an editor open on one rebinds instead of reading
    # the rename as a deletion. The removal + appearance pair still follows
    # it for clients that predate note_moved.
    origin = _origin(request, body.client_id)
    for old, new in moved:
        revision = content_hash(state.store.read(new))
        state.live.note_moved(old, new, revision, origin=origin)
        state.live.note_removed(old, origin=origin)
        state.live.note_changed(new, revision, origin=origin)
    return {
        "path": state.store.normalize_folder(body.new_path),
        "moved": len(moved),
        **rewired,
    }


class DeleteFolder(BaseModel):
    path: str


async def _delete_folder(state, path: str, origin: str | None = None) -> dict:
    # The built-in folders (names.PROTECTED_FOLDERS) are refused by the trash
    # store itself, after it normalizes the path.
    _manifest, deleted = state.trash.trash_folder(path)
    for rel in deleted:
        await state.indexer.remove_note(rel)
    state.autocommit.touch()
    for rel in deleted:
        state.live.note_removed(rel, origin=origin)
    return {"ok": True, "deleted": len(deleted)}


@router.post("/folders/delete")
async def delete_folder(request: Request, body: DeleteFolder):
    """The folder rides in the body, like rename's, so no note path can be
    mistaken for it."""
    return await _delete_folder(request.app.state, body.path, _origin(request))


@router.delete("/folders/{path:path}")
async def delete_folder_legacy(request: Request, path: str):
    """The spelling older clients still send. Registered ahead of
    `DELETE /{path:path}`, it also catches every note under a top-level folder
    named `folders` — so a path that is no folder but is such a note is that
    note's delete."""
    state = request.app.state
    note = f"folders/{path}"
    if not state.store.resolve_folder(path).is_dir() and state.store.exists(note):
        return await delete_note(request, note)
    return await _delete_folder(state, path, _origin(request))


class AddLink(BaseModel):
    target: str  # note path to link to
    # Optional so older clients keep working; an editor that sends its
    # revision gets a 409 instead of a link appended to a version it never saw.
    base_revision: str | None = None
    client_id: str | None = None


@router.post("/{path:path}/add-link")
async def add_link(request: Request, path: str, body: AddLink):
    """Append a wikilink to a note. A trusted user edit (explicit click in the
    Related Notes panel) — no review tray involved."""
    state = request.app.state
    # Folder-qualified when another note shares the name, so the link opens
    # the note that was clicked rather than a same-name one elsewhere.
    target_name = qualified_target(body.target, [n.path for n in state.store.list_notes()])
    content = state.store.read(path)
    if body.base_revision is not None and body.base_revision != content_hash(content):
        raise NoteError(409, "The note changed on another device; read it again")
    if not content.endswith("\n"):
        content += "\n"
    content += f"\n[[{target_name}]]\n"
    normalized = state.store.write(path, content)
    await state.indexer.index_note(path, normalized)
    state.autocommit.touch()
    meta, _ = fm.parse(normalized)
    revision = content_hash(normalized)
    state.live.note_changed(path, revision, origin=body.client_id)
    return {
        "path": path,
        "content": normalized,
        "meta": meta,
        "revision": revision,
        "status": "saved",
        "linked": target_name,
    }


@router.post("/{path:path}/pin")
async def pin_note(request: Request, path: str, body: PinNote):
    state = request.app.state
    if body.pinned:
        await ensure_pin_capacity(state.db, path)
    content = state.store.set_pinned(path, body.pinned)
    await state.indexer.index_note(path, content)
    state.autocommit.touch()
    # The flag lives in frontmatter: an editor holding this note elsewhere
    # needs the new revision, and every sidebar the new pin.
    state.live.note_changed(path, content_hash(content), origin=body.client_id)
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
    suffix = f" (conflict {stamp})"
    # Keep as much of the name as fits: a long name plus the suffix would pass
    # the filesystem's 255 bytes, and the edit being parked would have nowhere
    # to go. " 999" leaves room for the counter below.
    budget = names.MAX_COMPONENT_BYTES - len(suffix.encode("utf-8")) - len(" 999")
    stem = names.truncate_stem_bytes(original.stem, budget) + suffix
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
        # Nor may it claim the folder's board: a second board in one folder
        # would take over whenever it sorted ahead of the original.
        conflict_content = task_boards.strip_board_flag(conflict_content)
        conflict_content = state.store.create(
            conflict_path, conflict_content, check_name=False
        )
        await state.indexer.index_note(conflict_path, conflict_content)
        state.autocommit.touch()
        # A new note: no origin, so every sidebar — the saver's too — lists it.
        state.live.note_changed(conflict_path, content_hash(conflict_content))
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
    prepared = await claim_task_ids(state.db, path, prepared)
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
    rewired = {"links_updated": 0, "linkers": []}
    if target != path:
        state.store.rename(path, target)
        await state.indexer.rename_note(path, target)
        # Carry a staged proofread across, so renaming mid-review doesn't
        # strand the suggestions on a path that no longer exists.
        state.proofread.rename(path, target)
        # Links that opened the note follow it (its own included, which is
        # why the content is read after), and so does its sidebar slot.
        rewired = await rewrite_inbound_links(state, [(path, target)])
        sidebar_order.rename_paths(state.settings.vault_dir, notes=[(path, target)])
        # And its OMNIA conversation, for every device, not just this one.
        carry_note_threads(state, [(path, target)])
        content = state.store.read(target)
        # note_moved first, so an open editor rebinds; the removal +
        # appearance pair still follows for clients that predate it.
        origin = _origin(request, body.client_id)
        revision = content_hash(content)
        state.live.note_moved(path, target, revision, origin=origin)
        state.live.note_removed(path, origin=origin)
        state.live.note_changed(target, revision, origin=origin)
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
    return {
        "path": target,
        "revision": content_hash(content),
        "content": content,
        **rewired,
    }


@router.delete("/{path:path}")
async def delete_note(request: Request, path: str):
    state = request.app.state
    state.trash.trash_note(path)
    await state.indexer.remove_note(path)
    state.proofread.clear(path)
    state.autocommit.touch()
    state.live.note_removed(path, origin=_origin(request))
    return {"ok": True}
