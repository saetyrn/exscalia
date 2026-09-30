"""OMNIA's direct productivity, recovery, settings, and calendar tools."""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import hashlib
import logging
import re
import secrets
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..api.meta import (
    AppearanceSettingsBody,
    _appearance,
    _redact_remote,
    appearance_payload,
    salvage_appearance,
)
from ..api.paper import restore_paper_item
from ..flashcards import card_state, cards_from_markdown
from ..flashcards import deck_stats as flashcard_stats
from ..flashcards import new_card as new_flashcard
from ..index import parse as note_parse
from ..index.links import (
    LinkResolver,
    qualified_target,
    relink,
    rewrite_inbound_links,
    rewrite_links,
)
from ..index.semantic import prefix_list
from .. import media_items, media_lookup, media_theme, sidebar_order, task_boards
from ..scheduler import SCHEDULE_PATH, ScheduleStore, clean_cadence, notify_run, schedule_tz
from ..tz import local_now, local_today, parse_user_datetime, user_tz
from .vision import attachments_in, is_document, load_document, load_image
from . import background, vision
from ..paper_store import page_url, paper_page_url
from ..index.db import content_hash
from ..task_service import (
    _validate_due,
    clean_task_recurrence,
    create_task as create_task_record,
    delete_task as delete_task_record,
    move_task as move_task_record,
    move_tasks,
    parse_user_date,
    public_task,
    reorder_tasks as reorder_tasks_record,
    set_task_reminder as set_task_reminder_record,
    update_task as update_task_record,
)
from ..vault import fm, names
from ..vault import templates as tpl
from . import observations
from .memory import (
    CATCH_ALL as MEMORY_CATCH_ALL,
    MEMORY_CATEGORIES,
    MEMORY_PATH,
    add_entry as memory_add_entry,
    heading_for as memory_heading_for,
    is_memory_path,
    local_today as memory_today,
    normalized as memory_normalized,
)
from ..vault.internal import TrashStore, atomic_json, read_json
from ..proofread import CATEGORIES, anchor
from ..vault.notes import NoteError, NoteStore, check_landing
from .provider import ToolDef, image_block

log = logging.getLogger(__name__)

Executor = Callable[[Any, str, dict], Awaitable[dict]]
CONFIG_PATH = f"{NoteStore.INTERNAL_DIR}/config.json"
ACTIVITY_PATH = f"{NoteStore.INTERNAL_DIR}/activity-log.json"
EVENTS_PATH = f"{NoteStore.INTERNAL_DIR}/calendar/events.json"
TOMBSTONES_PATH = f"{NoteStore.INTERNAL_DIR}/task-tombstones.json"


def _schema(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required}


def _config(state) -> dict[str, Any]:
    value = read_json(state.settings.vault_dir / CONFIG_PATH, {})
    return value if isinstance(value, dict) else {}


def _write_config(state, value: dict[str, Any]) -> None:
    atomic_json(state.settings.vault_dir / CONFIG_PATH, value)
    state.autocommit.touch()


async def _context_note(state, thread_id: str) -> str | None:
    """The note the user has open, recovered from the thread.

    The chat endpoint writes `context_note` onto the user's message before the
    model runs, so by the time a tool executes it is already persisted. Kept a
    separate function on purpose: the tool-contract test reads `inp[...]` keys
    out of handler bodies, and this is not one of the tool's arguments.
    """
    if not hasattr(state, "threads"):
        return None  # test harness, or a run without a thread store
    try:
        thread = state.threads.get(thread_id)
    except Exception:
        return None
    for message in reversed((thread or {}).get("messages") or []):
        if message.get("role") == "user":
            return message.get("context_note")
    return None


async def _selection(state, thread_id: str) -> dict | None:
    """The editor selection range from the user's message — persisted alongside
    context_note so selection-editing tools can act on the exact offsets. Returns
    {from, to, head, doc_len, path} or None when there was no live editor."""
    if not hasattr(state, "threads"):
        return None
    try:
        thread = state.threads.get(thread_id)
    except Exception:
        return None
    for message in reversed((thread or {}).get("messages") or []):
        if message.get("role") == "user":
            sel = message.get("selection_range")
            return sel if isinstance(sel, dict) and sel.get("path") else None
    return None


async def _default_task_path(state, thread_id: str) -> str:
    """Where a task goes when the model does not name a note: the board of the
    folder the user is working in, else the global inbox."""
    board = await task_boards.nearest_board(state.db, await _context_note(state, thread_id))
    return board or "Tasks Inbox.md"


async def _record(
    state,
    thread_id: str,
    tool: str,
    summary: str,
    paths: list[str],
    refresh: list[str],
    result: dict[str, Any],
    *,
    undoable: bool = True,
    navigate: str | None = None,
    close_tabs: dict | None = None,
) -> dict[str, Any]:
    # The chat goto()s `navigate` verbatim, so a bare vault path would resolve
    # against whatever page the user is on. Anything not already a route is a
    # note path.
    if navigate and not navigate.startswith("/"):
        navigate = f"/note/{navigate}"
    return await state.actions.record(
        thread_id=thread_id,
        tool=tool,
        summary=summary,
        paths=paths,
        refresh=refresh,
        result=result,
        undoable=undoable,
        navigate=navigate,
        close_tabs=close_tabs,
    )


async def _write_note(
    state, path, content, *, previous=None, create=False, check_name=True
) -> str:
    """Save and index a note the way the editor does: task lines get their
    metadata comments first. A bare `- [ ]` is indexed as a `legacy:` task
    that no task operation can find again. `previous` is the note as read
    before the edit, so a task whose comment was lost keeps its id. Returns
    what was written (frontmatter prepared), which is what revisions hash.
    `check_name=False` (create only) skips the new-name rules for a copy
    named after an existing note, or a media item's slugged path."""
    if content is not None:  # None: NoteStore.create builds an empty note
        content, _ = note_parse.normalize_task_metadata(content, previous_content=previous)
    if create:
        written = state.store.create(path, content, check_name=check_name)
    else:
        written = state.store.write(path, content)
    await state.indexer.index_note(path, written)
    return written


class _Recording:
    """Bookkeeping for a tool that writes once per item. `_record` is a tool's
    last statement, so an exception after k writes used to skip it: the k
    changes had no commit, card or Undo (an anonymous autosave swept them up),
    and the model, told only that the call failed, resent the whole list and
    duplicated them. A failure inside `async with` after a write records what
    was written as one undoable action and leaves the result on `.failed` for
    the tool to return, the counts in its error since that is all the model
    reads. A failure before any write raises as before.

        async with _Recording(state, thread_id, tool, refresh, n, summary) as rec:
            for item in items:
                rec.current = label  # named in the error
                ...write...
                rec.done(path)
        if rec.failed:
            return rec.failed
    """

    def __init__(self, state, thread_id, tool, refresh, total, summary):
        self.state, self.thread_id, self.tool = state, thread_id, tool
        self.refresh, self.total, self.summary = refresh, total, summary
        self.paths: set[str] = set()
        self.applied = 0
        self.current: str | None = None
        self.failed: dict[str, Any] | None = None

    def wrote(self, *paths: str) -> None:
        """A write that is not one of the `total` items (an archive note)."""
        self.paths.update(paths)

    def done(self, *paths: str) -> None:
        self.applied += 1
        self.paths.update(paths)

    async def __aenter__(self) -> "_Recording":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if not isinstance(exc, Exception) or not self.paths:
            return False
        if isinstance(exc, NoteError):
            detail = exc.detail
        else:
            log.error(
                "%s failed after %d of %d (thread %s)",
                self.tool,
                self.applied,
                self.total,
                self.thread_id,
                exc_info=exc,
            )
            detail = f"Tool failed: {exc}"
        at = f" at “{self.current}”" if self.current else ""
        # Counts first: an approved card's outcome reaches the next turn
        # clipped to 200 characters.
        if self.applied:
            error = (
                f"Applied {self.applied} of {self.total} (one undoable action; do not "
                f"resend those), then failed{at}: {detail}"
            )
        else:  # only a side write landed (an archive note)
            error = f"Failed{at} with 0 of {self.total} applied: {detail}"
        self.failed = await _record(
            self.state,
            self.thread_id,
            self.tool,
            f"{self.summary(self.applied)} (partial: {self.applied} of {self.total})",
            sorted(self.paths),
            self.refresh,
            {"error": error, "applied": self.applied, "failed_at": self.current},
        )
        return True


async def _legacy_stage(
    state,
    thread_id: str,
    op: str,
    path: str,
    *,
    new_content: str | None = None,
    new_path: str | None = None,
    rationale: str | None = None,
) -> dict[str, Any] | None:
    """Compatibility for minimal test/embedded states without the coordinator."""
    if hasattr(state, "actions"):
        return None
    pending_id = await state.pending.stage(
        thread_id,
        op,
        path,
        new_content=new_content,
        new_path=new_path,
        rationale=rationale,
    )
    return {"staged": True, "pending_id": pending_id}


def _require_revision(state, path: str, expected: str | None) -> str:
    current = state.store.read(path)
    if not expected:
        raise NoteError(409, "Read the note first and provide its current revision")
    if content_hash(current) != expected:
        raise NoteError(409, "The note changed on another device; read it again")
    return current


def _require_unchanged(state, path: str, checked: str) -> None:
    """Compare-and-set for a tool that awaited between reading `path` and
    writing it: an editor autosave in between would otherwise be overwritten
    with the content read before it. No await may sit between this and the
    write it guards."""
    if content_hash(state.store.read(path)) != checked:
        raise NoteError(409, f"{path} changed while OMNIA was working; read it again")


def _refuse_memory_staging(*paths) -> None:
    """The memory note is written one entry at a time through remember/forget.
    A staged whole-file rewrite was the one way a proposal-only background pass
    could reach the next turn's standing context, and forcing it through later
    would drop every entry remembered since."""
    if any(is_memory_path(path) for path in paths):
        raise NoteError(400, f"{MEMORY_PATH} is written through remember/forget, never staged")


async def propose_note_create(state, thread_id, inp) -> dict:
    path, content = inp["path"], inp["content"]
    _refuse_memory_staging(path)
    if await state.pending.effective_content(path) is not None:
        raise NoteError(409, f"{path} already exists")
    pending_id = await state.pending.stage(
        thread_id,
        "create",
        path,
        new_content=content,
        rationale=inp.get("rationale"),
    )
    return {"staged": True, "pending_id": pending_id}


async def propose_note_delete(state, thread_id, inp) -> dict:
    """Suggest a deletion for review. PendingStore has always implemented the
    delete op — staleness, diffing, approve-all — with no tool to produce one,
    so the only route was the irreversible-feeling confirmation path."""
    path = inp["path"]
    _refuse_memory_staging(path)
    if await state.pending.effective_content(path) is None:
        raise NoteError(404, f"{path} does not exist")
    pending_id = await state.pending.stage(
        thread_id, "delete", path, rationale=inp.get("rationale")
    )
    return {"staged": True, "pending_id": pending_id, "op": "delete", "path": path}


async def propose_note_move(state, thread_id, inp) -> dict:
    """Suggest a move/rename for review."""
    path = inp["path"]
    new_path = inp["new_path"]
    _refuse_memory_staging(path, new_path)
    if await state.pending.effective_content(path) is None:
        raise NoteError(404, f"{path} does not exist")
    if await state.pending.effective_content(new_path) is not None:
        raise NoteError(409, f"{new_path} already exists")
    pending_id = await state.pending.stage(
        thread_id,
        "move",
        path,
        new_path=new_path,
        rationale=inp.get("rationale"),
    )
    return {
        "staged": True,
        "pending_id": pending_id,
        "op": "move",
        "path": path,
        "new_path": new_path,
    }


async def propose_note_edit(state, thread_id, inp) -> dict:
    path = inp["path"]
    _refuse_memory_staging(path)
    if await state.pending.effective_content(path) is None:
        raise NoteError(404, f"{path} does not exist")
    pending_id = await state.pending.stage(
        thread_id,
        "edit",
        path,
        new_content=inp["new_content"],
        rationale=inp.get("rationale"),
    )
    return {"staged": True, "pending_id": pending_id}


async def create_note(state, thread_id, inp) -> dict:
    path = inp["path"]
    legacy = await _legacy_stage(
        state,
        thread_id,
        "create",
        path,
        new_content=inp.get("content") or "",
        rationale=inp.get("rationale"),
    )
    if legacy is not None:
        return legacy
    normalized = await _write_note(state, path, inp.get("content"), create=True)
    return await _record(
        state,
        thread_id,
        "create_note",
        f"Created {path}",
        [path],
        ["notes", "search", "tasks", "calendar"],
        {"path": path, "revision": content_hash(normalized)},
    )


async def edit_note(state, thread_id, inp) -> dict:
    path = inp["path"]
    legacy = await _legacy_stage(
        state,
        thread_id,
        "edit",
        path,
        new_content=inp["new_content"],
        rationale=inp.get("rationale"),
    )
    if legacy is not None:
        return legacy
    current = _require_revision(state, path, inp.get("base_revision"))
    normalized = await _write_note(state, path, inp["new_content"], previous=current)
    return await _record(
        state,
        thread_id,
        "edit_note",
        f"Edited {path}",
        [path],
        ["notes", "search", "tasks", "calendar"],
        {"path": path, "revision": content_hash(normalized)},
    )


async def move_note(state, thread_id, inp) -> dict:
    old, new = inp["path"], inp["new_path"]
    legacy = await _legacy_stage(
        state,
        thread_id,
        "move",
        old,
        new_path=new,
        rationale=inp.get("rationale"),
    )
    if legacy is not None:
        return legacy
    _require_revision(state, old, inp.get("base_revision"))
    state.store.rename(old, new)
    await state.indexer.rename_note(old, new)
    paths = [old, new]
    paths += await _rewire_links(state, [(old, new)], paths)
    if sidebar_order.rename_paths(state.settings.vault_dir, notes=[(old, new)]):
        paths.append(CONFIG_PATH)
    return await _record(
        state,
        thread_id,
        "move_note",
        f"Moved {old} to {new}",
        paths,
        ["notes", "search", "tasks", "calendar"],
        {"path": new, "old_path": old, "revision": content_hash(state.store.read(new))},
    )


async def delete_note(state, thread_id, inp) -> dict:
    path = inp["path"]
    legacy = await _legacy_stage(
        state,
        thread_id,
        "delete",
        path,
        rationale=inp.get("rationale"),
    )
    if legacy is not None:
        return legacy
    _require_revision(state, path, inp.get("base_revision"))
    manifest = state.trash.trash_note(path)
    await state.indexer.remove_note(path)
    trash_path = f"{NoteStore.INTERNAL_DIR}/trash/{manifest['id']}"
    return await _record(
        state,
        thread_id,
        "delete_note",
        f"Moved {path} to Trash",
        [path, trash_path],
        ["notes", "search", "tasks", "calendar", "trash"],
        {"path": path, "trash_item": manifest},
    )


async def edit_frontmatter(state, thread_id, inp) -> dict:
    path = inp["path"]
    if not hasattr(state, "actions"):
        content = await state.pending.effective_content(path)
        if content is None:
            raise NoteError(404, f"{path} does not exist")
        meta, body = fm.parse(content)
        for key, value in (inp.get("set") or {}).items():
            meta[key] = value
        for key in inp.get("remove") or []:
            meta.pop(key, None)
        return (
            await _legacy_stage(
                state,
                thread_id,
                "edit",
                path,
                new_content=fm.serialize(meta, body),
                rationale=inp.get("rationale"),
            )
            or {}
        )
    current = _require_revision(state, path, inp.get("base_revision"))
    meta, body = fm.parse(current)
    for key, value in (inp.get("set") or {}).items():
        if key in {"created", "modified", "title"}:
            raise NoteError(400, f"{key} is managed by EXSCALIA")
        meta[key] = value
    for key in inp.get("remove") or []:
        if key in {"created", "modified", "title"}:
            raise NoteError(400, f"{key} cannot be removed")
        meta.pop(key, None)
    normalized = await _write_note(state, path, fm.serialize(meta, body), previous=current)
    return await _record(
        state,
        thread_id,
        "edit_frontmatter",
        f"Updated properties in {path}",
        [path],
        ["notes", "search"],
        {"path": path, "revision": content_hash(normalized)},
    )


async def list_folders(state, thread_id, inp) -> dict:
    folders = state.store.list_folders()
    if not folders:
        return {"folders": [], "note": "The vault has no folders; every note is at the root."}
    return {"folders": folders}


async def create_folder(state, thread_id, inp) -> dict:
    path = inp["path"]
    state.store.create_folder(path)
    return await _record(
        state,
        thread_id,
        "create_folder",
        f"Created folder {path}",
        [f"{path}/.gitkeep"],
        ["notes"],
        {"path": path},
    )


async def move_folder(state, thread_id, inp) -> dict:
    # Canonical spellings for the action record; rename_folder refuses the
    # built-in folders itself.
    old = state.store.normalize_folder(inp["path"])
    new = state.store.normalize_folder(inp["new_path"])
    moved = state.store.rename_folder(old, new)
    for old_note, new_note in moved:
        await state.indexer.rename_note(old_note, new_note)
    paths = [old, new]
    paths += await _rewire_links(state, moved, paths)
    if sidebar_order.rename_paths(
        state.settings.vault_dir, folders=[(old, new)], notes=moved
    ):
        paths.append(CONFIG_PATH)
    return await _record(
        state,
        thread_id,
        "move_folder",
        f"Moved folder {old} to {new}",
        paths,
        ["notes", "search", "tasks", "calendar"],
        {"path": new, "old_path": old, "moved_notes": len(moved)},
    )


async def delete_folder(state, thread_id, inp) -> dict:
    path = state.store.normalize_folder(inp["path"])
    manifest, deleted = state.trash.trash_folder(path)
    for note_path in deleted:
        await state.indexer.remove_note(note_path)
    trash_path = f"{NoteStore.INTERNAL_DIR}/trash/{manifest['id']}"
    return await _record(
        state,
        thread_id,
        "delete_folder",
        f"Moved folder {path} to Trash",
        [path, trash_path],
        ["notes", "search", "tasks", "calendar", "trash"],
        {"path": path, "deleted_notes": len(deleted), "trash_item": manifest},
    )


async def ensure_daily_note(state, thread_id, inp) -> dict:
    requested = inp.get("date")
    try:
        day = date.fromisoformat(requested) if requested else local_now(state).date()
    except ValueError as exc:
        raise NoteError(400, "Daily date must be YYYY-MM-DD") from exc
    path = f"daily/{day.isoformat()}.md"
    if state.store.exists(path):
        content = state.store.read(path)
        return {"path": path, "revision": content_hash(content), "created": False}
    content = tpl.render(
        state.settings.vault_dir, "daily", title=day.isoformat(), date=day.isoformat()
    )
    normalized = await _write_note(state, path, content, create=True)
    return await _record(
        state,
        thread_id,
        "ensure_daily_note",
        f"Created daily note {day.isoformat()}",
        [path],
        ["notes", "calendar"],
        {"path": path, "revision": content_hash(normalized), "created": True},
    )


async def _rewire_links(state, pairs, affected: list[str]) -> list[str]:
    """Rewrite the links that opened moved notes, the way a rename from the
    editor or the sidebar does (index/links.py). Returns the linkers not
    already in `affected`, for the action record to commit and announce with
    the move, so an undo reverts them together."""
    rewired = await rewrite_inbound_links(state, pairs, announce=False)
    return [p for p in rewired["linkers"] if p not in affected]


def _unique_path(state, path: str, reserved: list[str] | None = None) -> str:
    """`path`, or the first free `<stem> N` beside it. Free means no note
    matches by names.identity_key, the key NoteStore.create refuses a sibling
    on, so 'Café.md' beside 'café.md' becomes 'Café 2.md' instead of a 409.
    `reserved` are paths the caller is about to create."""
    taken = {names.identity_key(note.path) for note in state.store.list_notes()}
    taken |= {names.identity_key(p) for p in reserved or ()}
    original = Path(path)
    candidate = path
    number = 2
    while names.identity_key(candidate) in taken:
        candidate = str(original.with_name(f"{original.stem} {number}{original.suffix}"))
        number += 1
    return candidate


async def create_note_from_template(state, thread_id, inp) -> dict:
    path = _unique_path(state, inp["path"])
    content = tpl.render(
        state.settings.vault_dir,
        inp["template"],
        title=Path(path).stem,
        date=inp.get("date"),
        timezone=state.timezone,
    )
    if content is None:
        raise NoteError(404, "Template not found")
    normalized = await _write_note(state, path, content, create=True)
    return await _record(
        state,
        thread_id,
        "create_note_from_template",
        f"Created {path} from template {inp['template']}",
        [path],
        ["notes", "search", "tasks", "calendar"],
        {"path": path, "revision": content_hash(normalized)},
    )


async def set_note_pin(state, thread_id, inp) -> dict:
    from ..api.notes import ensure_pin_capacity  # local import avoids a router import cycle

    path = inp["path"]
    if inp["pinned"]:
        await ensure_pin_capacity(state.db, path)
    content = state.store.set_pinned(path, bool(inp["pinned"]))
    await state.indexer.index_note(path, content)
    return await _record(
        state,
        thread_id,
        "set_note_pin",
        f"{'Pinned' if inp['pinned'] else 'Unpinned'} {path}",
        [path],
        ["notes"],
        {"path": path, "pinned": bool(inp["pinned"])},
    )


async def add_wikilink(state, thread_id, inp) -> dict:
    path, target = inp["path"], inp["target"]
    current = _require_revision(state, path, inp.get("base_revision"))
    state.store.resolve(target)
    if not state.store.exists(target):
        raise NoteError(404, f"Linked note not found: {target}")
    # Folder-qualified when another note shares the name, so it opens `target`.
    link = f"[[{qualified_target(target, [n.path for n in state.store.list_notes()])}]]"
    if link in current:
        return {"path": path, "linked": target, "already_present": True}
    content = current + ("" if current.endswith("\n") else "\n") + f"\n{link}\n"
    normalized = await _write_note(state, path, content, previous=current)
    return await _record(
        state,
        thread_id,
        "add_wikilink",
        f"Linked {path} to {target}",
        [path],
        ["notes", "search"],
        {"path": path, "target": target, "revision": content_hash(normalized)},
    )


def _task_scope_clause(path: str) -> tuple[str, list[str]]:
    """Task `path` filters take what the model was given, which is rarely an
    exact note path: "School" must find the tasks in School/Tasks.md. Matches
    the note itself, the name with .md appended, or anything inside the
    folder — case-insensitively."""
    p = str(path).strip().strip("/").lower()
    if p.endswith(".md"):
        return "lower(path) = ?", [p]
    escaped = p.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return (
        "(lower(path) = ? OR lower(path) = ? OR lower(path) LIKE ? ESCAPE '\\')",
        [p, f"{p}.md", f"{escaped}/%"],
    )


async def _resolve_board_arg(state, path: str) -> str:
    """A board named by its folder ("School") resolves to that folder's board
    note; a note path passes through untouched."""
    p = str(path).strip().strip("/")
    if p.lower().endswith(".md"):
        return p
    boards = await task_boards.board_map(state.db)
    lowered = {folder.lower(): note for folder, note in boards.items()}
    return lowered.get(p.lower(), path)


LIST_TASKS_LIMIT = 500


async def list_tasks(state, thread_id, inp) -> dict:
    # Completed tasks are listed unless the model opts out, and the opt-out is
    # applied in SQL: filtered after the LIMIT, old done tasks (their dues sort
    # first) crowded the open ones out of a long-lived vault.
    clauses, values = _task_filter(
        {**inp, "include_completed": inp.get("include_completed", True)}
    )
    rows = await state.db.execute_fetchall(
        f"SELECT * FROM tasks WHERE {' AND '.join(clauses) or '1=1'} "
        "ORDER BY due IS NULL, due, path, line LIMIT ?",
        [*values, LIST_TASKS_LIMIT + 1],
    )
    tasks = [public_task(dict(row)) for row in rows[:LIST_TASKS_LIMIT]]
    if not tasks:
        return {"tasks": [], "note": await _empty_tasks_note(state, inp)}
    if len(rows) > LIST_TASKS_LIMIT:
        return {"tasks": tasks, "truncated": True}
    return {"tasks": tasks}


async def _empty_tasks_note(state, inp: dict) -> str:
    """An empty list_tasks result must say whether the vault or the query is
    empty — the model otherwise reads "no rows" as "no tasks" and tells the
    user their work doesn't exist."""
    open_rows = await state.db.execute_fetchall(
        "SELECT path, COUNT(*) AS n FROM tasks WHERE status != 'done' "
        "GROUP BY path ORDER BY n DESC LIMIT 8"
    )
    if not open_rows:
        return "The vault has no open tasks at all."
    filters = ", ".join(
        f'{key}="{inp[key]}"'
        for key in ("path", "status", "text", "due_start", "due_end")
        if inp.get(key)
    )
    where = "; ".join(f"{row['path']} ({row['n']} open)" for row in open_rows)
    if not filters:
        return f"No tasks matched, but open tasks exist: {where}."
    return (
        f"No tasks matched {filters}. This reflects the query, not the vault — "
        f"notes with open tasks: {where}. Retry with one of those paths or "
        "fewer filters."
    )


def _empty_note(what: str, total: int, noun: str, hint: str = "") -> str:
    """The Retry-reflex note (HANDOFF §5) for a filtered result that came back
    empty: say whether the vault or the query is empty, then what to try. A
    bare empty list reads as "there are none" and invites the same call again."""
    if not total:
        return f"The vault has no {noun} at all."
    return " ".join(
        part
        for part in (what, f"This reflects the query, not the vault: {total} {noun} exist.", hint)
        if part
    )


async def get_task_timeline(state, thread_id, inp) -> dict:
    task_id = inp["task_id"]
    cur = await state.db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
    row = await cur.fetchone()
    task = dict(row) if row else state.task_tombstones.all().get(task_id)
    if not task:
        raise NoteError(404, "Task not found")
    return {
        "task": task,
        "milestones": {
            key: task.get(key)
            for key in (
                "created_at",
                "started_at",
                "completed_at",
                "reopened_at",
                "deleted_at",
            )
        },
    }


async def create_task(state, thread_id, inp) -> dict:
    task = await create_task_record(
        state,
        text=inp["text"],
        path=inp.get("path") or await _default_task_path(state, thread_id),
        status=inp.get("status") or "todo",
        due=inp.get("due"),
        due_visible=bool(inp.get("due_visible", False)),
        description=inp.get("description"),
    )
    return await _record(
        state,
        thread_id,
        "create_task",
        f"Created task “{task['text']}”",
        [task["path"]],
        ["notes", "tasks", "calendar", "search"],
        {"task": task},
    )


async def update_task(state, thread_id, inp) -> dict:
    task_id = inp["task_id"]
    before_cur = await state.db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
    before = await before_cur.fetchone()
    if not before:
        raise NoteError(404, "Task not found")
    kwargs: dict[str, Any] = {}
    for key in ("text", "status", "due", "due_visible", "description"):
        if key in inp:
            kwargs[key] = inp[key]
    task = await update_task_record(state, task_id, **kwargs)
    return await _record(
        state,
        thread_id,
        "update_task",
        f"Updated task “{task['text']}”",
        [task["path"]],
        ["notes", "tasks", "calendar", "search"],
        {"task": task},
    )


async def delete_task(state, thread_id, inp) -> dict:
    task_id = inp["task_id"]
    cur = await state.db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
    row = await cur.fetchone()
    if not row:
        raise NoteError(404, "Task not found")
    before = dict(row)
    state.task_tombstones.record([before])
    result = await delete_task_record(state, task_id)
    return await _record(
        state,
        thread_id,
        "delete_task",
        f"Deleted task “{before['text']}”",
        [before["path"], TOMBSTONES_PATH],
        ["notes", "tasks", "calendar", "search"],
        result,
    )


async def reorder_tasks(state, thread_id, inp) -> dict:
    ordered = await reorder_tasks_record(state, inp.get("task_ids") or [])
    paths = sorted({task["path"] for task in ordered})
    count = len(ordered)
    return await _record(
        state,
        thread_id,
        "reorder_tasks",
        f"Reordered {count} task{'' if count == 1 else 's'}",
        paths,
        ["notes", "tasks"],
        {"tasks": ordered},
    )


async def move_task(state, thread_id, inp) -> dict:
    task_id = inp["task_id"]
    before_cur = await state.db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
    before = await before_cur.fetchone()
    if not before:
        raise NoteError(404, "Task not found")
    source = before["path"]
    task = await move_task_record(state, task_id, inp.get("target_path") or "")
    paths = sorted({source, task["path"]})
    return await _record(
        state,
        thread_id,
        "move_task",
        f"Moved task “{task['text']}” to {task['path']}",
        paths,
        ["notes", "tasks", "calendar", "search"],
        {"task": task},
    )


async def set_task_reminder(state, thread_id, inp) -> dict:
    task_id = inp["task_id"]
    cur = await state.db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
    row = await cur.fetchone()
    if not row:
        raise NoteError(404, "Task not found")
    if inp.get("clear"):
        remind_at = None
    else:
        remind_at = str(inp.get("remind_at") or "").strip()
        if remind_at:
            # Stored as local YYYY-MM-DDTHH:MM. The ticker compares naive local
            # times, so a kept '14:00Z' fired at 14:00 local and a bare date at
            # midnight.
            if _is_bare_date(remind_at):
                remind_at = f"{remind_at}T{inp.get('at_time') or REMINDER_TIME}"
            try:
                remind_at = parse_user_datetime(remind_at, user_tz(state)).isoformat(
                    timespec="minutes"
                )
            except ValueError as exc:
                raise NoteError(400, "remind_at must be YYYY-MM-DDTHH:MM") from exc
        else:
            due = row["due"]
            if not due:
                raise NoteError(
                    400,
                    "This task has no due date — give an explicit remind_at, or set a due date first",
                )
            days_before = int(inp.get("days_before") or 0)
            at_time = str(inp.get("at_time") or REMINDER_TIME)
            try:
                base = datetime.fromisoformat(f"{due}T{at_time}")
            except ValueError as exc:
                raise NoteError(400, "at_time must be HH:MM") from exc
            remind_at = (base - timedelta(days=days_before)).isoformat(timespec="minutes")
    task = await set_task_reminder_record(state, task_id, remind_at)
    summary = (
        f"Cleared the reminder on task “{task['text']}”"
        if remind_at is None
        else f"Set a reminder for {remind_at} on task “{task['text']}”"
    )
    return await _record(
        state,
        thread_id,
        "set_task_reminder",
        summary,
        [task["path"]],
        ["notes", "tasks", "calendar"],
        {"task": task},
    )


async def create_recurring_task(state, thread_id, inp) -> dict:
    recurrence = clean_task_recurrence(inp.get("recurrence"))
    due = inp.get("due")
    if not due:
        raise NoteError(400, "A recurring task needs a first due date")
    task = await create_task_record(
        state,
        text=inp["text"],
        path=inp.get("path") or await _default_task_path(state, thread_id),
        due=due,
        due_visible=bool(inp.get("due_visible", False)),
        description=inp.get("description"),
        recurrence=recurrence,
    )
    return await _record(
        state,
        thread_id,
        "create_recurring_task",
        f"Created recurring task “{task['text']}” (every {recurrence['interval']} {recurrence['frequency']})",
        [task["path"]],
        ["notes", "tasks", "calendar", "search"],
        {"task": task, "recurrence": recurrence},
    )


async def create_task_board(state, thread_id, inp) -> dict:
    folder = (inp.get("folder") or "").strip("/")
    if not folder:
        raise NoteError(400, "Pick a folder — the vault root already has the Tasks board")
    folder = _canonical_folder(state, folder)
    if await task_boards.board_for_folder(state.db, folder):
        raise NoteError(409, "That folder already has a task board")
    path = task_boards.next_board_path(
        folder, {note.path for note in state.store.list_notes()}
    )
    content = await _write_note(state, path, task_boards.BOARD_TEMPLATE, create=True)
    return await _record(
        state,
        thread_id,
        "create_task_board",
        f"Created a task board in {folder}",
        [path],
        ["notes", "search", "tasks"],
        {"path": path, "revision": content_hash(content)},
        navigate=f"/tasks/{folder}",
    )


def _canonical_folder(state, folder: str) -> str:
    """A user-named folder in its on-disk casing. macOS is case-insensitive and
    Linux prod is not, so "school" must become "School" before it keys a board
    or a route; a folder that isn't real is refused."""
    folder = folder.strip().strip("/")
    state.store.resolve_folder(folder)  # traversal / reserved-name guard
    folders = state.store.list_folders()
    if folder in folders:
        return folder
    matches = [f for f in folders if f.lower() == folder.lower()]
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise NoteError(400, "Folder name is ambiguous — match its exact casing")
    raise NoteError(404, "No such folder")


def _task_filter(inp: dict) -> tuple[list[str], list[Any]]:
    """The list_tasks filter, shared by the bulk operations. Returns SQL
    clauses and their bound values; excludes completed tasks unless asked or
    a status is named — status='done' ANDed with `status != 'done'` matched
    nothing, so "clear the done column" reported zero tasks."""
    clauses: list[str] = []
    values: list[Any] = []
    if inp.get("status"):
        clauses.append("status = ?")
        values.append(inp["status"])
    if inp.get("text"):
        clauses.append("lower(text) LIKE ?")
        values.append(f"%{str(inp['text']).lower()}%")
    if inp.get("path"):
        clause, bound = _task_scope_clause(inp["path"])
        clauses.append(clause)
        values.extend(bound)
    if inp.get("due_start"):
        clauses.append("due >= ?")
        values.append(parse_user_date(inp["due_start"], "due_start"))
    if inp.get("due_end"):
        clauses.append("due <= ?")
        values.append(parse_user_date(inp["due_end"], "due_end"))
    if not inp.get("include_completed") and not inp.get("status"):
        clauses.append("status != 'done'")
    return clauses, values


def _refuse_bare_tasks(rows) -> None:
    """Updates and moves refuse a task with no metadata comment (a `legacy:`
    row, whose id is its line number, so an earlier delete in the same note
    points it at the next line), and a bulk change reaching one part-way
    stopped there with the rows before it already changed. Name them all
    before the first write instead. An editor save mints the missing comments."""
    bare = [row for row in rows if row["meta_line"] is None]
    if bare:
        notes = ", ".join(sorted({row["path"] for row in bare}))
        some = "1 matching task has" if len(bare) == 1 else f"{len(bare)} matching tasks have"
        raise NoteError(
            409,
            f"{some} no metadata comment (in {notes}) and can't be changed until "
            "those notes are saved once in the editor. Nothing was changed",
        )


BULK_TASK_LIMIT = 200


async def bulk_edit_tasks(state, thread_id, inp) -> dict:
    """Apply ONE change to every task matching a filter, in a single commit."""
    if not any(inp.get(key) for key in ("status", "text", "path", "due_start", "due_end")):
        raise NoteError(
            400,
            "Give at least one filter (status, text, path, or due range) so a bulk edit can't touch every task",
        )
    chosen = [
        key
        for key in ("set_status", "set_due", "move_to", "delete")
        if (key == "set_due" and "set_due" in inp) or inp.get(key)
    ]
    if len(chosen) != 1:
        raise NoteError(400, "Specify exactly one change: set_status, set_due, move_to, or delete")
    change = chosen[0]

    clauses, values = _task_filter(inp)
    rows = await state.db.execute_fetchall(
        f"SELECT * FROM tasks WHERE {' AND '.join(clauses)} ORDER BY path, line", values
    )
    if not rows:
        return {"changed": 0, "tasks": [], "note": await _empty_tasks_note(state, inp)}
    if len(rows) > BULK_TASK_LIMIT:
        raise NoteError(
            400, f"That matches {len(rows)} tasks — narrow the filter (max {BULK_TASK_LIMIT} per bulk edit)"
        )
    _refuse_bare_tasks(rows)

    count = len(rows)
    labels = {
        "set_status": f"set status to {inp.get('set_status')}",
        "set_due": "clear the due date" if not inp.get("set_due") else f"set due {inp.get('set_due')}",
        "move_to": f"move to {inp.get('move_to')}",
        "delete": "delete",
    }
    tomb = getattr(state, "task_tombstones", None)
    move_ids: list[str] = []
    async with _Recording(
        state,
        thread_id,
        "bulk_edit_tasks",
        ["notes", "tasks", "calendar", "search"],
        count,
        lambda k: f"Bulk {labels[change]} on {k} task{'' if k == 1 else 's'}",
    ) as rec:
        for row in rows:
            task_id = row["id"]
            rec.current = row["text"]
            if change == "delete":
                if tomb is not None:
                    tomb.record([dict(row)])
                await delete_task_record(state, task_id)
                rec.done(row["path"])
                if tomb is not None:
                    rec.wrote(TOMBSTONES_PATH)
            elif change == "set_status":
                task = await update_task_record(state, task_id, status=inp["set_status"])
                rec.done(task["path"])
            elif change == "set_due":
                task = await update_task_record(state, task_id, due=inp.get("set_due") or None)
                rec.done(task["path"])
            else:  # move_to — collected, then moved in one write per note
                move_ids.append(task_id)
        if move_ids:
            # move_tasks validates every source before its first write.
            rec.current = None
            moved = await move_tasks(state, move_ids, inp["move_to"])
            rec.wrote(*(row["path"] for row in rows), *(task["path"] for task in moved))
    if rec.failed:
        return rec.failed

    return await _record(
        state,
        thread_id,
        "bulk_edit_tasks",
        f"Bulk {labels[change]} on {count} task{'' if count == 1 else 's'}",
        sorted(rec.paths),
        ["notes", "tasks", "calendar", "search"],
        {"changed": count},
    )


async def archive_completed_tasks(state, thread_id, inp) -> dict:
    """Sweep a board's completed tasks into a dated archive note, keeping their
    completion metadata so the calendar record survives."""
    source = (inp.get("path") or "").strip()
    if not source:
        raise NoteError(400, "Name the board or note to archive completed tasks from")
    source = await _resolve_board_arg(state, source)
    if not state.store.exists(source):
        raise NoteError(404, "Note not found")
    rows = await state.db.execute_fetchall(
        "SELECT id, path, meta_line FROM tasks WHERE path = ? AND status = 'done'", (source,)
    )
    if not rows:
        return {"archived": 0, "archive_path": None, "note": f"{source} has no completed tasks."}
    _refuse_bare_tasks(rows)
    stem = Path(source).stem
    archive_path = (inp.get("archive_path") or f"archive/{stem} Done.md").strip()
    count = len(rows)
    async with _Recording(
        state,
        thread_id,
        "archive_completed_tasks",
        ["notes", "tasks", "calendar", "search"],
        count,
        lambda k: f"Archived {k} completed task{'' if k == 1 else 's'} from {source}",
    ) as rec:
        if not state.store.exists(archive_path):
            await _write_note(
                state,
                archive_path,
                f'---\ntitle: "{stem} — Done"\n---\n\n# {stem} — Done\n',
                create=True,
            )
            rec.wrote(archive_path)
        await move_tasks(state, [row["id"] for row in rows], archive_path)
    if rec.failed:
        return rec.failed
    return await _record(
        state,
        thread_id,
        "archive_completed_tasks",
        f"Archived {count} completed task{'' if count == 1 else 's'} from {source}",
        sorted({source, archive_path}),
        ["notes", "tasks", "calendar", "search"],
        {"archived": count, "archive_path": archive_path},
    )


async def roll_over_tasks(state, thread_id, inp) -> dict:
    """Carry overdue open tasks forward to today (or a chosen date)."""
    target = parse_user_date(inp.get("to") or local_today(state), "Roll-over date")
    clauses = ["status != 'done'", "due IS NOT NULL", "due < ?"]
    values: list[Any] = [target]
    if inp.get("path"):
        clause, bound = _task_scope_clause(inp["path"])
        clauses.append(clause)
        values.extend(bound)
    rows = await state.db.execute_fetchall(
        f"SELECT id, path, meta_line, text FROM tasks WHERE {' AND '.join(clauses)}", values
    )
    if not rows:
        note = f"No open task is due before {target}."
        if inp.get("path"):
            clause, bound = _task_scope_clause(inp["path"])
            has_tasks = await state.db.execute_fetchall(
                f"SELECT 1 FROM tasks WHERE {clause} LIMIT 1", bound
            )
            note = (
                f"No open task in {inp['path']} is due before {target}."
                if has_tasks
                else await _empty_tasks_note(state, inp)  # the scope itself is wrong
            )
        return {"rolled": 0, "to": target, "note": note}
    _refuse_bare_tasks(rows)
    count = len(rows)
    async with _Recording(
        state,
        thread_id,
        "roll_over_tasks",
        ["notes", "tasks", "calendar", "search"],
        count,
        lambda k: f"Rolled {k} overdue task{'' if k == 1 else 's'} forward to {target}",
    ) as rec:
        for row in rows:
            rec.current = row["text"]
            task = await update_task_record(state, row["id"], due=target)
            rec.done(task["path"])
    if rec.failed:
        return rec.failed
    return await _record(
        state,
        thread_id,
        "roll_over_tasks",
        f"Rolled {count} overdue task{'' if count == 1 else 's'} forward to {target}",
        sorted(rec.paths),
        ["notes", "tasks", "calendar", "search"],
        {"rolled": count, "to": target},
    )


async def scaffold_project(state, thread_id, inp) -> dict:
    """Stand up a project workspace in one call: a folder, its task board, and a
    few seeded notes, committed together."""
    name = (inp.get("name") or "").strip().strip("/")
    if not name:
        raise NoteError(400, "Give the project a name")
    parent = (inp.get("parent") or "").strip().strip("/")
    folder = f"{parent}/{name}" if parent else name
    if folder in state.store.list_folders():
        raise NoteError(409, "That folder already exists")
    state.store.create_folder(folder)

    paths = [f"{folder}/.gitkeep"]
    board_path = task_boards.next_board_path(
        folder, {note.path for note in state.store.list_notes()}
    )
    await _write_note(state, board_path, task_boards.BOARD_TEMPLATE, create=True)
    paths.append(board_path)

    seeds = inp.get("seed_notes") or ["Brief", "Log", "Resources"]
    for seed in seeds:
        title = str(seed).strip()
        if not title:
            continue
        note_path = f"{folder}/{title}.md"
        if state.store.exists(note_path):
            continue
        await _write_note(
            state, note_path, f'---\ntitle: "{title}"\n---\n\n# {title}\n', create=True
        )
        paths.append(note_path)

    return await _record(
        state,
        thread_id,
        "scaffold_project",
        f"Scaffolded project “{name}”",
        paths,
        ["notes", "search", "tasks"],
        {"folder": folder, "board": board_path, "notes": paths[2:]},
        navigate=f"/tasks/{folder}",
    )


async def board_digest(state, thread_id, inp) -> dict:
    """A read-only rollup of one board: counts by status, what's overdue/due,
    and what's next. Defaults to the board of the folder in use."""
    board = (inp.get("path") or "").strip()
    if board:
        requested, board = board, await _resolve_board_arg(state, board)
        if not state.store.exists(board):
            # _resolve_board_arg passes a miss through untouched, and a digest
            # of a path with no rows is an all-zero, healthy-looking board.
            boards = await task_boards.board_map(state.db)
            known = ", ".join(sorted(folder or note for folder, note in boards.items()))
            raise NoteError(
                404,
                f"No board named “{requested}”. Boards: {known or 'none yet'}; "
                "or give a note path.",
            )
    else:
        board = await task_boards.nearest_board(
            state.db, await _context_note(state, thread_id)
        ) or "Tasks Inbox.md"
    today = local_today(state)
    rows = [
        dict(row)
        for row in await state.db.execute_fetchall(
            "SELECT text, status, due FROM tasks WHERE path = ?", (board,)
        )
    ]
    counts = {"todo": 0, "in_progress": 0, "done": 0}
    overdue: list[dict] = []
    due_today: list[dict] = []
    upcoming: list[dict] = []
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
        if row["status"] == "done" or not row["due"]:
            continue
        if row["due"] < today:
            overdue.append(row)
        elif row["due"] == today:
            due_today.append(row)
        else:
            upcoming.append(row)
    digest = {
        "board": board,
        "total": len(rows),
        "counts": counts,
        "open": counts["todo"] + counts["in_progress"],
        "overdue": overdue,
        "due_today": due_today,
        "next": sorted(upcoming, key=lambda r: r["due"])[:5],
    }
    if not rows:
        digest["note"] = f"{board} has no tasks at all."
    return digest


async def generate_daily_briefing(state, thread_id, inp) -> dict:
    """Compose the day's brief: overdue/due tasks, today's events and event
    reminders, and open counts per board. Read-only — the caller writes the
    prose."""
    from .tools import get_digest_data  # local import avoids a circular module load

    days = max(1, min(int(inp.get("days") or 1), 14))
    digest = await get_digest_data(state, thread_id, {"days": days})
    boards = await task_boards.board_map(state.db)
    board_open = []
    for _folder, path in sorted(boards.items()):
        open_rows = await state.db.execute_fetchall(
            "SELECT 1 FROM tasks WHERE path = ? AND status != 'done'", (path,)
        )
        if open_rows:
            board_open.append({"board": path, "open": len(open_rows)})
    return {
        "today": digest["today"],
        "period_days": digest["period_days"],
        "overdue": digest["tasks"]["overdue"],
        "due_today": digest["tasks"]["due_today"],
        "upcoming": digest["tasks"]["upcoming"],
        "events_today": digest["events_today"],
        "event_reminders": digest["event_reminders"],
        "boards": board_open,
    }


# notify_user reaches the user's phone with model-written text, so how often it
# may do so is enforced here rather than left to the prompt: a scheduled pass
# that read a poisoned note could otherwise push any number of messages.
NOTIFY_TITLE_MAX = 120
NOTIFY_PER_THREAD = 2  # distinct messages per run (or conversation) within the window
NOTIFY_THREAD_WINDOW = timedelta(minutes=10)
NOTIFY_PER_DAY = 10  # distinct messages per local day, across every run


class NotifyLog:
    """notify_user messages delivered today, by dedup key, so the daily cap
    counts distinct messages and survives a restart. A transient ledger in
    data_dir, shaped like alerts.TaskReminderLog; only today's keys are kept."""

    def __init__(self, data_dir: Path):
        self.path = data_dir / "omnia-notify-log.json"

    def sent_on(self, day: str) -> dict[str, str]:
        # A corrupt ledger is set aside and read as empty: losing the count
        # lets a day's cap restart, which beats blocking every notification.
        value = read_json(self.path, {}, skip_corrupt=True)
        if not isinstance(value, dict):
            return {}
        return {key: at for key, at in value.items() if str(at).startswith(day)}

    def mark(self, key: str, now: datetime) -> None:
        day = now.date().isoformat()
        atomic_json(self.path, {**self.sent_on(day), key: now.isoformat()})


def _recent_notifies(state, scope: str, now: datetime) -> list[datetime]:
    """This run's sends inside the window, pruned in place. An unattended run
    (scheduler.run_scope) is its own scope; any other turn counts against its
    conversation. Kept on `state` rather than on disk: a restart ends a run
    anyway."""
    recent = getattr(state, "_notify_recent", None)
    if recent is None:
        recent = state._notify_recent = {}
    for key in list(recent):
        recent[key] = [at for at in recent[key] if now - at < NOTIFY_THREAD_WINDOW]
        if not recent[key] and key != scope:
            del recent[key]  # a finished run's key would otherwise linger
    return recent.setdefault(scope, [])


async def notify_user(state, thread_id, inp) -> dict:
    """Surface a notification to the user. This is the channel a scheduled or
    proactive run uses to reach them: it shows as an in-app toast when the app
    is open, and — if the user enabled notifications — is also delivered as a
    Web Push so it lands on their device when the app is closed."""
    from .. import push_service

    title = " ".join(str(inp.get("title") or "").split())[:NOTIFY_TITLE_MAX].rstrip()
    if not title:
        raise NoteError(400, "A notification needs a title")
    body = str(inp.get("body") or "").strip()[:500] or None
    queue = getattr(state, "alerts", None)
    if queue is None:
        return {"delivered": False, "reason": "no alert channel available"}
    # Content key, not a timestamp. AlertQueue.add dedups on `key`, but a
    # per-call timestamp made every key unique, so a scheduled run repeating
    # the same nudge every hour stacked a fresh copy each time. Scoping the
    # digest by local date coalesces repeats within a day while still letting
    # a genuinely recurring reminder fire again tomorrow. The push `tag` is the
    # same value, so the OS replaces the old notification instead of piling up.
    now = datetime.now(schedule_tz(state))
    digest = hashlib.sha1(f"{title}|{body or ''}".encode()).hexdigest()[:12]
    key = f"omnia:{digest}:{now.date().isoformat()}"
    ledger = NotifyLog(state.settings.data_dir)
    sent_today = ledger.sent_on(now.date().isoformat())
    run = notify_run.get()
    recent = _recent_notifies(state, run or thread_id, now)
    # A repeat of a message already delivered today is coalesced in-app and
    # replaces the OS notification, so only a new message counts against a cap.
    fresh = key not in sent_today
    refusal = None
    if fresh and len(sent_today) >= NOTIFY_PER_DAY:
        refusal = (
            f"Not sent: {NOTIFY_PER_DAY} notifications already went out today, the "
            "daily limit. Put this in the reply instead."
        )
    elif fresh and len(recent) >= NOTIFY_PER_THREAD:
        refusal = (
            f"Not sent: this {'run' if run else 'conversation'} already sent "
            f"{NOTIFY_PER_THREAD} notifications in the last 10 minutes. Fold anything "
            "else into one message later, or put it in the reply."
        )
    if refusal:
        log.info("notify_user capped in %s: %s", run or f"thread {thread_id}", title)
        return {"delivered": False, "title": title, "note": refusal}
    if fresh:  # counted before the await, so a concurrent call sees it
        recent.append(now)
        ledger.mark(key, now)
    queue.add([{"key": key, "title": title, "body": body, "source": "omnia"}])
    await push_service.deliver(
        state, [{"title": title, "body": body, "url": "/omnia", "source": "omnia", "tag": key}]
    )
    return {"delivered": True, "title": title, "body": body}


async def schedule_agent_run(state, thread_id, inp) -> dict:
    instruction = " ".join(str(inp.get("instruction") or "").split()).strip()
    if not instruction:
        raise NoteError(400, "Give the instruction to run on a schedule")
    cadence = clean_cadence(
        inp.get("frequency"), inp.get("interval"), inp.get("at"), inp.get("weekday")
    )
    store = ScheduleStore(state.settings.vault_dir)
    tz = schedule_tz(state)
    schedule = store.add(instruction, cadence, tz, datetime.now(tz))
    state.autocommit.touch()
    return await _record(
        state,
        thread_id,
        "schedule_agent_run",
        f"Scheduled “{instruction}” ({cadence['frequency']})",
        [SCHEDULE_PATH],
        ["calendar"],
        {"schedule": schedule},
    )


async def manage_scheduled_runs(state, thread_id, inp) -> dict:
    store = ScheduleStore(state.settings.vault_dir)
    operation = str(inp.get("operation") or "list").lower()
    if operation == "list":
        return {"schedules": store.list()}
    schedule_id = inp.get("schedule_id")
    if not schedule_id:
        raise NoteError(400, "Pass schedule_id — get it from the list operation")
    if operation == "cancel":
        removed = store.remove(schedule_id)
        state.autocommit.touch()
        return await _record(
            state,
            thread_id,
            "manage_scheduled_runs",
            f"Cancelled scheduled run “{removed.get('instruction', '')}”",
            [SCHEDULE_PATH],
            ["calendar"],
            {"cancelled": schedule_id},
        )
    if operation in ("pause", "resume"):
        schedule = store.update(schedule_id, paused=(operation == "pause"))
        state.autocommit.touch()
        return await _record(
            state,
            thread_id,
            "manage_scheduled_runs",
            f"{operation.title()}d a scheduled run",
            [SCHEDULE_PATH],
            ["calendar"],
            {"schedule": schedule},
        )
    raise NoteError(400, "operation must be list, pause, resume, or cancel")


async def get_calendar_range(state, thread_id, inp) -> dict:
    try:
        start = date.fromisoformat(inp["start"])
        end = date.fromisoformat(inp["end"])
    except ValueError as exc:
        raise NoteError(400, "Calendar dates must be YYYY-MM-DD") from exc
    if end < start or (end - start).days > 366:
        raise NoteError(400, "Calendar range must be inclusive and at most one year")
    rows = await state.db.execute_fetchall(
        """SELECT path, title, created, modified FROM notes
           WHERE substr(created,1,10) BETWEEN ? AND ?
              OR substr(modified,1,10) BETWEEN ? AND ?""",
        (start.isoformat(), end.isoformat(), start.isoformat(), end.isoformat()),
    )
    task_rows = await state.db.execute_fetchall(
        """SELECT * FROM tasks WHERE due BETWEEN ? AND ?
           OR substr(created_at,1,10) BETWEEN ? AND ?
           OR substr(started_at,1,10) BETWEEN ? AND ?
           OR substr(completed_at,1,10) BETWEEN ? AND ?
           OR substr(reopened_at,1,10) BETWEEN ? AND ?""",
        (
            start.isoformat(),
            end.isoformat(),
            start.isoformat(),
            end.isoformat(),
            start.isoformat(),
            end.isoformat(),
            start.isoformat(),
            end.isoformat(),
            start.isoformat(),
            end.isoformat(),
        ),
    )
    task_values = [public_task(dict(row)) for row in task_rows]
    active_ids = {task["id"] for task in task_values}
    for task_id, tombstone in state.task_tombstones.all().items():
        if task_id not in active_ids:
            task_values.append(
                public_task(
                    {
                        **tombstone,
                        "tombstone": True,
                        "due_visible": False,
                    }
                )
            )
    task_events: list[dict[str, Any]] = []
    local_tz = user_tz(state)
    for task in task_values:
        for kind, key in (
            ("created", "created_at"),
            ("started", "started_at"),
            ("completed", "completed_at"),
            ("reopened", "reopened_at"),
        ):
            timestamp = task.get(key)
            if not timestamp:
                continue
            try:
                milestone_day = (
                    datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
                    .astimezone(local_tz)
                    .date()
                    .isoformat()
                )
            except ValueError:
                continue
            if start.isoformat() <= milestone_day <= end.isoformat():
                task_events.append(
                    {
                        "type": kind,
                        "date": milestone_day,
                        "timestamp": timestamp,
                        "task": task,
                    }
                )
        if (
            task.get("due")
            and not task.get("tombstone")
            and start.isoformat() <= task["due"] <= end.isoformat()
        ):
            task_events.append(
                {
                    "type": "due",
                    "date": task["due"],
                    "timestamp": None,
                    "task": task,
                }
            )
    daily_notes: list[str] = []
    cursor = start
    while cursor <= end:
        day = cursor.isoformat()
        if state.store.exists(f"daily/{day}.md"):
            daily_notes.append(day)
        cursor += timedelta(days=1)
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "daily_notes": daily_notes,
        "notes": [dict(row) for row in rows],
        "task_events": sorted(
            task_events,
            key=lambda event: (event["date"], event["type"], event["task"]["text"]),
        ),
        "due_tasks": [
            task
            for task in task_values
            if task.get("due")
            and not task.get("tombstone")
            and start.isoformat() <= task["due"] <= end.isoformat()
        ],
        "activity_hours": state.activities.range_totals(start, end),
        "events": state.events.occurrences(start, end),
    }


def _activity_day(value: Any) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise NoteError(400, "Activity date must be YYYY-MM-DD") from exc


def _hours(value: Any) -> float:
    hours = float(value)
    if not 0 < hours <= 24:
        raise NoteError(400, "Hours must be greater than zero and at most 24")
    return hours


async def get_activity_summary(state, thread_id, inp) -> dict:
    day = _activity_day(inp.get("date") or local_now(state).date())
    return state.activities.summary(day)


async def add_activity(state, thread_id, inp) -> dict:
    day = _activity_day(inp["date"])
    activity = " ".join(inp["activity"].split()).strip()
    if not activity:
        raise NoteError(400, "Activity name is required")
    entry = state.activities.add(day, activity, _hours(inp["hours"]))
    return await _record(
        state,
        thread_id,
        "add_activity",
        f"Logged {entry['hours']}h of {entry['activity']}",
        [ACTIVITY_PATH],
        ["activity", "calendar"],
        {"entry": entry, "summary": state.activities.summary(day)},
    )


async def update_activity(state, thread_id, inp) -> dict:
    entry = state.activities.update(
        inp["entry_id"],
        activity=(" ".join(inp["activity"].split()).strip() if "activity" in inp else None),
        hours=(_hours(inp["hours"]) if "hours" in inp else None),
        expected_updated_at=inp["expected_updated_at"],
    )
    return await _record(
        state,
        thread_id,
        "update_activity",
        f"Updated activity “{entry['activity']}”",
        [ACTIVITY_PATH],
        ["activity", "calendar"],
        {"entry": entry, "summary": state.activities.summary(_activity_day(entry["date"]))},
    )


async def delete_activity(state, thread_id, inp) -> dict:
    entry = state.activities.delete(inp["entry_id"], inp["expected_updated_at"])
    return await _record(
        state,
        thread_id,
        "delete_activity",
        f"Deleted activity “{entry['activity']}”",
        [ACTIVITY_PATH],
        ["activity", "calendar"],
        {"entry": entry},
    )


async def get_note_history(state, thread_id, inp) -> dict:
    path = inp["path"]
    state.store.resolve(path)
    async with state.vault._git_lock:
        versions = await asyncio.to_thread(
            state.vault.history_sync, path, min(int(inp.get("limit", 100)), 300)
        )
    return {"path": path, "versions": versions}


async def inspect_note_version(state, thread_id, inp) -> dict:
    path = inp["path"]
    source_path = inp.get("version_path") or path
    state.store.resolve(path)
    state.store.resolve(source_path)
    async with state.vault._git_lock:
        old = await asyncio.to_thread(
            state.vault.commit_content_sync, inp["sha"], source_path
        )
    if old is None:
        raise NoteError(404, "That version does not contain this note")
    if inp.get("format", "content") == "diff":
        current = state.store.read(path)
        return {
            "path": path,
            "sha": inp["sha"],
            "diff": "".join(
                difflib.unified_diff(
                    old.splitlines(keepends=True),
                    current.splitlines(keepends=True),
                    fromfile=f"{path}@{inp['sha'][:8]}",
                    tofile=f"{path}@current",
                )
            ),
        }
    return {"path": path, "sha": inp["sha"], "content": old}


async def restore_note_version(state, thread_id, inp) -> dict:
    path = inp["path"]
    source_path = inp.get("version_path") or path
    state.store.resolve(path)
    state.store.resolve(source_path)
    async with state.vault._git_lock:
        content = await asyncio.to_thread(
            state.vault.commit_content_sync, inp["sha"], source_path
        )
    if content is None:
        raise NoteError(404, "That version does not contain this note")
    mode = inp.get("mode", "copy")
    if mode == "replace":
        current = _require_revision(state, path, inp.get("base_revision"))
        target = path
        normalized = await _write_note(state, path, content, previous=current)
    elif mode == "copy":
        target = _unique_path(
            state,
            inp.get("target_path")
            or str(Path(path).with_name(f"{Path(path).stem} restored.md")),
        )
        # As in /api/history/restore: a copy named after the note keeps that
        # name's characters; a chosen target_path is a new name.
        normalized = await _write_note(
            state, target, content, create=True, check_name=bool(inp.get("target_path"))
        )
    else:
        raise NoteError(400, "Restore mode must be copy or replace")
    return await _record(
        state,
        thread_id,
        "restore_note_version",
        f"Restored {path} from {inp['sha'][:8]}{' as ' + target if mode == 'copy' else ''}",
        [target],
        ["notes", "search", "tasks", "calendar"],
        {"path": target, "revision": content_hash(normalized), "mode": mode},
    )


async def list_trash(state, thread_id, inp) -> dict:
    items = state.trash.list()
    if not items:
        return {
            "items": [],
            "retention_days": 30,
            "note": "The trash is empty; deleted items older than 30 days are purged.",
        }
    return {"items": items, "retention_days": 30}


async def inspect_trash(state, thread_id, inp) -> dict:
    return state.trash.preview(inp["item_id"])


async def restore_trash(state, thread_id, inp) -> dict:
    item = state.trash.preview(inp["item_id"])
    trash_path = f"{NoteStore.INTERNAL_DIR}/trash/{item['id']}"
    if item["kind"] in TrashStore.PAPER_KINDS:
        # Same path as the Paper and Trash routes: the page list, tombstones
        # and handwriting index come back with the files, in place only.
        target = (inp.get("target_path") or "").strip("/")
        if target and target != item["original_path"]:
            raise NoteError(400, "Paper items restore to their original location")
        restored = await restore_paper_item(state, inp["item_id"])
        paths = [trash_path, restored["path"]]
        if restored["kind"] == "page" and restored.get("notebook"):
            # The page is re-listed in its notebook's manifest; commit that
            # with it so an undo does not leave a listed page with no files.
            paths.append(f"{restored['notebook']}/notebook.json")
        return await _record(
            state,
            thread_id,
            "restore_trash",
            f"Restored {restored['path']} from Trash",
            paths,
            ["trash"],
            restored,
        )
    restored = state.trash.restore(inp["item_id"], inp.get("target_path"))
    for path in restored["notes"]:
        await state.indexer.index_note(path, state.store.read(path))
    paths = [trash_path, *restored["notes"]]
    return await _record(
        state,
        thread_id,
        "restore_trash",
        f"Restored {restored['path']} from Trash",
        paths,
        ["notes", "search", "tasks", "calendar", "trash"],
        restored,
    )


async def get_appearance(state, thread_id, inp) -> dict:
    return appearance_payload(_config(state).get("appearance"))


def _editable_appearance(config: dict[str, Any], tool: str) -> dict[str, Any]:
    """The stored appearance to edit and write back — salvaged, never reset to
    defaults, so one bad value cannot cost the saved themes."""
    raw = config.get("appearance")
    if not isinstance(raw, dict):
        return _appearance(AppearanceSettingsBody())
    current, counts = salvage_appearance(raw)
    if counts["dropped"]:
        log.warning(
            "%s writes appearance without %d unusable stored entries",
            tool,
            counts["dropped"],
        )
    return current


async def set_appearance(state, thread_id, inp) -> dict:
    config = _config(state)
    current = _editable_appearance(config, "set_appearance")
    for key in ("theme", "font", "intoxicate", "custom_theme"):
        if key in inp:
            current[key] = inp[key]
    if "theme" in inp or "custom_theme" in inp:
        current["active_saved_theme_id"] = None
    normalized = _appearance(AppearanceSettingsBody.model_validate(current))
    config["appearance"] = normalized
    _write_config(state, config)
    return await _record(
        state,
        thread_id,
        "set_appearance",
        "Updated appearance",
        [CONFIG_PATH],
        ["appearance", "settings"],
        {"appearance": normalized},
    )


async def manage_saved_theme(state, thread_id, inp) -> dict:
    config = _config(state)
    current = _editable_appearance(config, "manage_saved_theme")
    saved = list(current.get("saved_themes") or [])
    op = inp["operation"]
    theme_id = inp.get("theme_id")
    existing = next((theme for theme in saved if theme["id"] == theme_id), None)
    if op == "create":
        if not inp.get("name") or not inp.get("theme"):
            raise NoteError(400, "Creating a saved theme needs a name and palette")
        theme_id = secrets.token_hex(8)
        saved.append({"id": theme_id, "name": inp["name"], "theme": inp["theme"]})
    elif not existing:
        raise NoteError(404, "Saved theme not found")
    elif op == "rename":
        existing["name"] = inp["name"]
    elif op == "update":
        existing["theme"] = inp["theme"]
        if inp.get("name"):
            existing["name"] = inp["name"]
    elif op == "apply":
        current["theme"] = "custom"
        current["custom_theme"] = existing["theme"]
        current["active_saved_theme_id"] = theme_id
    elif op == "delete":
        saved = [theme for theme in saved if theme["id"] != theme_id]
        if current.get("active_saved_theme_id") == theme_id:
            current["active_saved_theme_id"] = None
    else:
        raise NoteError(400, "Unknown saved-theme operation")
    current["saved_themes"] = saved
    normalized = _appearance(AppearanceSettingsBody.model_validate(current))
    config["appearance"] = normalized
    _write_config(state, config)
    # `verbs` was only ever a local of set_appearance, so this raised NameError
    # on every call — after _write_config had already persisted the change,
    # leaving a silent partial success with no commit and no undo.
    verbs = {
        "create": "Created",
        "rename": "Renamed",
        "apply": "Applied",
        "update": "Updated",
        "delete": "Deleted",
    }
    return await _record(
        state,
        thread_id,
        "manage_saved_theme",
        f"{verbs.get(op, 'Changed')} saved theme",
        [CONFIG_PATH],
        ["appearance", "settings"],
        {"appearance": normalized, "theme_id": theme_id},
    )


async def get_timezone(state, thread_id, inp) -> dict:
    return {"timezone": state.timezone}


async def set_timezone(state, thread_id, inp) -> dict:
    timezone_name = inp["timezone"]
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise NoteError(400, "Unknown IANA timezone") from exc
    config = _config(state)
    config["timezone"] = timezone_name
    _write_config(state, config)
    state.timezone = timezone_name
    return await _record(
        state,
        thread_id,
        "set_timezone",
        f"Set timezone to {timezone_name}",
        [CONFIG_PATH],
        ["settings", "calendar"],
        {"timezone": timezone_name},
    )


async def get_backup_status(state, thread_id, inp) -> dict:
    pusher = state.backup
    if pusher is None:
        return {"configured": False}
    return {
        "configured": True,
        "remote": _redact_remote(pusher.url),
        "interval": pusher.interval,
        "last_push": pusher.last_push,
        "last_error": pusher.last_error,
    }


async def push_backup(state, thread_id, inp) -> dict:
    if state.backup is None:
        raise NoteError(400, "No backup remote is configured")
    result = await state.backup.push()
    return await _record(
        state,
        thread_id,
        "push_backup",
        "Pushed backup",
        [],
        ["backup"],
        result,
        undoable=False,
    )


async def rebuild_search_index(state, thread_id, inp) -> dict:
    await state.indexer.sync_all(force=True)
    notes = state.store.list_notes()
    documents = [(note.path, state.store.read(note.path)) for note in notes]
    # Handwriting is part of the same search surface, so a rebuild that skipped
    # it would leave Paper findable by keyword but not by meaning.
    paper_index = getattr(state, "paper_index", None)
    paper_pages = 0
    if paper_index is not None:
        paper_pages = await paper_index.sync_all(force=True)
        documents += await paper_index.semantic_documents()
    # Conversation recall too: nothing else re-derives threads_fts on demand.
    # Under the write lock, as in /api/index/rebuild.
    messages = 0
    thread_index = getattr(state, "thread_index", None)
    threads = getattr(state, "threads", None)
    if thread_index is not None and threads is not None:
        conversations = threads.list(include_messages=True)
        async with state.indexer.write_lock:
            messages = thread_index.rebuild(conversations)
    semantic_errors = 0
    if state.semantic.available:
        for path, content in documents:
            try:
                await state.semantic.index_note(path, content)
            except Exception:
                semantic_errors += 1
    return await _record(
        state,
        thread_id,
        "rebuild_search_index",
        "Rebuilt search index",
        [],
        ["search"],
        {
            "notes_indexed": len(notes),
            "paper_pages_indexed": paper_pages,
            "conversation_messages_indexed": messages,
            "semantic_available": state.semantic.available,
            "semantic_errors": semantic_errors,
        },
        undoable=False,
    )


async def get_security_overview(state, thread_id, inp) -> dict:
    return {
        "sessions": state.auth.list_sessions(None),
        "failed_logins": state.auth.list_failed_logins(),
        "note": "Locations are approximate. No credentials or session tokens are exposed.",
    }


# A reminder given only a day fires at this local time. Midnight woke the user
# or got buried overnight; due-relative task reminders already default to it.
REMINDER_TIME = "09:00"


def _is_bare_date(value: str) -> bool:
    return "T" not in value and ":" not in value


def _event_zone(state, name: str | None) -> ZoneInfo:
    """The zone an event's naive local times are in: its own, else the user's."""
    if not name:
        return user_tz(state)
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise NoteError(400, "Unknown event timezone") from exc


def _moved(event: dict, new_start: str, tz: ZoneInfo) -> tuple[str, str]:
    """(start, end) for a timed event moved to `new_start`, keeping its length.

    A bare date moves the day and keeps the time of day: "push my dentist
    appointment to Friday" used to land at 00:00, reminder and all."""
    old_start = datetime.fromisoformat(event["start"])
    old_end = datetime.fromisoformat(event["end"])
    try:
        if _is_bare_date(new_start):
            start = datetime.combine(date.fromisoformat(new_start), old_start.time())
        else:
            start = parse_user_datetime(new_start, tz)
    except ValueError as exc:
        raise NoteError(400, "The new start must be YYYY-MM-DD or YYYY-MM-DDTHH:MM") from exc
    end = start + (old_end - old_start)
    return start.isoformat(timespec="minutes"), end.isoformat(timespec="minutes")


def _event_values(state, inp: dict) -> dict:
    note_path = inp.get("note_path")
    if note_path and not state.store.exists(note_path):
        raise NoteError(404, "Linked note not found")
    start, end = str(inp["start"]), inp.get("end")
    all_day = inp.get("all_day")
    if all_day is None:
        all_day = _is_bare_date(start)
    if not all_day:
        # Timed values are naive local in the event's zone. An explicit offset
        # is converted rather than dropped, and a missing end is an hour, as
        # capture assumes — `end or start` made it a 400.
        tz = _event_zone(state, inp.get("timezone"))
        try:
            start_dt = parse_user_datetime(start, tz)
            end_dt = parse_user_datetime(end, tz) if end else start_dt + timedelta(hours=1)
        except ValueError as exc:
            raise NoteError(400, "Timed event values must be ISO local date-times") from exc
        start = start_dt.isoformat(timespec="minutes")
        end = end_dt.isoformat(timespec="minutes")
    return state.events.normalize(
        title=inp["title"],
        start=start,
        end=end,
        all_day=bool(all_day),
        timezone_name=inp.get("timezone") or state.timezone,
        notes=inp.get("notes"),
        note_path=note_path,
        recurrence=inp.get("recurrence"),
        reminders=inp.get("reminders"),
        app_timezone=state.timezone,
    )


async def list_events(state, thread_id, inp) -> dict:
    try:
        start = date.fromisoformat(inp["start"])
        end = date.fromisoformat(inp["end"])
    except ValueError as exc:
        raise NoteError(400, "Event dates must be YYYY-MM-DD") from exc
    if end < start or (end - start).days > 366:
        raise NoteError(400, "Event range must be at most one year")
    events = state.events.occurrences(start, end)
    if not events:
        note = _empty_note(
            f"No events between {start} and {end}.",
            len(state.events.all()),
            "calendar events",
            "Widen the range; task due dates are not events (list_tasks with due_start/due_end).",
        )
        return {"events": [], "note": note}
    return {"events": events}


async def create_event(state, thread_id, inp) -> dict:
    event = state.events.add(_event_values(state, inp))
    state.autocommit.touch()
    return await _record(
        state,
        thread_id,
        "create_event",
        f"Created event “{event['title']}”",
        [EVENTS_PATH],
        ["calendar"],
        {"event": event},
    )


async def update_event(state, thread_id, inp) -> dict:
    existing = state.events.get(inp["event_id"])
    merged = {
        key: inp[key] if key in inp else existing.get(key)
        for key in (
            "title",
            "start",
            "end",
            "all_day",
            "notes",
            "note_path",
            "recurrence",
            "reminders",
        )
    }
    # Not the stored zone: events float in the app zone, and re-reading the
    # stored one after a settings change shifted a title-only edit's time.
    merged["timezone"] = inp.get("timezone")
    if (
        "start" in inp
        and "end" not in inp
        and not existing.get("all_day")
        and not merged["all_day"]
    ):
        # Keeping the stored end stretched a moved lecture across two days, or
        # put it before its new start.
        merged["start"], merged["end"] = _moved(
            existing, str(inp["start"]), _event_zone(state, merged["timezone"])
        )
    event = state.events.update(
        inp["event_id"],
        _event_values(state, merged),
        inp["expected_revision"],
    )
    state.autocommit.touch()
    return await _record(
        state,
        thread_id,
        "update_event",
        f"Updated event “{event['title']}”",
        [EVENTS_PATH],
        ["calendar"],
        {"event": event},
    )


async def delete_event(state, thread_id, inp) -> dict:
    event = state.events.delete(inp["event_id"], inp["expected_revision"])
    state.autocommit.touch()
    return await _record(
        state,
        thread_id,
        "delete_event",
        f"Deleted event “{event['title']}”",
        [EVENTS_PATH],
        ["calendar"],
        {"event": event},
    )


async def create_reminder(state, thread_id, inp) -> dict:
    """A standalone nudge that alerts the user at a chosen time.

    Stored as a calendar event carrying a reminder offset, so delivery flows
    through the same 60-second reminder ticker events use — no separate
    machinery — and it can be listed, updated, or deleted like any event."""
    text = " ".join(str(inp.get("text") or "").split()).strip()
    if not text:
        raise NoteError(400, "Reminder text is required")
    when = str(inp.get("when") or "").strip()
    if not when:
        raise NoteError(400, "A reminder needs a date or time (when)")
    # A bare YYYY-MM-DD is a reminder that morning, not an all-day event: an
    # all-day one fired its [0] alert at midnight.
    if _is_bare_date(when):
        when = f"{when}T{REMINDER_TIME}"
    try:
        start_dt = parse_user_datetime(
            when, _event_zone(state, inp.get("timezone"))
        )
    except ValueError as exc:
        raise NoteError(
            400, "Reminder time must be YYYY-MM-DD or YYYY-MM-DDTHH:MM"
        ) from exc
    # The event store insists a timed event outlast its start, so a reminder
    # gets a short default block rather than a zero-length one.
    end = (start_dt + timedelta(minutes=15)).isoformat(timespec="minutes")
    reminders = inp.get("reminders") or [0]  # default: alert at the reminder's own time
    values = state.events.normalize(
        title=text,
        start=start_dt.isoformat(timespec="minutes"),
        end=end,
        all_day=False,
        timezone_name=inp.get("timezone") or state.timezone,
        notes=None,
        note_path=inp.get("note_path"),
        recurrence=inp.get("recurrence"),
        reminders=reminders,
        app_timezone=state.timezone,
    )
    event = state.events.add(values)
    state.autocommit.touch()
    return await _record(
        state,
        thread_id,
        "create_reminder",
        f"Set a reminder: “{text}”",
        [EVENTS_PATH],
        ["calendar"],
        {"event": event, "reminder": True},
    )


REVISION = {
    "type": "string",
    "description": "Exact revision returned by read_notes or get_outline; required for "
    "concurrency safety.",
}
# These overrides replaced the originals in tools.py and dropped `rationale`
# from the schema, while the executors kept reading it — so every direct write
# landed in the action log with no explanation of why it happened.
RATIONALE = {"type": "string", "description": "One short sentence: why this change."}
DATE = {"type": "string", "description": "YYYY-MM-DD"}
STATUS = {"type": "string", "enum": ["todo", "in_progress", "done"]}
TASK_SCOPE = {
    "type": "string",
    "description": "Note path, board, or folder — a folder like \"School\" matches every task in it, its board included.",
}
PALETTE = {
    "type": "object",
    "description": "A custom theme with mode light/dark and all EXSCALIA color variables.",
}
RECURRENCE = {
    "type": "object",
    "properties": {
        "frequency": {"type": "string", "enum": ["daily", "weekly", "monthly", "yearly"]},
        "interval": {"type": "integer"},
        "weekdays": {"type": "array", "items": {"type": "integer"}},
        "count": {"type": "integer"},
        "until": DATE,
        "exceptions": {"type": "array", "items": DATE},
    },
    "required": ["frequency"],
}
EVENT_TIMEZONE = {
    "type": "string",
    "description": "IANA zone the start and end are given in, only when it is not the app's. The times are converted to the app zone, where events are kept.",
}


# --- media tracker ---------------------------------------------------------


# Image URLs that lookup_media / search_media_images actually returned, per
# thread. log_media and set_media_art run without a card, so the URL they
# download must be one the conversation was shown — never one the model made
# up or read in a note.
_OFFERED_IMAGE_URLS: dict[str, list[str]] = {}
OFFERED_URLS_PER_THREAD = 300
OFFERED_URL_THREADS = 64


def _offer_image_urls(thread_id: str, urls) -> None:
    fresh = [u.strip() for u in urls if isinstance(u, str) and u.strip()]
    if not fresh:
        return
    # Pop and reinsert keeps the dict in least-recently-offered order.
    offered = [u for u in _OFFERED_IMAGE_URLS.pop(thread_id, []) if u not in fresh]
    _OFFERED_IMAGE_URLS[thread_id] = (offered + fresh)[-OFFERED_URLS_PER_THREAD:]
    while len(_OFFERED_IMAGE_URLS) > OFFERED_URL_THREADS:
        _OFFERED_IMAGE_URLS.pop(next(iter(_OFFERED_IMAGE_URLS)))


def _image_url_offered(state, thread_id: str, url: str) -> bool:
    url = (url or "").strip()
    if not url:
        return False
    if url in _OFFERED_IMAGE_URLS.get(thread_id, ()):
        return True
    # After a restart the list is empty; galleries persisted on the thread
    # still show what the user was offered.
    if not hasattr(state, "threads"):
        return False
    try:
        thread = state.threads.get(thread_id)
    except Exception:
        return False
    for message in (thread or {}).get("messages") or []:
        for gallery in message.get("galleries") or []:
            for image in (gallery or {}).get("images") or []:
                if isinstance(image, dict) and image.get("url") == url:
                    return True
    return False


async def lookup_media(state, thread_id, inp) -> dict:
    keys = media_lookup.load_media_keys(state.settings.data_dir)
    result = await media_lookup.search_media(inp["media_type"], inp["query"], keys)
    _offer_image_urls(
        thread_id,
        [c.get("cover_url") for c in result.get("candidates") or [] if isinstance(c, dict)],
    )
    return result


async def log_media(state, thread_id, inp) -> dict:
    title = str(inp["title"]).strip()
    if not title:
        raise NoteError(400, "Provide the media title")
    taken = {note.path for note in state.store.list_notes()}
    slug_path = f"{media_items.MEDIA_DIR}/{media_items.media_slug(title)}.md"
    if slug_path.lower() in {p.lower() for p in taken}:
        return {
            "error": f"Already tracked as {slug_path} — use update_media instead",
            "path": slug_path,
        }
    cover = None
    cover_path = None
    cover_error = None
    if inp.get("cover_url"):
        if not _image_url_offered(state, thread_id, inp["cover_url"]):
            cover_error = "cover_url was not returned by lookup_media in this conversation"
        else:
            try:
                ingested = await media_lookup.ingest_cover(
                    state, inp["cover_url"], media_items.media_slug(title)
                )
                cover = ingested["url"]
                cover_path = ingested["path"]
            except NoteError as exc:
                cover_error = exc.detail
    try:
        content = media_items.build_item_content(
            title=title,
            media_type=inp["media_type"],
            status=inp.get("status") or "active",
            creator=inp.get("creator"),
            year=inp.get("year"),
            total=inp.get("total"),
            rating=inp.get("rating"),
            cover=cover,
            source=inp.get("source"),
            today=local_today(state),
        )
    except ValueError as exc:
        raise NoteError(400, str(exc))
    path = media_items.next_media_path(title, taken)
    # A slugged media path, as in the Media create route.
    written = await _write_note(state, path, content, create=True, check_name=False)
    # The Media job sources the page backdrop in the background.
    media_theme.enrich_soon(state, path)
    result: dict[str, Any] = {"path": path, "revision": content_hash(written)}
    if cover_error:
        result["cover_error"] = f"{cover_error} — item created without a cover"
    # The cover rides in `paths` so the scoped commit includes it and Undo
    # reverts the binary along with the note.
    return await _record(
        state,
        thread_id,
        "log_media",
        f'Logged {inp["media_type"]} "{title}"',
        [path] + ([cover_path] if cover_path else []),
        ["notes", "search", "media"],
        result,
        navigate=f"/note/{path}",
    )


async def search_media_images(state, thread_id, inp) -> dict:
    keys = media_lookup.load_media_keys(state.settings.data_dir)
    kind = inp.get("kind") or "backdrop"
    images = await media_lookup.image_candidates(
        inp.get("media_type") or "",
        inp.get("source"),
        inp["query"],
        keys,
        kind=kind,
        limit=int(inp.get("limit") or 8),
    )
    if not images:
        hint = (
            ""
            if (keys or {}).get("brave")
            else " A Brave Search key in Settings, Media lookup widens this to the whole web."
        )
        return {"shown": 0, "error": f"No images found for that search.{hint}"}
    _offer_image_urls(thread_id, [image["url"] for image in images])
    # The agent loop lifts _gallery into the chat and hands the model a
    # numbered URL list, so "use image 3" stays unambiguous.
    return {
        "shown": len(images),
        "_gallery": {
            "kind": kind,
            "query": inp["query"],
            "images": [
                {
                    "url": image["url"],
                    "thumb": image.get("thumb") or image["url"],
                    "origin": image.get("origin") or "web",
                }
                for image in images
            ],
        },
    }


async def set_media_art(state, thread_id, inp) -> dict:
    path = inp["path"]
    kind = inp["kind"]
    if kind not in ("backdrop", "cover"):
        raise NoteError(400, "kind must be 'backdrop' or 'cover'")
    if not _image_url_offered(state, thread_id, inp["url"]):
        raise NoteError(
            400,
            "That URL was not shown by search_media_images in this conversation"
            " — search again and pick from the numbered list",
        )
    meta, _body = fm.parse(state.store.read(path))
    if not media_items.is_media_item(meta):
        raise NoteError(400, f"Not a media note: {path}")
    title = str(meta.get("title") or Path(path).stem)
    ingested = await media_lookup.ingest_cover(
        state, inp["url"], f"{media_items.media_slug(title)}-{kind}", kind=kind
    )
    # The download takes seconds; patch the note as it is now so an edit made
    # meanwhile (a rating, a log line) survives. No await until the write.
    content = state.store.read(path)
    meta, body = fm.parse(content)
    meta[kind] = ingested["url"]
    written = await _write_note(state, path, fm.serialize(meta, body), previous=content)
    # The attachment rides in paths so Undo reverts the binary too.
    return await _record(
        state,
        thread_id,
        "set_media_art",
        f'Set the {kind} of "{title}"',
        [path, ingested["path"]],
        ["notes", "search", "media"],
        {"path": path, kind: ingested["url"], "revision": content_hash(written)},
    )


async def set_media_review(state, thread_id, inp) -> dict:
    path = inp["path"]
    current = _require_revision(state, path, inp.get("base_revision"))
    meta, _body = fm.parse(current)
    if not media_items.is_media_item(meta):
        raise NoteError(400, f"Not a media note: {path}")
    updated = media_items.set_review(current, inp["review"])
    written = await _write_note(state, path, updated, previous=current)
    title = str(meta.get("title") or Path(path).stem)
    return await _record(
        state,
        thread_id,
        "set_media_review",
        f'Wrote the review of "{title}"',
        [path],
        ["notes", "search", "media"],
        {"path": path, "revision": content_hash(written)},
    )


async def media_details(state, thread_id, inp) -> dict:
    keys = media_lookup.load_media_keys(state.settings.data_dir)
    return await media_lookup.media_details(
        inp["media_type"], inp.get("source"), inp.get("query"), keys
    )


async def similar_media(state, thread_id, inp) -> dict:
    keys = media_lookup.load_media_keys(state.settings.data_dir)
    return await media_lookup.similar_media(inp["media_type"], inp.get("source"), keys)


async def media_stats(state, thread_id, inp) -> dict:
    db = state.db
    by_type: dict[str, dict[str, int]] = {}
    for row in await db.execute_fetchall(
        "SELECT media_type, status, COUNT(*) AS n FROM media_items GROUP BY 1, 2"
    ):
        by_type.setdefault(row["media_type"], {})[row["status"]] = row["n"]
    histogram = [0] * 10
    for row in await db.execute_fetchall(
        "SELECT rating, COUNT(*) AS n FROM media_items"
        " WHERE rating IS NOT NULL GROUP BY rating"
    ):
        if isinstance(row["rating"], int) and 1 <= row["rating"] <= 10:
            histogram[row["rating"] - 1] = row["n"]
    averages = {
        row["media_type"]: round(row["avg"], 2)
        for row in await db.execute_fetchall(
            "SELECT media_type, AVG(rating) AS avg FROM media_items"
            " WHERE rating IS NOT NULL GROUP BY media_type"
        )
        if row["avg"] is not None
    }
    monthly = [
        {"month": row["ym"], "finished": row["n"]}
        for row in await db.execute_fetchall(
            "SELECT substr(finished, 1, 7) AS ym, COUNT(*) AS n FROM media_items"
            " WHERE status = 'finished' AND finished IS NOT NULL"
            " GROUP BY ym ORDER BY ym DESC LIMIT 12"
        )
    ]
    top = [
        {"title": row["title"], "media_type": row["media_type"], "rating": row["rating"]}
        for row in await db.execute_fetchall(
            "SELECT title, media_type, rating FROM media_items"
            " WHERE rating IS NOT NULL"
            " ORDER BY rating DESC, title COLLATE NOCASE LIMIT 5"
        )
    ]
    return {
        "by_type": by_type,
        "rating_histogram_1_to_10": histogram,
        "average_rating_by_type": averages,
        "finished_by_month": monthly,
        "top_rated": top,
        "note": "Ratings are stored 1-10; divide by 2 when speaking in stars.",
    }


async def update_media(state, thread_id, inp) -> dict:
    path = inp["path"]
    current = _require_revision(state, path, inp.get("base_revision"))
    remove = inp.get("remove_entry")
    try:
        if remove:
            updated, summary = media_items.remove_log_entry(
                current, str(remove.get("at")), str(remove.get("text"))
            )
        else:
            updated, summary = media_items.apply_update(
                current,
                rating=inp.get("rating"),
                status=inp.get("status"),
                entry=inp.get("entry"),
                alias=inp.get("alias"),
                today=local_today(state),
            )
    except ValueError as exc:
        raise NoteError(400, str(exc))
    written = await _write_note(state, path, updated, previous=current)
    meta, _body = fm.parse(written)
    title = str(meta.get("title") or Path(path).stem)
    return await _record(
        state,
        thread_id,
        "update_media",
        f'{summary} — "{title}"',
        [path],
        ["notes", "search", "media"],
        {"path": path, "revision": content_hash(written), "summary": summary},
    )


# --- paper (handwritten notebooks) ------------------------------------------
# Notebooks are written on the iPad; the vault holds the ink, a 2x render of
# every page and whatever text the device recognised. OMNIA's job here is to
# find pages (search_handwriting / list_notebooks, both core) and then, behind
# use_tools("paper"), actually look at one, read it with the model, or lift it
# into a normal markdown note. Nothing here ever writes ink: the drawing is the
# user's, and everything OMNIA touches is derived.

TRANSCRIBE_SYSTEM = (
    "You transcribe handwritten notes. Reproduce what is on the page and "
    "nothing else: no summary, no commentary, no preamble."
)

TRANSCRIBE_PROMPT = (
    "Transcribe this handwritten page to clean markdown. Keep the writer's "
    "own wording, line breaks, headings, lists and checkboxes. Write [?] in "
    "place of any word you cannot read rather than guessing it. Reply with "
    "the transcription only."
)


def _paper(state):
    store = getattr(state, "paper", None)
    if store is None:  # pragma: no cover - only a state built without Paper
        raise NoteError(503, "Paper is not available on this server")
    return store


def _page_ref(store, path: Any) -> tuple[str, str]:
    """Split the one Paper coordinate OMNIA ever passes — the vault-relative
    page directory `paper/<Folder>/<Title>.paper/pages/<id>` — into the
    notebook and page id the store takes."""
    rel = str(path or "").strip().strip("/")
    notebook, sep, page_id = rel.rpartition("/pages/")
    if not sep or not page_id or "/" in page_id:
        raise NoteError(
            400,
            "path must be a page directory like "
            "paper/Work/Ideas.paper/pages/<id> — search_handwriting and "
            "list_notebooks return them in that form.",
        )
    return store.notebook_path(notebook), store.check_page_id(page_id)


def _page_text(store, page_dir: str) -> dict:
    value = read_json(store.dir / page_dir / "text.json", None, skip_corrupt=True)
    return value if isinstance(value, dict) else {}


def _page_position(manifest: dict, page_id: str) -> tuple[int | None, int]:
    pages = [p for p in manifest.get("pages") or [] if isinstance(p, str)]
    position = pages.index(page_id) + 1 if page_id in pages else None
    return position, len(pages)


def _page_context(store, notebook: str, page_id: str) -> dict:
    """Everything the three per-page tools each need before doing their own
    work: the page record, its manifest, where it sits and what text exists."""
    page = store.page_public(notebook, page_id)
    manifest = store.read_notebook(notebook)
    position, total = _page_position(manifest, page_id)
    text = _page_text(store, page["path"])
    device = str(text.get("transcript") or "").strip()
    corrected = store.effective_transcript(text, int(page.get("revision") or 0))
    ai = text.get("transcript_ai")
    ai = ai if isinstance(ai, dict) else None
    return {
        "page": page,
        "manifest": manifest,
        "position": position,
        "total": total,
        "device_transcript": device,
        "corrected_transcript": corrected,
        "ai_transcript": ai,
        "label": f"Page {position}" if position else "Page",
        "title": str(manifest.get("title") or Path(notebook).stem),
        "durable_url": page.get("durable_url")
        or page_url(page["path"], "render.png", int(page.get("render_revision") or 0)),
    }


async def list_notebooks(state, thread_id, inp) -> dict:
    store = _paper(state)
    library = store.list_library()
    everything = library["notebooks"]

    folder = str(inp.get("folder") or "").strip()
    target = store.folder_path(folder) if folder else None
    favorites_only = bool(inp.get("favorites_only"))

    rows = everything
    if target and target != "paper":
        rows = [
            nb
            for nb in rows
            if nb["folder"] == target or nb["folder"].startswith(f"{target}/")
        ]
    if favorites_only:
        rows = [nb for nb in rows if nb["favorite"]]

    out: dict[str, Any] = {
        "notebooks": [
            {
                "path": nb["path"],
                "title": nb["title"],
                "folder": nb["folder"],
                "pages": nb["page_count"],
                "favorite": nb["favorite"],
                "tags": nb["tags"],
                "modified": nb["modified"],
            }
            for nb in rows
        ],
        "folders": library["folders"],
    }
    if rows:
        return out

    if not everything:
        out["note"] = (
            "There are no Paper notebooks in the vault yet. Notebooks are "
            "created by writing in Paper on the iPad, not from here — this is "
            "a fact about the vault, so do not retry the listing."
        )
        return out

    filters = []
    if target and target != "paper":
        filters.append(f"folder '{target}'")
    if favorites_only:
        filters.append("favourites only")
    where = " and ".join(filters) or "that filter"
    elsewhere = ", ".join(sorted({nb["folder"] for nb in everything})) or "paper"
    out["note"] = (
        f"No notebooks match {where}, but {len(everything)} exist elsewhere "
        f"(folders: {elsewhere}). Drop the filter or name one of those folders."
    )
    return out


async def search_handwriting(state, thread_id, inp) -> dict:
    store = _paper(state)
    query = str(inp.get("query") or "").strip()
    if not query:
        raise NoteError(400, "Provide a search query")
    index = getattr(state, "paper_index", None)
    if index is None:  # pragma: no cover - only a state built without the index
        return {
            "query": query,
            "results": [],
            "note": "The Paper index is not running on this server, so "
            "handwriting cannot be searched. Nothing to retry.",
        }

    notebook = str(inp.get("notebook") or "").strip()
    scope = store.notebook_path(notebook) if notebook else None
    limit = max(1, min(int(inp.get("limit") or 10), 50))
    results = await index.search(query, limit=limit, notebook=scope)
    out: dict[str, Any] = {"query": query, "results": results}
    if scope:
        out["notebook"] = scope
    if results:
        return out

    pages = await index.count_pages()
    transcribed = await index.count_transcribed()
    if not pages:
        out["note"] = (
            "No Paper pages are indexed at all, so nothing can match. Write in "
            "Paper on the iPad first — this is not a problem with the query."
        )
        return out
    if not transcribed:
        out["note"] = (
            f"{pages} Paper page(s) exist but none carries a transcript yet, so "
            "handwriting search has nothing to match against. Handwriting "
            'recognition runs on the iPad; use_tools("paper") exposes a tool '
            "that can read a single page with the model on demand."
        )
        return out
    if scope:
        wider = await index.search(query, limit=limit, notebook=None)
        if wider:
            names = ", ".join(
                sorted({str(hit.get("notebook_title") or hit.get("notebook")) for hit in wider})
            )
            out["note"] = (
                f"Nothing for '{query}' inside '{scope}', but {len(wider)} "
                f"page(s) match in other notebooks ({names}). The notebook "
                "filter is what excluded them — search again without it."
            )
            return out
    out["note"] = (
        f"No transcript contains '{query}' across {transcribed} transcribed "
        f"page(s) of {pages}. This search matches recognised words literally, "
        "so rephrasing rarely helps — try semantic_search for the idea, or a "
        "single distinctive word the user would actually have written."
    )
    return out


async def read_page(state, thread_id, inp) -> dict:
    """Look at one handwritten page. The render rides out as a real image
    block via `_images`, which the agent loop lifts before the model sees the
    result — recognised text is a hint, the picture is the evidence."""
    store = _paper(state)
    notebook, page_id = _page_ref(store, inp.get("path"))
    ctx = _page_context(store, notebook, page_id)
    page = ctx["page"]
    revision = int(page.get("revision") or 0)
    render_revision = int(page.get("render_revision") or 0)

    out: dict[str, Any] = {
        "path": page["path"],
        "notebook": notebook,
        "notebook_title": ctx["title"],
        "page": ctx["position"],
        "pages": ctx["total"],
        "revision": revision,
        "transcript": ctx["corrected_transcript"],
        "raw_transcript": ctx["device_transcript"],
        "corrected_transcript": _page_text(store, page["path"]).get(
            "corrected_transcript"
        ),
        "transcript_ai": (ctx["ai_transcript"] or {}).get("text", ""),
    }

    render = store.dir / page["path"] / "render.png"
    if not render.is_file():
        out["note"] = (
            "This page has no render.png yet, so there is no image to look at "
            "— only whatever text is above. A render appears once the iPad has "
            "drawn and uploaded the page; re-reading now will not produce one."
        )
        return out

    # Decoding and downscaling a page render is CPU work: off the event loop.
    encoded = await asyncio.to_thread(
        vision.encode_image, render.read_bytes(), "image/png", label=page["path"]
    )
    out["_images"] = [{**encoded, "kind": "image"}]
    stale = bool(render_revision and render_revision < revision)
    out["stale_render"] = stale
    out["note"] = (
        "The page image follows this result — read the handwriting yourself "
        "and trust it over the recognised text, which is often wrong."
        + (
            " The render is older than the ink (the iPad has not uploaded a "
            "fresh one), so the newest strokes may be missing."
            if stale
            else ""
        )
    )
    return out


async def transcribe_page(state, thread_id, inp) -> dict:
    """Have the model read a page and store the reading as `transcript_ai`.

    Derived data, like the media backdrop job: metered, budget-gated, and not
    recorded as an undoable action — the ink it was read from never moves.
    """
    store = _paper(state)
    notebook, page_id = _page_ref(store, inp.get("path"))
    ctx = _page_context(store, notebook, page_id)
    page = ctx["page"]
    base = {"path": page["path"], "notebook": notebook, "page": ctx["position"]}
    # Taken before the model call: the reading is of this render, whatever
    # the ink has become by the time the reply arrives.
    revision = int(page.get("revision") or 0)
    render_revision = int(page.get("render_revision") or 0)

    render = store.dir / page["path"] / "render.png"
    if not render.is_file():
        return {
            **base,
            "transcribed": False,
            "note": "This page has no render.png, so there is no image to "
            "read. The iPad uploads one after drawing; nothing here can "
            "produce it, so do not retry.",
        }
    if render_revision < revision:
        return {
            **base,
            "transcribed": False,
            "note": "The render is older than the ink (the iPad has not "
            "uploaded a fresh one), so a reading now would miss the newest "
            "strokes. Re-run once the iPad has uploaded a fresh render.",
        }

    config = background.config_for(state)
    if not background.allowed(state, config):
        return {
            **base,
            "transcribed": False,
            "note": "The monthly token budget has paused unattended model "
            "work, so this page cannot be transcribed right now. Any text the "
            "iPad already recognised is still readable without spending "
            "anything.",
        }

    provider = media_theme.build_media_provider(state)
    if provider is None:
        return {
            **base,
            "transcribed": False,
            "note": "No AI provider is configured, so a page cannot be "
            "transcribed. Settings, OMNIA is where a provider and key go.",
        }

    encoded = await asyncio.to_thread(
        vision.encode_image, render.read_bytes(), "image/png", label=page["path"]
    )
    reply = await provider.chat(
        TRANSCRIBE_SYSTEM,
        [
            {
                "role": "user",
                "content": [
                    image_block(encoded["media_type"], encoded["data"]),
                    {"type": "text", "text": TRANSCRIBE_PROMPT},
                ],
            }
        ],
        [],
        None,
    )
    ledger = getattr(state, "usage", None)
    if ledger is not None:
        ledger.record("paper", getattr(reply, "usage", None))

    text = (reply.text or "").strip()
    model = getattr(provider, "model", "") or ""
    if not text:
        return {
            **base,
            "transcribed": False,
            "note": f"The model ({model or 'configured provider'}) returned "
            "nothing for this page. Sending the same image again will most "
            "likely return nothing again — look at the page instead.",
        }

    index = getattr(state, "paper_index", None)
    # The write takes a feed seq, so it and its index row share the index's
    # feed lock as a route's do; the provider call above stays outside it.
    feed_lock = getattr(index, "feed_lock", None)
    async with feed_lock if feed_lock is not None else contextlib.nullcontext():
        updated = store.set_transcript_ai(
            notebook, page_id, text, model, made_for=render_revision
        )
        # Deliberately no autocommit.touch(): this is rebuildable derived data,
        # not a user edit, so it stays read-tier — no confirmation card, no
        # action-log entry, nothing to undo. text.json rides the next commit
        # sweep like the media job's backdrop metadata does.
        if index is not None and hasattr(index, "index_page"):
            await index.index_page(page["path"])
    live = getattr(state, "live", None)
    if live is not None:
        live.paper_changed(page["path"], int(updated.get("revision") or 0))
    return {
        **base,
        "transcribed": True,
        "model": model,
        "transcript": text,
        "note": "This is the model's reading of the handwriting, not the "
        "user's own text — say so if you quote it.",
    }


async def note_from_page(state, thread_id, inp) -> dict:
    """Lift a page's transcript into a normal markdown note, with the render
    embedded and a link back to the notebook so the ink stays the source."""
    store = _paper(state)
    notebook, page_id = _page_ref(store, inp.get("path"))
    ctx = _page_context(store, notebook, page_id)
    page = ctx["page"]
    # A user correction is authoritative. The model transcript is useful only
    # when the page has no corrected/device text.
    transcript = ctx["corrected_transcript"] or (ctx["ai_transcript"] or {}).get("text")
    transcript = str(transcript or "").strip()
    if not transcript:
        return {
            "path": page["path"],
            "created": False,
            "note": "This page has no transcript to lift into a note — "
            "neither the iPad's recognition nor a model reading. Transcribe "
            'it first (use_tools("paper") lists the tool that does), then '
            "call this again.",
        }

    target = str(inp.get("target_path") or "").strip()
    if not target:
        stem = f"{ctx['title']} — {ctx['label']}"
        target = f"{media_items.media_slug(stem)}.md"
    if not target.endswith(".md"):
        target = f"{target}.md"
    target = _unique_path(state, target)

    lines = [f"# {ctx['title']} — {ctx['label']}", "", transcript, ""]
    render_revision = int(page.get("render_revision") or 0)
    if (store.dir / page["path"] / "render.png").is_file():
        url = page_url(page["path"], "render.png", render_revision)
        lines += [f"![{ctx['label']}]({url})", ""]
    # Stable ids survive a notebook rename. Keep the path in the image URL
    # (that route serves the current blob) but use the id URL for navigation.
    stable_link = paper_page_url(str(ctx["manifest"].get("id") or ""), page_id)
    lines += [f"[{ctx['title']} in Paper]({stable_link})", ""]

    content = "\n".join(lines)
    written = await _write_note(state, target, content, create=True)
    return await _record(
        state,
        thread_id,
        "note_from_page",
        f'Created {target} from {ctx["title"]} — {ctx["label"]}',
        [target],
        ["notes", "search"],
        {
            "path": target,
            "revision": content_hash(written),
            "source": page["path"],
        },
        navigate=f"/note/{target}",
    )


def _tool(
    name: str,
    description: str,
    props: dict,
    required: list[str],
    fn: Executor,
    *,
    mode: str = "read",
) -> tuple[ToolDef, Executor]:
    return (
        ToolDef(
            name,
            description,
            _schema(props, required),
            mode=mode,
            tier="action" if mode in {"direct", "confirmed"} else "suggestion",
        ),
        fn,
    )


# --- capture ---------------------------------------------------------------
# The observed usage is "tell OMNIA a thing and have it filed". Doing that with
# ensure_daily_note + create_task/create_event + patch_note costs four rounds and
# a confirmation card, so in practice it degraded to prose in a note and nothing
# actionable. This does the whole thing in one call.


async def capture(state, thread_id, inp) -> dict:
    text = " ".join(str(inp.get("text") or "").split())
    if not text:
        raise NoteError(400, "Provide the thing to capture")
    kind = inp.get("kind") or "note"
    if kind not in ("task", "event", "note"):
        raise NoteError(400, "kind must be task, event or note")

    when = (inp.get("when") or "").strip()
    day: date | None = None
    start: str | None = None
    if when:
        try:
            if "T" in when:
                start_dt = parse_user_datetime(when, user_tz(state))
                start = start_dt.isoformat(timespec="minutes")
                day = start_dt.date()
            else:
                day = date.fromisoformat(when)
        except ValueError as exc:
            raise NoteError(
                400, "when must be YYYY-MM-DD or YYYY-MM-DDTHH:MM"
            ) from exc
    target_day = day or local_now(state).date()

    # An event is validated before the daily note below is created: refused
    # after it, the capture left that note written and never recorded.
    event_values: dict | None = None
    if kind == "event":
        if not day:
            raise NoteError(400, "An event needs `when`")
        all_day = start is None
        values = {
            "title": text,
            "start": start or day.isoformat(),
            "all_day": all_day,
            "reminders": inp.get("reminders") or ([30] if not all_day else []),
        }
        if not all_day:
            # A timed event needs an end after its start; nobody says how long
            # "library at 3" runs, so assume an hour.
            values["end"] = (
                datetime.fromisoformat(start) + timedelta(hours=1)
            ).isoformat()
        event_values = _event_values(state, values)

    created: dict = {}
    paths: list[str] = []

    # The daily note for the day it concerns anchors every capture, so it has
    # to exist before a task can be written into it.
    daily_path = f"daily/{target_day.isoformat()}.md"
    if not state.store.exists(daily_path):
        content = tpl.render(
            state.settings.vault_dir,
            "daily",
            title=target_day.isoformat(),
            date=target_day.isoformat(),
        )
        await _write_note(state, daily_path, content, create=True)

    board: str | None = None
    if event_values is not None:
        event = state.events.add(event_values)
        created["event"] = event
        paths.append(EVENTS_PATH)
    elif kind == "task":
        # Create the task *in* the daily note. It used to go to Tasks Inbox and
        # also leave a prose line in the daily note — two records of the same
        # thing, free to drift apart, and only one of them a real task.
        # Unless the user is working in a folder that owns a task board, in
        # which case that board is where the task belongs.
        board = await task_boards.nearest_board(
            state.db, await _context_note(state, thread_id)
        )
        task = await create_task_record(
            state,
            text=text,
            path=board or daily_path,
            due=day.isoformat() if day else None,
            due_visible=bool(day),
        )
        created["task"] = task
        paths.append(task.get("path", board or daily_path))

    if kind != "task" or board:
        # A task in the daily note is already its own record; one filed on a
        # folder board still needs the day's line, or the day loses the trace.
        body = state.store.read(daily_path)
        marker = "- " + text + (f" 📅 {day.isoformat()}" if day and kind == "note" else "")
        # Compare whole lines. A substring test meant capturing "Stand" matched
        # an existing "- Standup with the team" and silently wrote nothing,
        # while the structured record was still created.
        if marker not in body.splitlines():
            await _write_note(
                state, daily_path, body.rstrip("\n") + f"\n{marker}\n", previous=body
            )
    paths.append(daily_path)
    created["daily_note"] = daily_path

    label = {"task": "task", "event": "event", "note": "note"}[kind]
    return await _record(
        state,
        thread_id,
        "capture",
        f"Captured {label}: {text[:70]}",
        paths,
        ["notes", "tasks", "calendar"],
        {"captured": kind, "text": text, **created},
    )


async def propose_edits(state, thread_id, inp) -> dict:
    """Stage proofreading suggestions for the user to accept one at a time.

    The model supplies the text to replace, not offsets — asked for offsets it
    invents them. `anchor` resolves each phrase to a real position and returns
    a reason for every one it cannot, which goes back to the model so a bad
    batch is correctable in the same turn instead of vanishing.
    """
    path = inp["path"]
    raw = inp.get("suggestions") or []
    if not raw:
        raise NoteError(400, "Provide at least one suggestion")
    content = state.store.read(path)
    accepted, rejections = anchor(content, raw)

    if not accepted:
        return {
            "staged": 0,
            "rejected": rejections,
            "note": "Nothing could be anchored. Quote the note's text exactly.",
        }

    state.proofread.save(path, content_hash(content), accepted)
    counts: dict[str, int] = {}
    for suggestion in accepted:
        counts[suggestion["category"]] = counts.get(suggestion["category"], 0) + 1
    breakdown = ", ".join(f"{n} {name}" for name, n in sorted(counts.items()))

    return await _record(
        state,
        thread_id,
        "propose_edits",
        f"Proofread “{path}”: {len(accepted)} suggestion(s) — {breakdown}",
        [path],
        # Not "notes": nothing in the vault changed. The note page listens for
        # this token to open its Review tab.
        ["proofread"],
        {
            "staged": len(accepted),
            "by_category": counts,
            "rejected": rejections,
            "note": "The user reviews these in the app; do not also edit the note.",
        },
        undoable=False,
    )


# --- conversation recall ---------------------------------------------------
# A memory product that could not remember its own conversations: threads were
# persisted and structured, but no tool could read them.


MATCHES_PER_CONVERSATION = 3


async def search_conversations(state, thread_id, inp) -> dict:
    """Search what was actually said in past conversations."""
    query = str(inp.get("query") or "").strip()
    if not query:
        raise NoteError(400, "Provide a query")
    limit = max(1, min(int(inp.get("limit", 10)), 40))
    index = getattr(state.threads, "index", None)
    if index is None:
        return {
            "conversations": [],
            "query": query,
            "note": "Conversation search is unavailable in this session.",
        }
    # Rows, not conversations: one thread can match in a dozen messages, so ask
    # for a multiple of the thread limit before grouping. Ranking is bm25's —
    # the literal substring scan this replaced could not match a question
    # ("what did we decide about pricing") against how it was actually phrased.
    rows = await asyncio.to_thread(index.search, query, limit * 8)

    conversations: dict[str, dict] = {}
    for row in rows:
        found = conversations.get(row["thread_id"])
        if found is None:
            if len(conversations) >= limit:
                continue
            try:
                thread = state.threads.get(row["thread_id"])
            except NoteError:
                continue  # deleted since the last sync; the next boot purges it
            found = conversations[row["thread_id"]] = {
                "thread_id": row["thread_id"],
                "title": thread.get("title"),
                "updated_at": thread.get("updated_at"),
                "match_count": 0,
                "matches": [],
            }
        found["match_count"] += 1
        if len(found["matches"]) < MATCHES_PER_CONVERSATION:
            found["matches"].append(
                {
                    "role": row["role"],
                    "excerpt": str(row["excerpt"] or "").replace("\n", " "),
                }
            )

    out: dict = {"conversations": list(conversations.values()), "query": query}
    if not out["conversations"]:
        out["note"] = (
            f"Nothing said in any conversation matches “{query}”. Matching is on "
            "exact words with no stemming, so try the words the user would have "
            "typed (singular as well as plural), or list_conversations to see "
            "what is there."
        )
    return out


async def read_conversation(state, thread_id, inp) -> dict:
    """Read a past conversation's transcript (text only, no tool payloads)."""
    target = str(inp.get("thread_id") or "").strip()
    if not target:
        raise NoteError(400, "Provide thread_id (from search_conversations)")
    thread = state.threads.get(target)
    limit = max(1, min(int(inp.get("limit", 40)), 200))
    messages = [
        {"role": m.get("role"), "text": str(m.get("text") or "")[:2000]}
        for m in (thread.get("messages") or [])
        if m.get("text")
    ]
    return {
        "thread_id": target,
        "title": thread.get("title"),
        "date": thread.get("date"),
        "messages": messages[-limit:],
    }


async def list_conversations(state, thread_id, inp) -> dict:
    conversations = state.threads.list()[: max(1, min(int(inp.get("limit", 20)), 100))]
    if not conversations:
        return {"conversations": [], "note": "There are no saved conversations."}
    return {"conversations": conversations}


# --- memory ----------------------------------------------------------------
# Structure, template and the prompt rendering live in .memory; this half is
# the two tools that write.

MEMORY_NOTE = MEMORY_PATH
MEMORY_SECTION = f"## {MEMORY_CATCH_ALL}"
# The tags the prompt frames context with. An entry is rendered inside <memory>
# on every turn, so one that carried '</memory>' or a <data-...> fence could
# close its own block and speak as the prompt.
_PROMPT_TAG = re.compile(r"</?(?:memory|habits|data-[0-9a-f]*)\b[^>]*>", re.IGNORECASE)


async def remember(state, thread_id, inp) -> dict:
    """File one durable fact under the right heading of the memory note.

    Memory previously required composing and staging the WHOLE file through the
    review tray — an extra, unrequested tool call competing with a persona that
    says be brief and act. It never once happened. This is one cheap call.

    Entries carry the date they were learned and go newest-first, which is what
    lets the prompt rendering drop the oldest when the note outgrows its budget.
    """
    text = " ".join(_PROMPT_TAG.sub(" ", str(inp.get("text") or "")).split())
    if not text:
        raise NoteError(400, "Provide the fact to remember")
    if len(text) > 400:
        raise NoteError(400, "Keep a memory entry under 400 characters")
    heading = memory_heading_for(inp.get("category"))

    try:
        content = state.store.read(MEMORY_NOTE)
    except NoteError:
        content = await _write_note(
            state, MEMORY_NOTE, "---\ntitle: OMNIA Memory\n---\n\n", create=True
        )

    # Comparison ignores the date stamp, so re-learning the same fact on a
    # later day is still a duplicate — and so are the undated entries written
    # before stamping existed.
    wanted = memory_normalized(text)
    if any(
        memory_normalized(line.strip()[2:]) == wanted
        for line in content.split("\n")
        if line.strip().startswith("- ")
    ):
        return {"remembered": False, "reason": "already recorded", "text": text}

    entry = f"- {memory_today(state)} {text}"
    await _write_note(
        state, MEMORY_NOTE, memory_add_entry(content, heading, entry), previous=content
    )
    return await _record(
        state,
        thread_id,
        "remember",
        f"Noted: {text[:80]}",
        [MEMORY_NOTE],
        ["notes"],
        {"remembered": True, "text": text, "section": heading},
    )


async def forget(state, thread_id, inp) -> dict:
    """Drop memory lines matching a phrase.

    This used to delete every bullet *containing* the phrase, so forgetting "e"
    took the whole note with it. Exact entries win; a partial match is honoured
    only when it is unambiguous, so a vague call now fails instead of
    over-deleting.

    Matching ignores an entry's `YYYY-MM-DD ` stamp: the model forgets a fact by
    quoting the fact, and it has no reason to know which day it was recorded.
    """
    needle = memory_normalized(str(inp.get("text") or ""))
    if not needle:
        raise NoteError(400, "Provide the text to forget")
    content = state.store.read(MEMORY_NOTE)
    lines = content.split("\n")
    entries = [
        (index, memory_normalized(line.strip()[2:]))
        for index, line in enumerate(lines)
        if line.strip().startswith("- ")
    ]
    exact = [index for index, entry in entries if entry == needle]
    targets = exact or [index for index, entry in entries if needle in entry]
    if not targets:
        raise NoteError(404, f"No memory entry matching '{needle}'")
    if not exact and len(targets) > 1:
        found = "; ".join(lines[index].strip()[2:] for index in targets)
        raise NoteError(
            400,
            f"'{needle}' matches {len(targets)} memory entries ({found}). "
            "Give the full text of the one to forget.",
        )
    drop = set(targets)
    removed = [lines[index].strip()[2:] for index in targets]
    kept = [line for index, line in enumerate(lines) if index not in drop]
    await _write_note(state, MEMORY_NOTE, "\n".join(kept), previous=content)
    return await _record(
        state,
        thread_id,
        "forget",
        f"Forgot {len(removed)} memory entr(y/ies)",
        [MEMORY_NOTE],
        ["notes"],
        {"forgot": removed},
    )


async def note_observation(state, thread_id, inp) -> dict:
    """Stage something noticed in a background pass for the user to confirm.

    The proposal tier exists for exactly this: a proactive pass may accumulate
    a suggestion, but only the user turns it into a habit or a memory entry.
    Every limit — one a day, a week to answer, ninety days' silence after a
    dismissal — is enforced here rather than asked for in the prompt, and a
    refusal comes back as a note so the pass carries on quietly.
    """
    entry, note = observations.record(
        state,
        text=str(inp.get("text") or ""),
        kind=str(inp.get("kind") or "").strip().lower(),
        habit_key=str(inp.get("habit_key") or "").strip() or None,
        habit_value=str(inp.get("habit_value") or "").strip() or None,
    )
    if entry is None:
        return {"staged": False, "note": note}
    if hasattr(state, "autocommit"):
        state.autocommit.touch()
    return {
        "staged": True,
        "observation_id": entry["id"],
        "note": note,
    }


# --- bulk librarian work ---------------------------------------------------


def _tag_pattern(tag: str) -> re.Pattern:
    # Inline #tags only; never touches a "# Heading" or a longer tag that
    # merely starts with this one (#ml must not match #mlops).
    return re.compile(rf"(?<![\w/#])#{re.escape(tag)}(?![\w/-])")


async def _rewrite_each(state, paths: list[str], rewrite) -> tuple[list[str], int]:
    """Apply a pure text rewrite to each note, reading it again right before
    its write. Writing a note awaits its indexing, so a copy of the next note
    taken by the scan was stale by then and an editor save landing meanwhile
    was written over. actions.mutation_lock keeps OMNIA's own tools apart;
    the notes PUT and the media rewrite do not take it. `rewrite(content)`
    returns the new text and how many places changed; a note that no longer
    matches, or is gone, is left alone. Returns the paths written and the
    total count."""
    changed: list[str] = []
    total = 0
    for path in paths:
        try:
            content = state.store.read(path)
        except NoteError:
            continue
        new, count = rewrite(content)
        if not count:
            continue
        # No await between the read above and this write.
        await _write_note(state, path, new, previous=content)
        changed.append(path)
        total += count
    return changed, total


async def rename_tag(state, thread_id, inp) -> dict:
    """Rewrite an inline #tag across the whole vault in one operation.

    Previously the only route was read+edit per note, which cannot finish
    inside the tool-round budget for anything more than a handful of notes.
    """
    old = str(inp["from_tag"]).lstrip("#").strip()
    new = str(inp.get("to_tag") or "").lstrip("#").strip()
    if not old:
        raise NoteError(400, "from_tag is required")
    if not new:
        raise NoteError(400, "to_tag is required (use delete_note to remove notes)")
    if old == new:
        raise NoteError(400, "from_tag and to_tag are the same")
    if not re.fullmatch(r"[A-Za-z][\w/-]*", new):
        raise NoteError(400, "to_tag must start with a letter and contain no spaces")

    pattern = _tag_pattern(old)

    def retag(content: str) -> tuple[str, int]:
        return pattern.subn(f"#{new}", content)

    rows = await state.db.execute_fetchall(
        "SELECT path FROM tags WHERE tag = ? ORDER BY path", (old,)
    )
    hits: list[dict] = []
    for row in rows:
        path = row["path"]
        try:
            content = state.store.read(path)
        except NoteError:
            continue
        _replaced, count = retag(content)
        if count:
            hits.append({"path": path, "occurrences": count})

    if not hits:
        return {"renamed": 0, "note": f"No notes contain the inline tag #{old}."}
    if inp.get("dry_run"):
        return {
            "dry_run": True,
            "from": old,
            "to": new,
            "would_change": hits,
            "note_count": len(hits),
        }

    changed, _count = await _rewrite_each(state, [h["path"] for h in hits], retag)
    if not changed:
        return {"renamed": 0, "note": f"No notes contain the inline tag #{old}."}
    return await _record(
        state,
        thread_id,
        "rename_tag",
        f"Renamed #{old} to #{new} across {len(changed)} note(s)",
        changed,
        ["notes", "tags"],
        {
            "from": old,
            "to": new,
            "note_count": len(changed),
            "paths": changed,
        },
    )


async def patch_note(state, thread_id, inp) -> dict:
    """Targeted find/replace edits, so editing a long note doesn't require
    re-emitting the whole file (which the output-token ceiling forbids)."""
    path = inp["path"]
    edits = inp.get("edits") or []
    if not edits:
        raise NoteError(400, "Provide at least one edit")
    content = state.store.read(path)
    previous = _require_revision(state, path, inp.get("base_revision"))

    applied = []
    for index, edit in enumerate(edits):
        old = str(edit.get("old_string", ""))
        new = str(edit.get("new_string", ""))
        if not old:
            raise NoteError(400, f"edits[{index}].old_string is required")
        count = content.count(old)
        if count == 0:
            raise NoteError(
                409,
                f"edits[{index}].old_string was not found in {path}. Read the note "
                "again — it may have changed, or the text may differ in whitespace.",
            )
        if count > 1 and not edit.get("replace_all"):
            raise NoteError(
                409,
                f"edits[{index}].old_string appears {count} times in {path}. Include "
                "more surrounding text to make it unique, or set replace_all.",
            )
        content = content.replace(old, new) if edit.get("replace_all") else content.replace(old, new, 1)
        applied.append({"replaced": count if edit.get("replace_all") else 1})

    normalized = await _write_note(state, path, content, previous=previous)
    return await _record(
        state,
        thread_id,
        "patch_note",
        f"Patched {path} ({len(applied)} edit(s))",
        [path],
        ["notes"],
        {"path": path, "edits": applied, "revision": content_hash(normalized)},
    )


async def find_unlinked_mentions(state, thread_id, inp) -> dict:
    """Places where a note's title is written in prose but not linked."""
    target = inp.get("note_path")
    titles: dict[str, str] = {}
    rows = await state.db.execute_fetchall("SELECT path, title FROM notes")
    for row in rows:
        title = (row["title"] or "").strip()
        if len(title) >= 4 and not row["path"].startswith(("templates/", "daily/")):
            titles[row["path"]] = title
    if target:
        titles = {p: t for p, t in titles.items() if p == target}
        if not titles:
            raise NoteError(404, f"No indexed note at {target}")

    limit = max(1, min(int(inp.get("limit", 40)), 200))
    found: list[dict] = []
    for source in await state.pending.effective_paths():
        if source.startswith(("templates/", ".exscalia")):
            continue
        try:
            content = state.store.read(source)
        except NoteError:
            continue
        for path, title in titles.items():
            if path == source:
                continue
            if re.search(rf"\[\[{re.escape(title)}(\||#|\]\])", content):
                continue  # already linked
            match = re.search(rf"(?<![\w\[]){re.escape(title)}(?![\w\]])", content)
            if match:
                start = max(0, match.start() - 60)
                found.append(
                    {
                        "in_note": source,
                        "mentions": path,
                        "title": title,
                        "context": content[start : match.end() + 60].replace("\n", " "),
                    }
                )
                if len(found) >= limit:
                    return {"unlinked_mentions": found, "truncated": True}
    if not found:
        if not titles:
            note = "No note outside templates/ and daily/ has a title of 4+ characters to look for."
        elif target:
            note = f"No note mentions “{titles[target]}” without already linking it."
        else:
            note = f"None of {len(titles)} note titles appears unlinked in another note."
        return {"unlinked_mentions": [], "truncated": False, "note": note}
    return {"unlinked_mentions": found, "truncated": False}


# --- Phase 4: knowledge & power vault operations ---------------------------


RRF_K = 60  # the conventional reciprocal-rank-fusion damping constant


def _best_ranks(hits: list[dict]) -> dict[str, int]:
    """path → its best (1-based) rank within one result list.

    Several chunks of the same note collapse to the one that ranked highest,
    which is the same dedup the old single-pass version got by keeping the
    first hit for a path and skipping the rest.
    """
    ranks: dict[str, int] = {}
    for position, hit in enumerate(hits, start=1):
        path = hit.get("path")
        if path and path not in ranks:
            ranks[path] = position
    return ranks


def _first_by_path(hits: list[dict]) -> dict[str, dict]:
    best: dict[str, dict] = {}
    for hit in hits:
        path = hit.get("path")
        if path:
            best.setdefault(path, hit)
    return best


def _in_scope(path: str | None, path_prefix, exclude_prefix) -> bool:
    if not path:
        return False
    included = prefix_list(path_prefix)
    if included and not any(path.startswith(p) for p in included):
        return False
    return not any(path.startswith(p) for p in prefix_list(exclude_prefix))


async def ask_vault(state, thread_id, inp) -> dict:
    """Retrieve the notes most relevant to a question so the answer can cite
    them. Meaning and exact wording are searched together and fused by
    reciprocal rank — the caller synthesizes."""
    query = " ".join(str(inp.get("query") or "").split()).strip()
    if not query:
        raise NoteError(400, "Ask a question")
    k = max(1, min(int(inp.get("k") or 6), 12))
    path_prefix = inp.get("path_prefix") or None
    exclude_prefix = inp.get("exclude_prefix") or None
    from .tools import keyword_search, scope_phrase

    async def semantic_hits() -> list[dict]:
        if not getattr(state.semantic, "available", False):
            return []
        return await state.semantic.search(
            query, k=k * 2, path_prefix=path_prefix, exclude_prefix=exclude_prefix
        )

    async def keyword_hits() -> list[dict]:
        found = await keyword_search(state, thread_id, {"query": query, "limit": k * 2})
        rows = found.get("results") or found.get("notes") or []
        # FTS has no folder scope of its own, and the list is only k*2 long, so
        # applying the same scope here is cheaper than teaching it one.
        return [r for r in rows if _in_scope(r.get("path"), path_prefix, exclude_prefix)]

    # Both halves always run, concurrently. Semantic-first-with-keyword-backfill
    # meant a question with k strong vector hits never reached FTS at all, so
    # exact terms — names, course codes, error strings — lost to fuzzy
    # neighbours that merely read like the question.
    outcomes = await asyncio.gather(
        semantic_hits(), keyword_hits(), return_exceptions=True
    )
    semantic, keyword = (o if isinstance(o, list) else [] for o in outcomes)

    semantic_ranks, keyword_ranks = _best_ranks(semantic), _best_ranks(keyword)
    semantic_by_path, keyword_by_path = _first_by_path(semantic), _first_by_path(keyword)
    fused: list[tuple[float, str]] = []
    for path in list(semantic_ranks) + [p for p in keyword_ranks if p not in semantic_ranks]:
        score = sum(
            1.0 / (RRF_K + rank)
            for rank in (semantic_ranks.get(path), keyword_ranks.get(path))
            if rank is not None
        )
        fused.append((score, path))
    # A stable sort leaves ties in semantic-then-keyword order.
    fused.sort(key=lambda row: row[0], reverse=True)

    sources: list[dict] = []
    for _score, path in fused[:k]:
        semantic_rank, keyword_rank = semantic_ranks.get(path), keyword_ranks.get(path)
        # Quote from whichever list ranked the note better: an FTS snippet is
        # built around the matched words, a semantic chunk around the meaning,
        # and the stronger signal makes the more useful excerpt.
        if semantic_rank is not None and (keyword_rank is None or semantic_rank <= keyword_rank):
            hit = semantic_by_path[path]
            sources.append(
                {
                    "path": path,
                    "heading": hit.get("heading"),
                    "excerpt": (hit.get("text") or "")[:800],
                }
            )
        else:
            hit = keyword_by_path[path]
            sources.append(
                {"path": path, "title": hit.get("title"), "snippet": hit.get("snippet")}
            )
    return {
        "query": query,
        "sources": sources,
        "note": (
            "Answer only from these sources and cite the note paths; say so if "
            "they don't cover it."
            if sources
            else f"Nothing in the notebook matches “{query}”"
            + scope_phrase(path_prefix, exclude_prefix)
            + " — by meaning or by wording. Say so rather than answering from "
            "general knowledge."
        ),
    }


async def summarize_timespan(state, thread_id, inp) -> dict:
    """What happened in a date range: notes created/touched, tasks completed,
    events — for a weekly or monthly review. Read-only."""
    start = str(inp.get("start") or "").strip()
    end = str(inp.get("end") or "").strip()
    try:
        start_d = date.fromisoformat(start)
        end_d = date.fromisoformat(end)
    except ValueError:
        raise NoteError(400, "start and end must be YYYY-MM-DD")
    # Bound below as text, so '20260905' must become '2026-09-05'.
    start, end = start_d.isoformat(), end_d.isoformat()
    if end_d < start_d:
        raise NoteError(400, "end cannot be before start")
    if (end_d - start_d).days > 366:
        raise NoteError(400, "Range is limited to one year")
    db = state.db
    created = [
        dict(r)
        for r in await db.execute_fetchall(
            "SELECT path, title FROM notes WHERE substr(created,1,10) BETWEEN ? AND ? "
            "AND path NOT LIKE 'templates/%' ORDER BY created",
            (start, end),
        )
    ]
    modified = [
        dict(r)
        for r in await db.execute_fetchall(
            "SELECT path, title FROM notes WHERE substr(modified,1,10) BETWEEN ? AND ? "
            "AND substr(created,1,10) < ? AND path NOT LIKE 'templates/%' ORDER BY modified",
            (start, end, start),
        )
    ]
    completed = [
        dict(r)
        for r in await db.execute_fetchall(
            "SELECT path, text, completed_at FROM tasks WHERE status = 'done' "
            "AND substr(completed_at,1,10) BETWEEN ? AND ? ORDER BY completed_at",
            (start, end),
        )
    ]
    events = state.events.occurrences(start_d, end_d) if hasattr(state, "events") else []
    return {
        "start": start,
        "end": end,
        "notes_created": created,
        "notes_modified": modified,
        "tasks_completed": completed,
        "events": events,
    }


async def set_note_followup(state, thread_id, inp) -> dict:
    """Resurface a note later — a reminder linked to the note, delivered like
    any calendar reminder."""
    path = inp["path"]
    if not state.store.exists(path):
        raise NoteError(404, "Note not found")
    when = str(inp.get("when") or "").strip()
    if not when:
        days = int(inp.get("in_days") or 0)
        if days <= 0:
            raise NoteError(400, "Give a follow-up date (when) or in_days")
        when = (local_now(state).date() + timedelta(days=days)).isoformat()
    # A day with no time is a reminder that morning, like create_reminder.
    if _is_bare_date(when):
        when = f"{when}T{REMINDER_TIME}"
    try:
        start_dt = parse_user_datetime(when, user_tz(state))
    except ValueError as exc:
        raise NoteError(400, "when must be YYYY-MM-DD or YYYY-MM-DDTHH:MM") from exc
    when = start_dt.isoformat(timespec="minutes")
    end = (start_dt + timedelta(minutes=15)).isoformat(timespec="minutes")
    meta, _body = fm.parse(state.store.read(path))
    title = str(meta.get("title") or Path(path).stem)
    values = state.events.normalize(
        title=f"Follow up: {title}",
        start=when,
        end=end,
        all_day=False,
        timezone_name=state.timezone,
        notes=None,
        note_path=path,
        recurrence=None,
        reminders=[0],
    )
    event = state.events.add(values)
    state.autocommit.touch()
    return await _record(
        state,
        thread_id,
        "set_note_followup",
        f"Follow-up on {path} set for {when}",
        [EVENTS_PATH],
        ["calendar"],
        {"event": event, "path": path},
    )


async def flashcards_from_note(state, thread_id, inp) -> dict:
    """Make a spaced-repetition deck from a note: cards the caller wrote from it
    and/or `Question :: Answer` lines already in the note."""
    path = inp["path"]
    content = state.store.read(path)
    cards = [
        new_flashcard(str(c.get("front", "")), str(c.get("back", "")))
        for c in inp.get("cards") or []
        if str(c.get("front", "")).strip() and str(c.get("back", "")).strip()
    ]
    cards.extend(cards_from_markdown(content))
    if not cards:
        raise NoteError(
            400,
            "No cards — supply cards (Q/A you wrote from the note) or add "
            "`Question :: Answer` lines to the note",
        )
    meta, _body = fm.parse(content)
    name = str(inp.get("deck_name") or meta.get("title") or Path(path).stem)
    deck = state.flashcards.create(name, cards, [path])
    return await _record(
        state,
        thread_id,
        "flashcards_from_note",
        f"Created deck “{deck['name']}” ({len(cards)} cards) from {path}",
        [_deck_dir(deck["id"])],
        ["flashcards"],
        {"id": deck["id"], "name": deck["name"], "card_count": len(cards)},
        navigate=f"/study/{deck['id']}",
    )


async def bulk_tag(state, thread_id, inp) -> dict:
    """Add or remove a frontmatter tag across every note under a folder or that
    already has a given tag."""
    tag = str(inp.get("tag") or "").lstrip("#").strip()
    if not tag:
        raise NoteError(400, "tag is required")
    operation = str(inp.get("operation") or "add").lower()
    if operation not in ("add", "remove"):
        raise NoteError(400, "operation must be add or remove")
    folder = (inp.get("folder") or "").strip("/")
    has_tag = str(inp.get("has_tag") or "").lstrip("#").strip()
    if not folder and not has_tag:
        raise NoteError(400, "Give a folder or has_tag filter")
    tagged = None
    if has_tag:
        rows = await state.db.execute_fetchall("SELECT path FROM tags WHERE tag = ?", (has_tag,))
        tagged = {row["path"] for row in rows}
    changed: list[str] = []
    matched = 0
    for info in state.store.list_notes():
        path = info.path
        if path.startswith("templates/"):
            continue
        if folder and not path.startswith(folder + "/"):
            continue
        if tagged is not None and path not in tagged:
            continue
        matched += 1
        current = state.store.read(path)
        meta, body = fm.parse(current)
        raw = meta.get("tags") or []
        tags = [
            str(t).strip().lstrip("#")
            for t in (raw if isinstance(raw, list) else [raw])
            if str(t).strip()
        ]
        if operation == "add":
            if tag in tags:
                continue
            tags.append(tag)
        else:
            if tag not in tags:
                continue
            tags = [t for t in tags if t != tag]
        meta["tags"] = tags
        await _write_note(state, path, fm.serialize(meta, body), previous=current)
        changed.append(path)
    if not changed:
        scope = " and ".join(
            part
            for part in (f"folder “{folder}”" if folder else "", f"#{has_tag}" if has_tag else "")
            if part
        )
        note = (
            f"All {matched} note(s) matching {scope} already "
            f"{'have' if operation == 'add' else 'lack'} #{tag}; nothing changed."
            if matched
            else f"No notes matched {scope}, so nothing changed. Check the folder name "
            "with list_folders, or the tag with keyword_search."
        )
        return {"changed": 0, "note": note}
    return await _record(
        state,
        thread_id,
        "bulk_tag",
        f"{operation.title()}ed #{tag} on {len(changed)} note(s)",
        changed,
        ["notes", "tags", "search"],
        {"changed": len(changed), "tag": tag, "operation": operation},
    )


async def resolve_unlinked_mentions(state, thread_id, inp) -> dict:
    """Turn detected unlinked title mentions into real [[wikilinks]]."""
    mentions = await find_unlinked_mentions(
        state, thread_id, {"note_path": inp.get("note_path"), "limit": int(inp.get("limit") or 100)}
    )
    by_source: dict[str, list[dict]] = {}
    for item in mentions.get("unlinked_mentions") or []:
        by_source.setdefault(item["in_note"], []).append(item)
    changed: list[str] = []
    linked = 0
    for source, items in by_source.items():
        original = content = state.store.read(source)
        touched = False
        for item in items:
            title = item["title"]
            match = re.search(rf"(?<![\w\[]){re.escape(title)}(?![\w\]])", content)
            if match:
                # Spliced rather than subn'd: re reads a str replacement as a
                # template, so a "\a" in the title wrote a control character.
                content = f"{content[: match.start()]}[[{title}]]{content[match.end() :]}"
                touched = True
                linked += 1
        if touched:
            await _write_note(state, source, content, previous=original)
            changed.append(source)
    if not changed:
        note = mentions.get("note") or (
            "The mentions found no longer match the notes' text; nothing was linked."
        )
        return {"linked": 0, "note": note}
    return await _record(
        state,
        thread_id,
        "resolve_unlinked_mentions",
        f"Linked {linked} mention(s) across {len(changed)} note(s)",
        changed,
        ["notes", "search"],
        {"linked": linked, "notes": changed},
    )


async def find_replace_across_vault(state, thread_id, inp) -> dict:
    """Preview-then-apply a plain text find/replace across every note."""
    find = str(inp.get("find") or "")
    if not find:
        raise NoteError(400, "find is required")
    replace = str(inp.get("replace") or "")
    case_sensitive = bool(inp.get("case_sensitive", True))
    pattern = None if case_sensitive else re.compile(re.escape(find), re.IGNORECASE)

    def substitute(content: str) -> tuple[str, int]:
        if case_sensitive:
            count = content.count(find)
            return (content.replace(find, replace) if count else content), count
        # A function, not the string: re reads a str replacement as a
        # template, so "\alpha" wrote a control character and "C:\Users" raised.
        return pattern.subn(lambda _m: replace, content)

    hits: list[dict] = []
    for info in state.store.list_notes():
        if info.path.startswith("templates/"):
            continue
        try:
            content = state.store.read(info.path)
        except NoteError:
            continue
        _new, count = substitute(content)
        if count:
            hits.append({"path": info.path, "occurrences": count})
    if not hits:
        return {"changed": 0, "note": f"No notes contain “{find}”."}
    if inp.get("dry_run"):
        return {
            "dry_run": True,
            "find": find,
            "replace": replace,
            "note_count": len(hits),
            "total_occurrences": sum(h["occurrences"] for h in hits),
            "would_change": hits,
        }
    changed, total = await _rewrite_each(state, [h["path"] for h in hits], substitute)
    if not changed:
        return {"changed": 0, "note": f"No notes contain “{find}”."}
    return await _record(
        state,
        thread_id,
        "find_replace_across_vault",
        f"Replaced “{find}” in {len(changed)} note(s)",
        changed,
        ["notes", "search", "tasks"],
        {"note_count": len(changed), "total_occurrences": total},
    )


async def split_note(state, thread_id, inp) -> dict:
    """Split a note into one linked note per `## ` section, leaving the original
    holding the links."""
    path = inp["path"]
    _require_revision(state, path, inp.get("base_revision"))
    content = state.store.read(path)
    meta, body = fm.parse(content)
    preamble: list[str] = []
    sections: list[list] = []
    current: list | None = None
    in_fence = False
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
        heading = None if in_fence else re.match(r"^##\s+(.+?)\s*$", line)
        if heading:
            if current:
                sections.append(current)
            current = [heading.group(1).strip(), []]
        elif current is not None:
            current[1].append(line)
        else:
            preamble.append(line)
    if current:
        sections.append(current)
    if len(sections) < 2:
        raise NoteError(400, "Nothing to split — the note needs at least two `## ` sections")
    folder = task_boards.folder_of(path)
    made: list[str] = []
    children: list[str] = []
    links: list[str] = []
    paths = [n.path for n in state.store.list_notes()]
    for title, section_lines in sections:
        slug = re.sub(r"[^\w\- ]", "", title).strip() or "Section"
        child_path = _unique_path(state, f"{folder + '/' if folder else ''}{slug}.md", made)
        # The children are created after the original is cut down, so a name
        # (or a landing) NoteStore.create would refuse has to stop the split here.
        NoteStore._check_new_name(child_path)
        check_landing(child_path)
        child_body = "\n".join(section_lines).strip() + "\n"
        children.append(fm.serialize({"title": title}, child_body))
        made.append(child_path)
        # Link the file the heading became (punctuation dropped, " 2" on a
        # collision), keeping the heading as the label.
        target = qualified_target(child_path, [*paths, *made])
        label = re.sub(r"[\[\]|]", "", title).strip()
        links.append(f"- [[{target}|{label}]]" if label and label != target else f"- [[{target}]]")
    lead = "\n".join(preamble).strip()
    new_body = (lead + "\n\n" if lead else "") + "\n".join(links) + "\n"
    # The original gives up its task rows before a child claims them. Task ids
    # are unique in the index: a child indexed first kept none of its tasks,
    # and the original's rewrite then recorded them as deleted.
    _require_unchanged(state, path, content_hash(content))
    normalized = await _write_note(state, path, fm.serialize(meta, new_body), previous=content)
    for child_path, child in zip(made, children):
        await _write_note(state, child_path, child, create=True)
    return await _record(
        state,
        thread_id,
        "split_note",
        f"Split {path} into {len(made)} notes",
        [path, *made],
        ["notes", "search"],
        {"path": path, "created": made, "revision": content_hash(normalized)},
    )


async def merge_notes(state, thread_id, inp) -> dict:
    """Combine notes into one, rewrite inbound [[wikilinks]] to the survivor,
    and move the merged-away originals to Trash."""
    sources = [str(p) for p in (inp.get("sources") or []) if p]
    target = str(inp.get("target") or "").strip()
    if len(sources) < 2:
        raise NoteError(400, "Give at least two source notes to merge")
    if not target:
        raise NoteError(400, "Give a target note path")
    if target in sources:
        raise NoteError(400, "Target cannot be one of the sources")
    for path in sources:
        if not state.store.exists(path):
            raise NoteError(404, f"Source note not found: {path}")
    # What each link opened before the merge, by the editor's rules.
    before = LinkResolver(n.path for n in state.store.list_notes())

    checked: dict[str, str] = {}
    blocks: list[str] = []
    for path in sources:
        content = state.store.read(path)
        checked[path] = content_hash(content)
        meta, body = fm.parse(content)
        title = str(meta.get("title") or Path(path).stem)
        blocks.append(f"## {title}\n\n{body.strip()}\n")
    combined = "\n".join(blocks)

    # The sources give up their task rows before the target claims the same
    # ids. Task ids are unique in the index: a target indexed first kept none
    # of them, and removing the sources then recorded them as deleted.
    for path in sources:
        await state.indexer.remove_note(path)
    try:
        if state.store.exists(target):
            existing = state.store.read(target)
            emeta, ebody = fm.parse(existing)
            await _write_note(
                state,
                target,
                fm.serialize(emeta, ebody.rstrip() + "\n\n" + combined),
                previous=existing,
            )
        else:
            await _write_note(
                state, target, fm.serialize({"title": Path(target).stem}, combined), create=True
            )
    except Exception:
        # A target that cannot be written leaves the sources as they were.
        for path in sources:
            await state.indexer.index_note(path, state.store.read(path))
        raise
    affected = [target]

    async def keep_sources(detail: str) -> dict:
        """The target holds the sources' content now, so a failure records
        what was written as one undoable action instead of raising. A raise
        left the sources out of the index with no record, and the retry
        appended the same blocks to the target again."""
        # The sources stay in place, so they go back into the index (the task
        # rows they shared with the target now belong to the target).
        for path in sources:
            await state.indexer.index_note(path, state.store.read(path))
        return await _record(
            state,
            thread_id,
            "merge_notes",
            f"Merged {len(sources)} notes into {target} (partial: sources kept)",
            affected,
            ["notes", "search", "tasks", "calendar"],
            {
                "error": f"{detail} — the sources were left in place; the merged "
                "note and relinked notes are saved as one undoable action, so do not "
                "merge again",
                "target": target,
            },
        )

    # Inbound links are the ones that opened a source: by file name or path,
    # never by title (the two differ under media/ and omnia/), and a same-name
    # note elsewhere keeps its links.
    merged = set(sources)
    paths = [n.path for n in state.store.list_notes() if n.path not in merged]

    def retarget(link: str) -> str | None:
        return relink(link, target, paths) if before.resolve(link) in merged else None

    rows = await state.db.execute_fetchall("SELECT DISTINCT source, target FROM links")
    linkers = {row["source"] for row in rows if before.resolve(row["target"]) in merged}
    try:
        # Best effort per linker, as rewrite_inbound_links: one that cannot be
        # read or written is logged and skipped. A linker whose frontmatter
        # does not load (a template's `title: {{title}}`) gets its body
        # rewrite written as is, since only the body changed.
        for linker in sorted(linkers - merged - {target}):
            try:
                content = state.store.read(linker)
                new, changed = rewrite_links(content, retarget)
                if not changed:
                    continue
                try:
                    await _write_note(state, linker, new, previous=content)
                except NoteError as exc:
                    if exc.status != 422:
                        raise
                    state.store.write_raw(linker, new)
                    await state.indexer.index_note(linker, new)
            except NoteError:
                log.warning("could not rewrite the links in %s", linker, exc_info=True)
                continue
            affected.append(linker)
    except Exception as err:
        log.warning("merge_notes stopped rewriting links", exc_info=True)
        return await keep_sources(f"Rewriting links failed: {err}")

    # Trash only what was merged: every source is checked before any is
    # trashed, with no await in between, so an edit made while the target and
    # links were written stays in its note instead of only in a Trash entry.
    try:
        for path in sources:
            _require_unchanged(state, path, checked[path])
    except NoteError as err:
        return await keep_sources(err.detail)
    for path in sources:
        manifest = state.trash.trash_note(path)
        if hasattr(state, "proofread"):
            state.proofread.clear(path)
        # The Trash entry rides the merge's commit, so Undo takes it back out
        # along with restoring the note.
        affected += [path, f"{NoteStore.INTERNAL_DIR}/trash/{manifest['id']}"]

    return await _record(
        state,
        thread_id,
        "merge_notes",
        f"Merged {len(sources)} notes into {target}",
        affected,
        ["notes", "search", "tasks", "calendar", "trash"],
        {"target": target, "merged": sources},
        navigate=target,
    )


async def export_notes(state, thread_id, inp) -> dict:
    """Bundle selected notes into one markdown note under exports/ that the user
    can open or copy. Stays inside the vault."""
    paths = [str(p) for p in (inp.get("paths") or []) if p]
    if not paths:
        raise NoteError(400, "Give the note paths to export")
    parts: list[str] = []
    for path in paths:
        if not state.store.exists(path):
            raise NoteError(404, f"Note not found: {path}")
        meta, body = fm.parse(state.store.read(path))
        title = str(meta.get("title") or Path(path).stem)
        parts.append(f"# {title}\n\n{body.strip()}\n")
    name = re.sub(r"[^\w\- ]", "", str(inp.get("name") or "export")).strip() or "export"
    out_path = _unique_path(state, f"exports/{name}.md")
    combined = "\n\n---\n\n".join(parts)
    await _write_note(state, out_path, combined, create=True)
    return await _record(
        state,
        thread_id,
        "export_notes",
        f"Exported {len(paths)} note(s) to {out_path}",
        [out_path],
        ["notes", "search"],
        {"path": out_path, "note_count": len(paths)},
        navigate=out_path,
    )


# --- Round 3: selection-native editing -------------------------------------


def _code_point_index(units: bytes, offset: int) -> int | None:
    """The str index of a UTF-16 offset into `units` (the note encoded
    UTF-16-LE), or None when it lies outside the note or splits a surrogate
    pair."""
    if not 0 <= offset <= len(units) // 2:
        return None
    try:
        return len(units[: 2 * offset].decode("utf-16-le"))
    except UnicodeDecodeError:
        return None


async def _resolve_selection(state, thread_id, inp) -> tuple[str, str, int, int]:
    """(path, content, from, to) for a selection edit. Uses the live editor
    range when its doc_len still matches the file; else falls back to locating an
    explicit old_string in the open note. Raises if neither resolves."""
    sel = await _selection(state, thread_id)
    if sel:
        path = sel["path"]
        try:
            content = state.store.read(path)
        except NoteError:
            content = None
        if content is not None:
            # CodeMirror counts UTF-16 code units, Python code points: a due
            # marker (U+1F4C5) or any emoji before the range is two units.
            units = content.encode("utf-16-le")
            if int(sel.get("doc_len") or -1) == len(units) // 2:
                frm = _code_point_index(units, int(sel.get("from") or 0))
                to = _code_point_index(units, int(sel.get("to") or 0))
                if frm is not None and to is not None and frm <= to:
                    return path, content, frm, to
    # Fallback: an explicit old_string in the note the user has open.
    path = inp.get("path") or (sel.get("path") if sel else None) or await _context_note(state, thread_id)
    if not path or not state.store.exists(path):
        raise NoteError(409, "No live selection — open the note and pass path + old_string")
    content = state.store.read(path)
    old = inp.get("old_string")
    if not old:
        raise NoteError(409, "The editor selection is stale — pass old_string (the exact text to act on)")
    index = content.find(str(old))
    if index < 0:
        raise NoteError(409, "old_string not found in the note; read it again")
    return path, content, index, index + len(str(old))


_BLOCK_STYLES = {"heading", "quote", "callout", "bullet", "task", "code"}
_ALL_STYLES = _BLOCK_STYLES | {"bold", "italic"}
# An existing heading marker (not a #tag), and an existing bullet/checkbox.
_HEADING_PREFIX = re.compile(r"^\s*#{1,6}(?:\s+|$)")
_TASK_PREFIX = re.compile(r"^\s*(?:[-*+]\s+)?(?:\[[ xX/]\](?:\s+|$))?")


def _apply_style(text: str, style: str, level=None) -> str:
    lines = text.split("\n")
    if style == "bold":
        return f"**{text}**"
    if style == "italic":
        return f"*{text}*"
    if style == "code":
        return f"```\n{text}\n```" if "\n" in text else f"`{text}`"
    if style == "heading":
        lv = max(1, min(int(level or 2), 6))
        return "\n".join(
            f"{'#' * lv} {_HEADING_PREFIX.sub('', line).strip()}" if line.strip() else line
            for line in lines
        )
    if style == "quote":
        return "\n".join(f"> {line}" if line.strip() else ">" for line in lines)
    if style == "callout":
        body = "\n".join(f"> {line}" if line.strip() else ">" for line in lines)
        return "> [!note]\n" + body
    if style == "bullet":
        return "\n".join(f"- {line.lstrip()}" if line.strip() else line for line in lines)
    if style == "task":
        return "\n".join(
            f"- [ ] {_TASK_PREFIX.sub('', line).strip()}" if line.strip() else line for line in lines
        )
    return text


async def edit_selection(state, thread_id, inp) -> dict:
    """Replace the user's selection with new text; an empty selection inserts at
    the cursor. Acts on the exact live range, or on old_string as a fallback."""
    new_text = str(inp.get("new_string") or "")
    path, content, frm, to = await _resolve_selection(state, thread_id, inp)
    updated = content[:frm] + new_text + content[to:]
    normalized = await _write_note(state, path, updated, previous=content)
    verb = "Inserted text into" if frm == to else "Replaced the selection in"
    return await _record(
        state,
        thread_id,
        "edit_selection",
        f"{verb} {path}",
        [path],
        ["notes", "search", "tasks"],
        {"path": path, "revision": content_hash(normalized)},
    )


async def format_selection(state, thread_id, inp) -> dict:
    """Wrap or prefix the selection: heading / quote / callout / bold / italic /
    code / bullet / task."""
    style = str(inp.get("style") or "").lower()
    if style not in _ALL_STYLES:
        raise NoteError(400, f"style must be one of: {', '.join(sorted(_ALL_STYLES))}")
    path, content, frm, to = await _resolve_selection(state, thread_id, inp)
    if style in _BLOCK_STYLES:
        # Snap to whole lines so block markers apply cleanly.
        frm = content.rfind("\n", 0, frm) + 1
        line_end = content.find("\n", to)
        to = len(content) if line_end == -1 else line_end
    formatted = _apply_style(content[frm:to], style, inp.get("level"))
    updated = content[:frm] + formatted + content[to:]
    normalized = await _write_note(state, path, updated, previous=content)
    return await _record(
        state,
        thread_id,
        "format_selection",
        f"Formatted the selection as {style} in {path}",
        [path],
        ["notes", "search"],
        {"path": path, "revision": content_hash(normalized)},
    )


async def extract_to_note(state, thread_id, inp) -> dict:
    """Cut the selection into a new note and leave a [[link]] where it was."""
    path, content, frm, to = await _resolve_selection(state, thread_id, inp)
    extracted = content[frm:to].strip()
    if not extracted:
        raise NoteError(400, "Nothing selected to extract")
    title = " ".join(str(inp.get("title") or "").split()).strip()
    if not title:
        title = re.sub(r"^#+\s*", "", extracted.splitlines()[0]).strip()[:80] or "Untitled"
    slug = re.sub(r"[^\w\- ]", "", title).strip() or "Untitled"
    folder = task_boards.folder_of(path)
    new_path = _unique_path(state, f"{folder + '/' if folder else ''}{slug}.md")
    # Checked before the cut: the new note is created after it.
    NoteStore._check_new_name(new_path)
    check_landing(new_path)
    # Link the file the note becomes (qualified_target counts new_path itself).
    link = qualified_target(new_path, [n.path for n in state.store.list_notes()])
    updated = content[:frm] + f"[[{link}]]" + content[to:]
    # The source gives up the selection's task ids before the new note claims
    # them: task ids are unique in the index.
    await _write_note(state, path, updated, previous=content)
    await _write_note(
        state, new_path, fm.serialize({"title": Path(new_path).stem}, extracted + "\n"), create=True
    )
    return await _record(
        state,
        thread_id,
        "extract_to_note",
        f"Extracted selection from {path} to {new_path}",
        [path, new_path],
        ["notes", "search", "tasks"],
        {"path": new_path, "source": path},
        navigate=f"/note/{new_path}",
    )


# --- Round 3: navigation ---------------------------------------------------

_APP_VIEWS = {
    "calendar": "/calendar",
    "tasks": "/tasks",
    "media": "/media",
    "notes": "/notes",
    "paper": "/paper",
    "search": "/search",
    "graph": "/graph",
    # A bare /history is a dead end ("choose a note"): with a `note` the view
    # is that note's versions, without one it is what OMNIA has done.
    "history": "/history/omnia",
    "trash": "/trash",
    "pending": "/pending",
    "settings": "/settings",
    "omnia": "/omnia",
}


def _uri_component(value: str) -> str:
    """JavaScript's encodeURIComponent, so a route matches the one the app
    builds itself (tabs dedupe by exact URL)."""
    return quote(value, safe="!'()*")


def _route_path(path: str) -> str:
    """lib/api.ts encodePath: each segment encoded, slashes kept."""
    return "/".join(_uri_component(part) for part in path.split("/"))


async def _resolve_note_path(state, ref: str) -> str:
    ref = ref.strip()
    if state.store.exists(ref):
        return ref
    if not ref.endswith(".md") and state.store.exists(ref + ".md"):
        return ref + ".md"
    needle = ref[:-3].lower() if ref.lower().endswith(".md") else ref.lower()
    rows = await state.db.execute_fetchall(
        "SELECT path, title FROM notes WHERE path NOT LIKE 'templates/%'"
    )
    scored: list[tuple] = []
    for row in rows:
        stem = row["path"].rsplit("/", 1)[-1]
        stem = stem[:-3] if stem.endswith(".md") else stem
        stem_l, title_l = stem.lower(), str(row["title"] or "").lower()
        if stem_l == needle or title_l == needle:
            scored.append((0, row["path"].count("/"), row["path"]))
        elif needle in stem_l or needle in title_l:
            scored.append((1, row["path"].count("/"), row["path"]))
    if not scored:
        raise NoteError(404, f"No note matching “{ref}”")
    scored.sort()
    return scored[0][2]


def _notebook_href(notebook: str) -> str:
    """lib/paperurl.ts notebookHref: the viewer route for a notebook path."""
    return f"/paper/{_route_path(notebook.removeprefix('paper/'))}"


def _find_notebook(store, ref: str) -> dict:
    """A notebook named by path (with or without `paper/` and `.paper`) or by
    title — exact before partial, shallow before deep, like notes."""
    needle = ref.strip().strip("/").lower().removeprefix("paper/").removesuffix(".paper")
    scored: list[tuple] = []
    for nb in store.list_library()["notebooks"]:
        route = nb["path"].lower().removeprefix("paper/").removesuffix(".paper")
        names = {route, route.rsplit("/", 1)[-1], str(nb.get("title") or "").lower()}
        if needle in names:
            scored.append((0, nb["path"].count("/"), nb["path"], nb))
        elif any(needle in name for name in names):
            scored.append((1, nb["path"].count("/"), nb["path"], nb))
    if not needle or not scored:
        raise NoteError(404, f"No Paper notebook matching “{ref}” — list_notebooks shows the library")
    scored.sort(key=lambda row: row[:3])
    return scored[0][3]


def _paper_target(state, ref: str) -> tuple[str, str, dict]:
    """(route, label, result) for a notebook or one page of it. A page gets
    the rename-proof /paper/id/… link; the iPad shell hands either to its
    native canvas."""
    store = _paper(state)
    if "/pages/" in ref:
        notebook, page_id = _page_ref(store, ref)
        manifest = store.read_notebook(notebook)
        position, _total = _page_position(manifest, page_id)
        if position is None:
            raise NoteError(404, f"Page not found: {page_id}")
        title = str(manifest.get("title") or Path(notebook).stem)
        if manifest.get("id"):
            route = paper_page_url(str(manifest["id"]), page_id)
        else:
            route = f"{_notebook_href(notebook)}#{_uri_component(page_id)}"
        return route, f"{title}, page {position}", {"notebook": notebook, "page": page_id}
    nb = _find_notebook(store, ref)
    label = str(nb.get("title") or nb["path"])
    return _notebook_href(nb["path"]), label, {"notebook": nb["path"]}


async def open_in_app(state, thread_id, inp) -> dict:
    """Open a note, a view, a folder's task board, a Paper notebook or page, a
    search or a tag in the app (navigation only — changes no data)."""
    view = str(inp.get("view") or "").strip().lower()
    note = str(inp.get("note") or "").strip()
    folder = str(inp.get("folder") or "").strip()
    paper = str(inp.get("paper") or "").strip()
    query = str(inp.get("query") or "").strip()
    tag = str(inp.get("tag") or "").strip().lstrip("#")

    async def go(route: str, summary: str, result: dict) -> dict:
        return await _record(
            state, thread_id, "open", summary, [], [], result,
            undoable=False, navigate=route,
        )

    if view == "history" and note:
        path = await _resolve_note_path(state, note)
        return await go(
            f"/history?path={_uri_component(path)}",
            f"Opened the history of {path}",
            {"view": view, "path": path},
        )
    # The specific targets outrank a generic view passed alongside them:
    # view="tasks" with a folder means that folder's board.
    if folder:
        folder = _canonical_folder(state, folder)
        if not await task_boards.board_for_folder(state.db, folder):
            raise NoteError(404, f"{folder} has no task board — create_task_board makes one")
        return await go(
            f"/tasks/{_route_path(folder)}", f"Opened the {folder} task board", {"folder": folder}
        )
    if paper:
        route, label, result = _paper_target(state, paper)
        return await go(route, f"Opened {label}", result)
    if query:
        return await go(f"/search?q={_uri_component(query)}", f"Searched for {query}", {"query": query})
    if tag:
        rows = await state.db.execute_fetchall(
            "SELECT DISTINCT tag FROM tags WHERE lower(tag) = ? AND path NOT LIKE 'templates/%'",
            (tag.lower(),),
        )
        tags = sorted(row["tag"] for row in rows)
        if not tags:
            raise NoteError(404, f"No notes are tagged #{tag}")
        canonical = tag if tag in tags else tags[0]
        return await go(f"/tags/{_uri_component(canonical)}", f"Opened #{canonical}", {"tag": canonical})
    if view:
        route = _APP_VIEWS.get(view)
        if not route:
            raise NoteError(400, f"Unknown view — choose from: {', '.join(sorted(_APP_VIEWS))}")
        return await go(route, f"Opened {view}", {"view": view})
    if not note:
        raise NoteError(
            400, "Give a note, a view, a folder, a Paper notebook, a search query or a tag to open"
        )
    path = await _resolve_note_path(state, note)
    return await go(f"/note/{path}", f"Opened {path}", {"path": path})


async def close_tabs(state, thread_id, inp) -> dict:
    """Close open editor tabs — by a match on their name, or all but the current
    one (others=true)."""
    match = str(inp.get("match") or "").strip()
    others = bool(inp.get("others"))
    if not match and not others:
        raise NoteError(400, "Say which tabs: a match string, or others=true for all but the current")
    directive = {"match": match or None, "others": others}
    return await _record(
        state,
        thread_id,
        "close_tabs",
        f"Closed tabs matching “{match}”" if match else "Closed the other tabs",
        [],
        [],
        {"closed": directive},
        undoable=False,
        close_tabs=directive,
    )


# --- Round 3: discovery, hygiene & history ---------------------------------


def _note_stem(path: str) -> str:
    stem = path.rsplit("/", 1)[-1]
    return (stem[:-3] if stem.endswith(".md") else stem).lower()


async def related_notes(state, thread_id, inp) -> dict:
    """Notes semantically nearest to a given note ('what else did I write about
    this?'). Uses the same neighbor search the tag suggester does."""
    path = inp["path"]
    if not state.store.exists(path):
        raise NoteError(404, "Note not found")
    if not getattr(state.semantic, "available", False):
        return {"related": [], "note": "Semantic index isn't available."}
    _meta, body = fm.parse(state.store.read(path))
    k = max(1, min(int(inp.get("k") or 8), 20))
    results = await state.semantic.search(body[:2000], k=k + 1)
    related = [
        {"path": r["path"], "heading": r.get("heading"), "score": r.get("score")}
        for r in results
        if r["path"] != path
    ]
    if not related:
        return {
            "related": [],
            "note": f"Nothing in the semantic index sits near {path}; it may not be "
            "embedded yet. Try keyword_search on its key terms, or get_backlinks.",
        }
    return {"related": related[:k]}


async def get_forward_links(state, thread_id, inp) -> dict:
    """What this note links out to (the complement of get_backlinks)."""
    path = inp["path"]
    if not state.store.exists(path):
        raise NoteError(404, "Note not found")
    rows = await state.db.execute_fetchall(
        "SELECT DISTINCT target, resolved_path FROM links WHERE source = ?", (path,)
    )
    out = [
        {
            "target": row["target"],
            "path": row["resolved_path"],
            "exists": row["resolved_path"] is not None,
        }
        for row in rows
    ]
    if not out:
        return {
            "forward_links": [],
            "note": f"{path} links to no other note. get_backlinks shows what links to it.",
        }
    return {"forward_links": out}


async def find_notes(state, thread_id, inp) -> dict:
    """Vault hygiene sweep: filter = orphan | stale | empty | large."""
    kind = str(inp.get("filter") or "").lower()
    limit = max(1, min(int(inp.get("limit") or 50), 200))
    notes = [
        dict(r)
        for r in await state.db.execute_fetchall(
            "SELECT path, title, modified FROM notes WHERE path NOT LIKE 'templates/%'"
        )
    ]
    if kind == "orphan":
        sources = {r["source"] for r in await state.db.execute_fetchall("SELECT DISTINCT source FROM links")}
        targets = {r["resolved_path"] for r in await state.db.execute_fetchall("SELECT DISTINCT resolved_path FROM links")}
        result = [n for n in notes if n["path"] not in sources and n["path"] not in targets]
    elif kind == "stale":
        before = str(inp.get("before") or "").strip()
        if not before:
            raise NoteError(400, "For stale notes, give `before` (YYYY-MM-DD)")
        result = [n for n in notes if str(n.get("modified") or "")[:10] < before]
    elif kind == "empty":
        floor = int(inp.get("min_chars") or 1)
        result = []
        for note in notes:
            try:
                _m, body = fm.parse(state.store.read(note["path"]))
            except NoteError:
                continue
            if len(body.strip()) < floor:
                result.append(note)
    elif kind == "large":
        sized = []
        for note in notes:
            try:
                sized.append((len(state.store.read(note["path"])), note))
            except NoteError:
                continue
        sized.sort(key=lambda item: item[0], reverse=True)
        result = [{**note, "chars": size} for size, note in sized]
    else:
        raise NoteError(400, "filter must be orphan, stale, empty, or large")
    trimmed = [
        {k: v for k, v in note.items() if k in ("path", "title", "modified", "chars")}
        for note in result[:limit]
    ]
    if not result:
        hints = {
            "orphan": "Every note links out or is linked to.",
            "stale": "Try a later `before` date.",
            "empty": "Raise min_chars to catch near-empty notes.",
        }
        note = _empty_note(
            f"No notes match filter={kind}.", len(notes), "notes", hints.get(kind, "")
        )
        return {"filter": kind, "count": 0, "notes": [], "note": note}
    return {"filter": kind, "count": len(result), "notes": trimmed}


async def find_broken_links(state, thread_id, inp) -> dict:
    """[[wikilinks]] whose target note doesn't exist."""
    rows = await state.db.execute_fetchall(
        "SELECT DISTINCT source, target FROM links WHERE resolved_path IS NULL"
    )
    broken = [{"in_note": r["source"], "target": r["target"]} for r in rows]
    limit = max(1, min(int(inp.get("limit") or 100), 500))
    if not broken:
        total = await state.db.execute_fetchall("SELECT COUNT(*) AS n FROM links")
        n = int(total[0]["n"]) if total else 0
        note = f"No broken links: all {n} links resolve." if n else "The vault has no links at all."
        return {"broken_links": [], "count": 0, "note": note}
    return {"broken_links": broken[:limit], "count": len(broken)}


async def vault_overview(state, thread_id, inp) -> dict:
    """A state-of-the-vault snapshot: counts of notes, tasks, tags, media, and
    notes per folder."""
    db = state.db

    async def scalar(sql: str) -> int:
        rows = await db.execute_fetchall(sql)
        return int(rows[0]["c"]) if rows else 0

    folders: dict[str, int] = {}
    for row in await db.execute_fetchall("SELECT path FROM notes WHERE path NOT LIKE 'templates/%'"):
        folder = row["path"].rsplit("/", 1)[0] if "/" in row["path"] else "(root)"
        folders[folder] = folders.get(folder, 0) + 1
    media = {
        r["status"]: r["c"]
        for r in await db.execute_fetchall("SELECT status, COUNT(*) c FROM media_items GROUP BY status")
    }
    return {
        "notes": await scalar("SELECT COUNT(*) c FROM notes WHERE path NOT LIKE 'templates/%'"),
        "open_tasks": await scalar("SELECT COUNT(*) c FROM tasks WHERE status != 'done'"),
        "tags": await scalar("SELECT COUNT(DISTINCT tag) c FROM tags"),
        "task_boards": await scalar("SELECT COUNT(*) c FROM notes WHERE task_board = 1"),
        "media_by_status": media,
        "notes_by_folder": dict(sorted(folders.items(), key=lambda kv: -kv[1])[:15]),
    }


async def summarize_notes(state, thread_id, inp) -> dict:
    """Gather a note / folder / set of notes (bodies, capped) for the model to
    summarize. The folder/set gather is the value; a single note is read_notes."""
    paths = [str(p) for p in (inp.get("paths") or []) if p]
    folder = str(inp.get("folder") or "").strip("/")
    if not paths and folder:
        rows = await state.db.execute_fetchall(
            "SELECT path FROM notes WHERE path LIKE ? AND path NOT LIKE 'templates/%' ORDER BY path",
            (f"{folder}/%",),
        )
        paths = [r["path"] for r in rows]
    if not paths:
        raise NoteError(400, "Give a folder or a list of note paths")
    cap = 40
    out = []
    for path in paths[:cap]:
        try:
            meta, body = fm.parse(state.store.read(path))
        except NoteError:
            continue
        out.append(
            {"path": path, "title": str(meta.get("title") or Path(path).stem), "excerpt": body.strip()[:1500]}
        )
    return {
        "count": len(out),
        "truncated": len(paths) > cap,
        "notes": out,
        "note": "Summarize these and cite the note paths.",
    }


async def note_changes(state, thread_id, inp) -> dict:
    """What changed in a note since a date — a diff against its state then."""
    path = inp["path"]
    since = str(inp.get("since") or "").strip()
    state.store.resolve(path)
    async with state.vault._git_lock:
        versions = await asyncio.to_thread(state.vault.history_sync, path, 300)
    if not versions:
        return {"path": path, "changed": False, "note": "No version history yet."}
    baseline = None
    if since:
        baseline = next((v for v in versions if str(v.get("committed_at") or "")[:10] < since), None)
    baseline = baseline or versions[-1]
    async with state.vault._git_lock:
        old = await asyncio.to_thread(
            state.vault.commit_content_sync, baseline["sha"], baseline.get("version_path") or path
        )
    current = state.store.read(path)
    diff = "".join(
        difflib.unified_diff(
            (old or "").splitlines(keepends=True),
            current.splitlines(keepends=True),
            fromfile=f"{path}@{baseline['short_sha']}",
            tofile=f"{path}@current",
        )
    )
    return {
        "path": path,
        "since": since or str(baseline.get("committed_at") or "")[:10],
        "baseline": baseline["short_sha"],
        "changed": bool(diff),
        "diff": diff[:8000],
    }


async def restore_note_to(state, thread_id, inp) -> dict:
    """Restore a note to how it was at or before a date."""
    path = inp["path"]
    on_or_before = str(inp.get("date") or "").strip()
    if not on_or_before:
        raise NoteError(400, "Give the date to restore to (YYYY-MM-DD)")
    state.store.resolve(path)
    async with state.vault._git_lock:
        versions = await asyncio.to_thread(state.vault.history_sync, path, 300)
    chosen = next(
        (
            v
            for v in versions
            if str(v.get("committed_at") or "")[:10] <= on_or_before and not v.get("deleted")
        ),
        None,
    )
    if chosen is None:
        raise NoteError(404, f"No version of {path} at or before {on_or_before}")
    async with state.vault._git_lock:
        content = await asyncio.to_thread(
            state.vault.commit_content_sync, chosen["sha"], chosen.get("version_path") or path
        )
    if content is None:
        raise NoteError(404, "That version could not be read")
    # The whole note is replaced, so, as restore_note_version's replace does,
    # a save the model never read refuses instead of vanishing: a saved but
    # not yet autocommitted edit is in no commit Undo could bring back.
    # Checked after the history reads, with no await before the write.
    current = _require_revision(state, path, inp.get("base_revision"))
    written = await _write_note(state, path, content, previous=current)
    return await _record(
        state,
        thread_id,
        "restore_note_to",
        f"Restored {path} to its {str(chosen.get('committed_at') or '')[:10]} version",
        [path],
        ["notes", "search", "tasks"],
        {"path": path, "sha": chosen["sha"], "revision": content_hash(written)},
    )


# --- Round 3: calendar & media ---------------------------------------------


def _valid_range(start: str, end: str, max_days: int) -> tuple[date, date]:
    try:
        start_d = date.fromisoformat(start)
        end_d = date.fromisoformat(end)
    except ValueError:
        raise NoteError(400, "start and end must be YYYY-MM-DD")
    if end_d < start_d:
        raise NoteError(400, "end cannot be before start")
    if (end_d - start_d).days > max_days:
        raise NoteError(400, f"Range must be within {max_days} days")
    return start_d, end_d


async def find_free_time(state, thread_id, inp) -> dict:
    """Open slots between events in a range, within working hours."""
    start_d, end_d = _valid_range(str(inp.get("start") or ""), str(inp.get("end") or ""), 31)
    day_start = max(0, min(int(inp.get("day_start_hour") or 9), 23)) * 60
    day_end = max(day_start // 60 + 1, min(int(inp.get("day_end_hour") or 18), 24)) * 60
    min_minutes = max(5, int(inp.get("min_minutes") or 30))
    occ = state.events.occurrences(start_d, end_d) if hasattr(state, "events") else []
    busy: dict[str, list[tuple[int, int]]] = {}
    for o in occ:
        day = o.get("occurrence_date")
        if o.get("all_day"):
            busy.setdefault(day, []).append((day_start, day_end))
            continue
        try:
            s = datetime.fromisoformat(o["occurrence_start"])
            e = datetime.fromisoformat(o["occurrence_end"])
        except (ValueError, KeyError):
            continue
        busy.setdefault(day, []).append((s.hour * 60 + s.minute, e.hour * 60 + e.minute))

    days = []
    cursor_day = start_d
    while cursor_day <= end_d:
        ds = cursor_day.isoformat()
        intervals = sorted(busy.get(ds, []))
        free = []
        cursor = day_start
        for bs, be in intervals:
            bs, be = max(bs, day_start), min(be, day_end)
            if bs > cursor and bs - cursor >= min_minutes:
                free.append((cursor, bs))
            cursor = max(cursor, be)
        if day_end - cursor >= min_minutes:
            free.append((cursor, day_end))
        days.append(
            {
                "date": ds,
                "free": [
                    {
                        "start": f"{ds}T{fs // 60:02d}:{fs % 60:02d}",
                        "end": f"{ds}T{fe // 60:02d}:{fe % 60:02d}",
                        "minutes": fe - fs,
                    }
                    for fs, fe in free
                ],
            }
        )
        cursor_day += timedelta(days=1)
    return {
        "start": start_d.isoformat(),
        "end": end_d.isoformat(),
        "working_hours": f"{day_start // 60:02d}:00-{day_end // 60:02d}:00",
        "days": days,
    }


async def week_agenda(state, thread_id, inp) -> dict:
    """Events + due tasks laid out day by day across a range."""
    start_d, end_d = _valid_range(str(inp.get("start") or ""), str(inp.get("end") or ""), 62)
    events = state.events.occurrences(start_d, end_d) if hasattr(state, "events") else []
    tasks = await state.db.execute_fetchall(
        "SELECT text, path, due FROM tasks WHERE status != 'done' AND due BETWEEN ? AND ? ORDER BY due",
        (start_d.isoformat(), end_d.isoformat()),
    )
    agenda: dict[str, dict] = {}
    cursor = start_d
    while cursor <= end_d:
        agenda[cursor.isoformat()] = {"events": [], "due_tasks": []}
        cursor += timedelta(days=1)
    for o in events:
        agenda.setdefault(o["occurrence_date"], {"events": [], "due_tasks": []})["events"].append(
            {"title": o["title"], "start": o.get("occurrence_start"), "all_day": o.get("all_day")}
        )
    for task in tasks:
        agenda.setdefault(task["due"], {"events": [], "due_tasks": []})["due_tasks"].append(
            {"text": task["text"], "path": task["path"]}
        )
    return {"start": start_d.isoformat(), "end": end_d.isoformat(), "agenda": agenda}


async def list_media(state, thread_id, inp) -> dict:
    """Media items by status — 'what am I watching', 'my watchlist', 'finished'."""
    status = str(inp.get("status") or "").strip().lower()
    if status == "watchlist":
        status = "planned"
    media_type = str(inp.get("media_type") or "").strip().lower()
    clauses, params = [], []
    if status:
        if status not in media_items.STATUSES:
            raise NoteError(400, f"status must be one of: {', '.join(media_items.STATUSES)} (or watchlist)")
        clauses.append("status = ?")
        params.append(status)
    if media_type:
        if media_type not in media_items.MEDIA_TYPES:
            raise NoteError(400, f"media_type must be one of: {', '.join(media_items.MEDIA_TYPES)}")
        clauses.append("media_type = ?")
        params.append(media_type)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = await state.db.execute_fetchall(
        f"SELECT path, COALESCE(NULLIF(alias, ''), title) AS title, media_type, status, creator, "
        f"year, rating, total FROM media_items {where} "
        "ORDER BY modified DESC, title COLLATE NOCASE LIMIT 200",
        tuple(params),
    )
    if not rows:
        note = await _empty_media_note(state, status, media_type)
        return {"items": [], "count": 0, "note": note}
    return {"items": [dict(r) for r in rows], "count": len(rows)}


async def _empty_media_note(state, status: str, media_type: str) -> str:
    """Name what IS tracked, so status='active' over a vault of planned items
    says so instead of reading as "you track no media"."""
    rows = await state.db.execute_fetchall(
        "SELECT status, media_type, COUNT(*) AS n FROM media_items GROUP BY status, media_type"
    )
    by_status: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for row in rows:
        by_status[row["status"]] = by_status.get(row["status"], 0) + row["n"]
        by_type[row["media_type"]] = by_type.get(row["media_type"], 0) + row["n"]
    filters = ", ".join(
        f"{key}={value}" for key, value in (("status", status), ("media_type", media_type)) if value
    )
    tracked = "; ".join(
        ", ".join(f"{name} {n}" for name, n in sorted(counts.items()))
        for counts in (by_status, by_type)
    )
    return _empty_note(
        f"No media matched {filters}." if filters else "",
        sum(by_status.values()),
        "tracked media items",
        f"Tracked by status and type: {tracked}." if rows else "",
    )


async def media_diary(state, thread_id, inp) -> dict:
    """Recent media log activity — 'what have I been watching lately'."""
    limit = max(1, min(int(inp.get("limit") or 30), 200))
    rows = await state.db.execute_fetchall(
        """SELECT l.at, l.kind, l.value, l.text, l.path,
                  COALESCE(NULLIF(i.alias, ''), i.title) AS title, i.media_type, i.rating, i.status
           FROM media_log l JOIN media_items i ON i.path = l.path
           ORDER BY l.at DESC, l.path, l.line DESC LIMIT ?""",
        (limit,),
    )
    if not rows:
        tracked = await state.db.execute_fetchall("SELECT COUNT(*) AS n FROM media_items")
        n = int(tracked[0]["n"]) if tracked else 0
        note = (
            f"No progress, rating or status changes are logged for the {n} tracked "
            "item(s) yet; list_media shows them."
            if n
            else "The vault tracks no media, so there is no activity."
        )
        return {"activity": [], "note": note}
    return {"activity": [dict(r) for r in rows]}


async def update_media_by_title(state, thread_id, inp) -> dict:
    """Set a media item's status / progress / rating by TITLE — 'I'm on S01E05
    of Severance', 'add Dune to my watchlist'. Ambiguous title returns candidates."""
    title = " ".join(str(inp.get("title") or "").split()).strip()
    if not title:
        raise NoteError(400, "Give the media title")
    rows = [
        dict(r)
        for r in await state.db.execute_fetchall(
            "SELECT path, COALESCE(NULLIF(alias, ''), title) AS title FROM media_items "
            "WHERE lower(COALESCE(NULLIF(alias, ''), title)) LIKE ?",
            (f"%{title.lower()}%",),
        )
    ]
    if not rows:
        raise NoteError(404, f"No media item matches “{title}”")
    exact = [r for r in rows if r["title"].strip().lower() == title.lower()]
    chosen = exact[0] if exact else (rows[0] if len(rows) == 1 else None)
    if chosen is None:
        return {"ambiguous": True, "candidates": [r["title"] for r in rows[:8]], "note": "Several match — say which."}
    path = chosen["path"]
    status = inp.get("status")
    if status == "watchlist":
        status = "planned"
    progress = inp.get("progress")
    entry = {"kind": "progress", "value": str(progress)} if progress else None
    current = state.store.read(path)
    try:
        updated, summary = media_items.apply_update(
            current,
            rating=inp.get("rating"),
            status=status,
            entry=entry,
            alias=None,
            today=local_today(state),
        )
    except ValueError as exc:
        raise NoteError(400, str(exc))
    written = await _write_note(state, path, updated, previous=current)
    return await _record(
        state,
        thread_id,
        "update_media_by_title",
        f'{summary} — "{chosen["title"]}"',
        [path],
        ["notes", "search", "media"],
        {"path": path, "revision": content_hash(written), "summary": summary},
    )


# --- Round 3: in-note structure, tasks-from-prose, study -------------------


def _table_cells(row: str) -> list[str]:
    r = row.strip()
    if r.startswith("|"):
        r = r[1:]
    if r.endswith("|"):
        r = r[:-1]
    return [c.strip() for c in r.split("|")]


def _find_table(lines: list[str], lo: int, hi: int) -> tuple[int, int, list[str]] | None:
    i = lo
    while i < hi:
        if "|" in lines[i] and lines[i].strip():
            j = i
            while j < hi and "|" in lines[j] and lines[j].strip():
                j += 1
            if j - i >= 2 and re.search(r"-{2,}", lines[i + 1]):
                return i, j, lines[i:j]
            i = j
        else:
            i += 1
    return None


def _emit_table(header: list[str], rows: list[list[str]]) -> list[str]:
    cols = len(header)
    grid = [header, *rows]
    widths = [max(len(r[c]) if c < len(r) else 0 for r in grid) for c in range(cols)]
    widths = [max(3, w) for w in widths]

    def fmt(cells: list[str]) -> str:
        return "| " + " | ".join((cells[c] if c < len(cells) else "").ljust(widths[c]) for c in range(cols)) + " |"

    sep = "| " + " | ".join("-" * widths[c] for c in range(cols)) + " |"
    return [fmt(header), sep, *[fmt(r) for r in rows]]


def _scope_bounds(lines: list[str], heading: str) -> tuple[int, int]:
    if not heading:
        return 0, len(lines)
    section = _find_heading_section(lines, heading)
    if section is None:
        raise NoteError(404, f"No section titled “{heading}”")
    return section[0] + 1, section[2]


async def format_table(state, thread_id, inp) -> dict:
    """Align, sort, or add a row to a markdown table (scoped to a heading)."""
    path = inp["path"]
    current = _require_revision(state, path, inp.get("base_revision"))
    meta, body = fm.parse(current)
    lines = body.splitlines()
    lo, hi = _scope_bounds(lines, str(inp.get("heading") or "").strip())
    found = _find_table(lines, lo, hi)
    if not found:
        raise NoteError(404, "No markdown table found in scope")
    ts, te, block = found
    header = _table_cells(block[0])
    rows = [_table_cells(r) for r in block[2:]]
    op = str(inp.get("operation") or "align").lower()
    if op == "sort":
        col = max(0, int(inp.get("column") or 0))
        rows.sort(key=lambda r: (r[col].lower() if col < len(r) else ""))
    elif op == "add_row":
        cells = [str(c) for c in (inp.get("row") or [])]
        if not cells:
            raise NoteError(400, "Give `row` (an array of cell values) to add")
        rows.append(cells)
    elif op != "align":
        raise NoteError(400, "operation must be align, sort, or add_row")
    new_lines = lines[:ts] + _emit_table(header, rows) + lines[te:]
    trailing = "\n" if body.endswith("\n") else ""
    normalized = await _write_note(
        state, path, fm.serialize(meta, "\n".join(new_lines) + trailing), previous=current
    )
    return await _record(
        state, thread_id, "format_table", f"Updated a table in {path}", [path],
        ["notes", "search"], {"path": path, "revision": content_hash(normalized)},
    )


async def sort_list(state, thread_id, inp) -> dict:
    """Sort (and optionally dedupe) a bullet list, scoped to a heading."""
    path = inp["path"]
    current = _require_revision(state, path, inp.get("base_revision"))
    meta, body = fm.parse(current)
    lines = body.splitlines()
    lo, hi = _scope_bounds(lines, str(inp.get("heading") or "").strip())
    bullet = re.compile(r"^(\s*)([-*+])\s+(.*)$")
    run_start = run_end = None
    for i in range(lo, hi):
        if bullet.match(lines[i]):
            if run_start is None:
                run_start = i
            run_end = i + 1
        elif run_start is not None:
            break
    if run_start is None:
        raise NoteError(404, "No bullet list found in scope")
    items = lines[run_start:run_end]
    if inp.get("dedupe"):
        seen: set[str] = set()
        deduped = []
        for line in items:
            key = bullet.match(line).group(3).strip().lower()
            if key not in seen:
                seen.add(key)
                deduped.append(line)
        items = deduped
    items.sort(key=lambda line: bullet.match(line).group(3).strip().lower(), reverse=bool(inp.get("descending")))
    new_lines = lines[:run_start] + items + lines[run_end:]
    trailing = "\n" if body.endswith("\n") else ""
    normalized = await _write_note(
        state, path, fm.serialize(meta, "\n".join(new_lines) + trailing), previous=current
    )
    return await _record(
        state, thread_id, "sort_list", f"Sorted a list in {path}", [path],
        ["notes", "search"], {"path": path, "revision": content_hash(normalized)},
    )


async def insert_toc(state, thread_id, inp) -> dict:
    """Insert a table of contents built from the note's headings."""
    path = inp["path"]
    current = _require_revision(state, path, inp.get("base_revision"))
    meta, body = fm.parse(current)
    lines = body.splitlines()
    toc = []
    in_fence = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = re.match(r"^(#{2,6})\s+(.+?)\s*$", line)  # skip the H1 title
        if match:
            toc.append(f"{'  ' * (len(match.group(1)) - 2)}- {match.group(2).strip()}")
    if not toc:
        raise NoteError(400, "The note has no ## headings to build a contents list from")
    insert_at = 0
    for i, line in enumerate(lines):
        if re.match(r"^#\s+", line):
            insert_at = i + 1
            break
    block = ["", "## Contents", "", *toc, ""]
    new_lines = lines[:insert_at] + block + lines[insert_at:]
    trailing = "\n" if body.endswith("\n") else ""
    normalized = await _write_note(
        state, path, fm.serialize(meta, "\n".join(new_lines) + trailing), previous=current
    )
    return await _record(
        state, thread_id, "insert_toc", f"Inserted a table of contents in {path}", [path],
        ["notes", "search"], {"path": path, "revision": content_hash(normalized)},
    )


async def embed_attachment(state, thread_id, inp) -> dict:
    """Insert an attachment reference (image) into a note."""
    path = inp["path"]
    if not state.store.exists(path):
        raise NoteError(404, "Note not found")
    attachment = str(inp.get("attachment") or "").strip()
    if not attachment:
        raise NoteError(400, "Give the attachment name")
    root = state.settings.vault_dir / NoteStore.ATTACHMENT_DIR
    files = [p.name for p in root.glob("*") if p.is_file()] if root.is_dir() else []
    needle = attachment.rsplit("/", 1)[-1].lower()
    match = next((f for f in files if f.lower() == needle), None) or next(
        (f for f in files if needle in f.lower()), None
    )
    if not match:
        raise NoteError(404, f"No attachment matching “{attachment}”")
    ref = f"![{str(inp.get('alt') or '').strip() or match}](/api/attachments/{match})"
    current = state.store.read(path)
    meta, body = fm.parse(current)
    heading = str(inp.get("heading") or "").strip()
    if heading:
        lines = body.splitlines()
        section = _find_heading_section(lines, heading)
        if section is None:
            body = body.rstrip("\n") + f"\n\n## {heading}\n\n{ref}\n"
        else:
            insert_at = section[2]
            while insert_at > section[0] + 1 and not lines[insert_at - 1].strip():
                insert_at -= 1
            lines[insert_at:insert_at] = ["", ref]
            body = "\n".join(lines)
    else:
        body = body.rstrip("\n") + f"\n\n{ref}\n"
    normalized = await _write_note(state, path, fm.serialize(meta, body), previous=current)
    return await _record(
        state, thread_id, "embed_attachment", f"Embedded {match} in {path}", [path],
        ["notes", "search"], {"path": path, "attachment": match, "revision": content_hash(normalized)},
    )


async def extract_action_items(state, thread_id, inp) -> dict:
    """Pull action items out of a note (or one section) onto a task board.

    A task that already has metadata moves, keeping its id, reminder,
    description and milestones. A bare checkbox becomes a new task in the same
    state, and a plain bullet does only with `include_bullets` (the default
    when the scope has no checkbox, so attendee lists and links stay put).
    Checked-off items stay in the note unless `include_done`."""
    path = inp["path"]
    board = str(inp.get("board") or "").strip()
    if not board:
        raise NoteError(400, "Give the destination board (a note path)")
    if not state.store.exists(board):
        raise NoteError(404, "Destination board not found")
    if state.store.exists(path) and state.store.resolve(board).samefile(
        state.store.resolve(path)
    ):
        # Rewriting the note would erase the tasks just appended to it.
        raise NoteError(
            400, "The board is this note; use convert_bullets_to_tasks to make tasks in place"
        )
    current = _require_revision(state, path, inp.get("base_revision"))
    checked = content_hash(current)
    meta, body = fm.parse(current)
    lines = note_parse.split_lines(body)
    lo, hi = _scope_bounds(lines, str(inp.get("heading") or "").strip())
    remove = inp.get("remove", True)
    include_done = bool(inp.get("include_done"))
    tasks = {task.line - 1: task for task in note_parse.extract_tasks(body)}
    indexed = {
        row["id"]
        for row in await state.db.execute_fetchall("SELECT id FROM tasks WHERE path = ?", (path,))
    }
    checkbox = re.compile(r"^\s*[-*]\s+\[[ xX/]\]")
    bullet = re.compile(r"^\s*[-*]\s+(.*\S)\s*$")
    moves: list[str] = []
    items: list[dict] = []
    bullets: list[dict] = []
    has_checkbox = False
    skipped_done = 0
    in_fence = False
    for i in range(lo, hi):
        line = lines[i].rstrip("\r")
        if note_parse.FENCE_RE.match(line.strip()):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if checkbox.match(line):
            has_checkbox = True
            task = tasks.get(i)
            if task is None or not task.text:
                continue
            if task.done and not include_done:
                skipped_done += 1
                continue
            # Only a task the index holds at this path can move: a legacy line
            # has no id, and a repeated id belongs to its first copy.
            movable = task.meta_line is not None and task.id in indexed
            if remove and movable and task.id not in moves:
                moves.append(task.id)
                continue
            items.append(
                {
                    "line": i,
                    "meta_line": None if task.meta_line is None else task.meta_line - 1,
                    "text": task.text,
                    "status": task.status,
                    "due": task.due,
                    "due_visible": task.due_visible,
                }
            )
            continue
        match = bullet.match(line)
        if match:
            due = note_parse.DUE_RE.search(match.group(1))
            text = note_parse.DUE_RE.sub("", match.group(1)).strip()
            if text:
                bullets.append(
                    {
                        "line": i,
                        "meta_line": None,
                        "text": text,
                        "status": "todo",
                        "due": due.group(1) if due else None,
                        "due_visible": bool(due),
                    }
                )
    include_bullets = inp.get("include_bullets")
    if include_bullets is None:
        include_bullets = not has_checkbox
    if include_bullets:
        items = sorted(items + bullets, key=lambda item: item["line"])
    skipped: dict[str, int] = {}
    if skipped_done:
        skipped["skipped_done"] = skipped_done
    if bullets and not include_bullets:
        skipped["skipped_bullets"] = len(bullets)
    if not items and not moves:
        heading = str(inp.get("heading") or "").strip()
        where = f"the “{heading}” section of {path}" if heading else path
        left = []
        if skipped_done:
            left.append(f"{skipped_done} done item{'' if skipped_done == 1 else 's'} (include_done)")
        if bullets and not include_bullets:
            left.append(f"{len(bullets)} plain bullet{'' if len(bullets) == 1 else 's'} (include_bullets)")
        note = (
            f"Nothing to extract from {where}; left in place: {' and '.join(left)}."
            if left
            else f"No bullet or checkbox items in {where}; nothing was moved."
        )
        return {"extracted": 0, "note": note, **skipped}
    for item in items:
        try:
            item["due"] = parse_user_date(item["due"]) if item["due"] else None
        except NoteError:
            item["due"] = None  # not a real date; create_task would refuse it

    if remove and meta.error is not None:
        # The note is rewritten after its items are on the board, and a block
        # that does not load cannot be written back: every retry would add the
        # same tasks to the board again.
        raise NoteError(
            422,
            f"The frontmatter of {path} is not valid YAML ({meta.error}); repair it, "
            "or pass remove=false to copy the items and leave the note as it is",
        )
    new_source = None
    if remove and items:
        drop = {item["line"] for item in items}
        drop |= {item["meta_line"] for item in items if item["meta_line"] is not None}
        trailing = "\n" if body.endswith("\n") else ""
        kept = "\n".join(line for i, line in enumerate(lines) if i not in drop) + trailing
        new_source = fm.serialize(meta, kept)

    total = len(items) + len(moves)
    refresh = ["notes", "tasks", "calendar", "search"]
    written: list[str] = []
    applied = 0
    try:
        # New tasks land on the board before their lines leave the note, and
        # move_tasks re-reads the note, so a failure leaves an item in both
        # places, never in neither.
        for item in items:
            await create_task_record(
                state,
                text=item["text"],
                path=board,
                status=item["status"],
                due=item["due"],
                due_visible=item["due_visible"],
            )
            applied += 1
            written = [board]
        if new_source is not None:
            _require_unchanged(state, path, checked)
            await _write_note(state, path, new_source, previous=current)
            written = [path, board]
        if moves:
            await move_tasks(state, moves, board)
            applied += len(moves)
            written = [path, board]
    except (NoteError, fm.FrontmatterError) as err:
        if not written:
            raise
        detail = err.detail if isinstance(err, NoteError) else str(err)
        return await _record(
            state,
            thread_id,
            "extract_action_items",
            f"Extracted {applied} of {total} action item(s) from {path} to {board} (partial)",
            written,
            refresh,
            {
                "error": f"{detail} — {applied} of {total} are on {board}, saved as one "
                "undoable action; do not resend them",
                "extracted": applied,
                "board": board,
            },
        )
    return await _record(
        state,
        thread_id,
        "extract_action_items",
        f"Extracted {total} action item(s) from {path} to {board}",
        [path, board] if remove else [board],
        refresh,
        {"extracted": total, "moved": len(moves), "board": board, **skipped},
    )


async def study_stats(state, thread_id, inp) -> dict:
    """Cross-deck flashcard stats: totals, due, retention, per deck."""
    decks = state.flashcards.list()
    total_cards = total_reps = total_lapses = total_due = 0
    per_deck = []
    for deck in decks:
        stats = flashcard_stats(deck)
        cards = deck.get("cards") or []
        reps = sum(int(c.get("reps") or 0) for c in cards)
        lapses = sum(int(c.get("lapses") or 0) for c in cards)
        total_cards += int(stats.get("total") or 0)
        total_reps += reps
        total_lapses += lapses
        total_due += int(stats.get("due") or 0)
        per_deck.append(
            {
                "name": deck.get("name"),
                "total": stats.get("total"),
                "due": stats.get("due"),
                "known": stats.get("known"),
                "reps": reps,
                "lapses": lapses,
            }
        )
    return {
        "decks": len(decks),
        "total_cards": total_cards,
        "due": total_due,
        "reviews": total_reps,
        "lapses": total_lapses,
        "retention": round(1 - (total_lapses / total_reps), 3) if total_reps else None,
        "per_deck": per_deck,
    }


# --- Round 2: note conveniences --------------------------------------------


def _find_heading_section(lines: list[str], heading: str) -> tuple[int, int, int] | None:
    """(start, level, end) of a `## heading` section, fence-aware. `end` is the
    first line at a heading level ≤ this one (so nested subsections travel with
    it), else the end of the body. None if the heading isn't found."""
    needle = heading.strip().lower()
    in_fence = False
    start = level = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if match and match.group(2).strip().lower() == needle:
            start, level = i, len(match.group(1))
            break
    if start is None:
        return None
    end = len(lines)
    in_fence = False
    for j in range(start + 1, len(lines)):
        stripped = lines[j].strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        heading_match = re.match(r"^(#{1,6})\s+", lines[j])
        if heading_match and len(heading_match.group(1)) <= level:
            end = j
            break
    return (start, level, end)


async def append_to_note(state, thread_id, inp) -> dict:
    """Add text to a note without rewriting it: at the end (default), under a
    named `## heading`, or at the top."""
    path = inp["path"]
    text = str(inp.get("text") or "")
    if not text.strip():
        raise NoteError(400, "Provide text to add")
    current = _require_revision(state, path, inp.get("base_revision"))
    meta, body = fm.parse(current)
    block = text.rstrip("\n")
    heading = str(inp.get("heading") or "").strip()
    position = str(inp.get("position") or "").lower()
    if heading:
        lines = body.splitlines()
        section = _find_heading_section(lines, heading)
        if section is None:
            body = body.rstrip("\n") + f"\n\n## {heading}\n\n{block}\n"
        else:
            _start, _level, end = section
            insert_at = end
            while insert_at > section[0] + 1 and not lines[insert_at - 1].strip():
                insert_at -= 1
            lines[insert_at:insert_at] = ["", block]
            body = "\n".join(lines)
    elif position == "top":
        body = f"{block}\n\n" + body.lstrip("\n")
    else:
        body = body.rstrip("\n") + f"\n\n{block}\n"
    normalized = await _write_note(state, path, fm.serialize(meta, body), previous=current)
    return await _record(
        state,
        thread_id,
        "append_to_note",
        f"Added to {path}" + (f" under ## {heading}" if heading else ""),
        [path],
        ["notes", "search", "tasks"],
        {"path": path, "revision": content_hash(normalized)},
    )


async def duplicate_note(state, thread_id, inp) -> dict:
    """Clone a note (fresh task IDs so the index stays consistent)."""
    path = inp["path"]
    content = state.store.read(path)
    target = inp.get("target_path")
    if target:
        new_path = _unique_path(state, target)
    else:
        stem = path[:-3] if path.endswith(".md") else path
        new_path = _unique_path(state, f"{stem} copy.md")
    fresh = task_boards.strip_board_flag(note_parse.regenerate_task_ids(content))
    # The default '… copy' name is the source note's own, which may predate
    # the rules for new names; only a caller-chosen target is checked.
    created = await _write_note(
        state, new_path, fresh, create=True, check_name=bool(target)
    )
    return await _record(
        state,
        thread_id,
        "duplicate_note",
        f"Duplicated {path} to {new_path}",
        [new_path],
        ["notes", "search", "tasks"],
        {"path": new_path, "source": path, "revision": content_hash(created)},
        navigate=new_path,
    )


async def rename_note(state, thread_id, inp) -> dict:
    """Rename a note by giving it a new title (recomputes the filename) and
    rewrite inbound [[wikilinks]] to it."""
    path = inp["path"]
    new_title = " ".join(str(inp.get("new_title") or "").split()).strip()
    if not new_title:
        raise NoteError(400, "Give the new title")
    if path.startswith("daily/") or not NoteStore.title_follows_path(path):
        raise NoteError(
            400, "This note's title isn't editable as a filename (daily/media/templates) — edit it directly"
        )
    _require_revision(state, path, inp.get("base_revision"))
    folder = task_boards.folder_of(path)
    slug = re.sub(r"[^\w\- ]", "", new_title).strip() or "Untitled"
    new_path = f"{folder + '/' if folder else ''}{slug}.md"
    if new_path == path:
        return {"path": path, "unchanged": True}
    # A case-only retitle renames the note onto its own identity, which is
    # allowed; anything else must not collide with another note.
    if names.identity_key(new_path) != names.identity_key(path):
        new_path = _unique_path(state, new_path)
    state.store.rename(path, new_path)
    await state.indexer.rename_note(path, new_path)
    # The same rewrite a rename from the editor or the sidebar gets
    # (index/links.py); the action record commits and announces the linkers.
    affected = [path, new_path]
    affected += await _rewire_links(state, [(path, new_path)], affected)
    if sidebar_order.rename_paths(state.settings.vault_dir, notes=[(path, new_path)]):
        affected.append(CONFIG_PATH)
    return await _record(
        state,
        thread_id,
        "rename_note",
        f"Renamed {path} to {new_path}",
        affected,
        ["notes", "search", "tasks", "calendar"],
        {"path": new_path, "old_path": path},
        navigate=new_path,
    )


def outline_headings(body: str) -> list[dict]:
    """A note body's heading map. Fenced blocks are skipped, so a `# comment`
    inside code never reads as a section. Shared with the agent's open-note
    context block."""
    headings = []
    in_fence = False
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if match:
            headings.append({"level": len(match.group(1)), "text": match.group(2).strip()})
    return headings


async def get_outline(state, thread_id, inp) -> dict:
    """A note's heading map + word count, to navigate long notes without pulling
    the whole body."""
    path = inp["path"]
    content = state.store.read(path)
    meta, body = fm.parse(content)
    return {
        "path": path,
        # Enough to edit a note too long to read in one call: the gated tools
        # (patch_note, move_section, ...) take this as base_revision.
        "revision": content_hash(content),
        "title": str(meta.get("title") or Path(path).stem),
        "headings": outline_headings(body),
        "word_count": len(re.findall(r"\S+", body)),
        "char_count": len(body),
    }


async def move_section(state, thread_id, inp) -> dict:
    """Move a `## ` section (with its subsections) out to another note."""
    source = inp["path"]
    heading = str(inp.get("heading") or "").strip()
    if not heading:
        raise NoteError(400, "Name the section heading to move")
    target = str(inp.get("target_path") or "").strip()
    if not target or target == source:
        raise NoteError(400, "Give a different target_path to move the section into")
    if not state.store.exists(target):
        raise NoteError(404, "Target note not found")
    current = _require_revision(state, source, inp.get("base_revision"))
    meta, body = fm.parse(current)
    lines = body.splitlines()
    section = _find_heading_section(lines, heading)
    if section is None:
        raise NoteError(404, f"No section titled “{heading}” in {source}")
    start, _level, end = section
    section_text = "\n".join(lines[start:end]).strip() + "\n"
    remaining = "\n".join(lines[:start] + lines[end:]).rstrip("\n") + "\n"
    await _write_note(state, source, fm.serialize(meta, remaining), previous=current)
    target_current = state.store.read(target)
    tmeta, tbody = fm.parse(target_current)
    await _write_note(
        state,
        target,
        fm.serialize(tmeta, tbody.rstrip("\n") + "\n\n" + section_text),
        previous=target_current,
    )
    return await _record(
        state,
        thread_id,
        "move_section",
        f"Moved section “{heading}” from {source} to {target}",
        [source, target],
        ["notes", "search", "tasks", "calendar"],
        {"source": source, "target": target},
    )


async def convert_bullets_to_tasks(state, thread_id, inp) -> dict:
    """Turn plain `- item` bullets into `- [ ] item` tasks, optionally scoped to
    one `## heading` section, then mint fresh task IDs."""
    path = inp["path"]
    _require_revision(state, path, inp.get("base_revision"))
    meta, body = fm.parse(state.store.read(path))
    lines = body.splitlines()
    heading = str(inp.get("heading") or "").strip()
    lo, hi = 0, len(lines)
    if heading:
        section = _find_heading_section(lines, heading)
        if section is None:
            raise NoteError(404, f"No section titled “{heading}” in {path}")
        lo, hi = section[0] + 1, section[2]
    bullet = re.compile(r"^(\s*)-\s+(?!\[[ xX/]\]\s)(\S.*)$")
    in_fence = False
    converted = 0
    for i in range(lo, hi):
        stripped = lines[i].strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = bullet.match(lines[i])
        if match:
            lines[i] = f"{match.group(1)}- [ ] {match.group(2)}"
            converted += 1
    if not converted:
        where = f"the “{heading}” section of {path}" if heading else path
        return {
            "converted": 0,
            "note": f"No plain `- ` bullets in {where} to convert; existing checkboxes "
            "and lines inside code fences are left alone.",
        }
    new_body = "\n".join(lines) + ("\n" if body.endswith("\n") else "")
    written = await _write_note(state, path, fm.serialize(meta, new_body))
    return await _record(
        state,
        thread_id,
        "convert_bullets_to_tasks",
        f"Converted {converted} bullet(s) to tasks in {path}",
        [path],
        ["notes", "search", "tasks", "calendar"],
        {"converted": converted, "revision": content_hash(written)},
    )


async def save_note_as_template(state, thread_id, inp) -> dict:
    """Save a note as a reusable template under templates/."""
    path = inp["path"]
    content = state.store.read(path)
    name = re.sub(r"[^\w\- ]", "", str(inp.get("name") or Path(path).stem)).strip() or "template"
    tpl_path = _unique_path(state, f"templates/{name}.md")
    # Not _write_note: metadata minted into a template would be copied, ids
    # and all, into every note made from it.
    created = state.store.create(tpl_path, tpl.strip_managed_frontmatter(content))
    await state.indexer.index_note(tpl_path, created)
    return await _record(
        state,
        thread_id,
        "save_note_as_template",
        f"Saved {path} as template “{name}”",
        [tpl_path],
        ["notes", "search"],
        {"path": tpl_path, "name": name},
    )


# --- Round 2: calendar, media, connections, settings -----------------------


async def reschedule_event(state, thread_id, inp) -> dict:
    """Move an event/reminder to a new time, addressing it by title — hiding the
    id+revision dance. Best match then act; duration is preserved if no end."""
    needle = " ".join(str(inp.get("title") or "").split()).lower()
    if not needle:
        raise NoteError(400, "Give the event title to reschedule")
    new_start = str(inp.get("start") or "").strip()
    if not new_start:
        raise NoteError(400, "Give the new start (YYYY-MM-DD or YYYY-MM-DDTHH:MM)")
    matches = [e for e in state.events.all() if needle in str(e.get("title") or "").lower()]
    if not matches:
        raise NoteError(404, f"No event matches “{inp.get('title')}”")
    matches.sort(
        key=lambda e: (str(e.get("title") or "").strip().lower() == needle, str(e.get("start") or "")),
        reverse=True,
    )
    event = matches[0]
    all_day = bool(event.get("all_day"))
    new_end = inp.get("end")
    if all_day:
        if not new_end:
            try:
                span = date.fromisoformat(event["end"]) - date.fromisoformat(event["start"])
                new_end = (date.fromisoformat(new_start) + span).isoformat()
            except ValueError:
                new_end = None
    else:
        tz = _event_zone(state, event.get("timezone"))
        moved_start, moved_end = _moved(event, new_start, tz)
        if new_end:
            try:
                moved_end = parse_user_datetime(str(new_end), tz).isoformat(timespec="minutes")
            except ValueError as exc:
                raise NoteError(400, "The new end must be YYYY-MM-DDTHH:MM") from exc
        new_start, new_end = moved_start, moved_end
    values = state.events.normalize(
        title=event["title"],
        start=new_start,
        end=new_end,
        all_day=all_day,
        timezone_name=state.timezone,
        notes=event.get("notes"),
        note_path=event.get("note_path"),
        recurrence=event.get("recurrence"),
        reminders=event.get("reminders"),
        app_timezone=state.timezone,
    )
    updated = state.events.update(event["id"], values, event.get("revision"))
    state.autocommit.touch()
    return await _record(
        state,
        thread_id,
        "reschedule_event",
        f"Rescheduled “{event['title']}” to {new_start}",
        [EVENTS_PATH],
        ["calendar"],
        {"event": updated},
    )


async def edit_media_profile(state, thread_id, inp) -> dict:
    """Set the media hub's name, location, and/or favorites (≤4 tracked items).
    Avatar/banner images are set through the app, not here."""
    from ..api.media import MAX_FAVORITES, _PROFILE_KEY, _item

    # Validate (the only awaits) before reading the config, so the
    # read-merge-write below can't write back a snapshot a concurrent
    # settings save has since changed.
    favorites = inp.get("favorites")
    cleaned: list[str] | None = None
    if favorites is not None:
        if len(favorites) > MAX_FAVORITES:
            raise NoteError(400, f"Favorites are limited to {MAX_FAVORITES} items")
        cleaned = []
        for value in favorites:
            path = str(value)
            if path in cleaned:
                continue
            if await _item(state.db, path) is None:
                raise NoteError(400, f"Not a tracked media item: {path}")
            cleaned.append(path)
    config = _config(state)
    profile = dict(config.get(_PROFILE_KEY) or {})
    for field, cap in (("name", 80), ("location", 120)):
        value = inp.get(field)
        if value is None:
            continue
        value = " ".join(str(value).split())[:cap]
        if value:
            profile[field] = value
        else:
            profile.pop(field, None)
    if cleaned is not None:
        profile["favorites"] = cleaned
    config[_PROFILE_KEY] = profile
    atomic_json(state.settings.vault_dir / CONFIG_PATH, config)
    return await _record(
        state,
        thread_id,
        "edit_media_profile",
        "Updated the media profile",
        [CONFIG_PATH],
        ["media"],
        {"profile": {k: profile.get(k) for k in ("name", "location", "favorites")}},
    )


# Each logged item queues a Media job; a longer list is logged in batches.
MAX_BULK_MEDIA = 50


async def bulk_log_media(state, thread_id, inp) -> dict:
    """Track several media items at once (bare — no per-item cover lookup; add
    art later with set_media_art)."""
    items = inp.get("items") or []
    if not items:
        raise NoteError(400, "Give the media items to log")
    dropped = max(0, len(items) - MAX_BULK_MEDIA)
    items = items[:MAX_BULK_MEDIA]
    taken = {note.path for note in state.store.list_notes()}
    lowered = {p.lower() for p in taken}
    today = local_today(state)
    # Every item is built before the first is written: a bad rating on item k
    # used to fail the call with items 1..k-1 already created and unrecorded.
    planned: list[tuple[str, str, str]] = []
    errors: list[str] = []
    skipped: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        media_type = item.get("media_type")
        if not title or not media_type:
            continue
        slug_path = f"{media_items.MEDIA_DIR}/{media_items.media_slug(title)}.md"
        if slug_path.lower() in lowered:
            skipped.append(title)  # already tracked; skip rather than fail the whole batch
            continue
        try:
            content = media_items.build_item_content(
                title=title,
                media_type=media_type,
                status=item.get("status") or "planned",
                creator=item.get("creator"),
                year=item.get("year"),
                total=item.get("total"),
                rating=item.get("rating"),
                today=today,
            )
        except ValueError as exc:
            errors.append(f"{title}: {exc}")
            continue
        path = media_items.next_media_path(title, taken)
        taken.add(path)
        lowered.add(path.lower())
        planned.append((path, title, content))
    if errors:
        raise NoteError(400, f"Nothing was logged — {'; '.join(errors)}")
    truncated = {"truncated": dropped} if dropped else {}
    already = {"skipped": skipped} if skipped else {}
    if not planned:
        note = (
            f"Nothing logged: already tracked — {', '.join(skipped)}. "
            "update_media_by_title changes an existing item."
            if skipped
            else "Nothing logged: no item had both a title and a media_type."
        )
        return {"logged": 0, **already, "note": note, **truncated}
    created: list[dict] = []
    async with _Recording(
        state,
        thread_id,
        "bulk_log_media",
        ["notes", "search", "media"],
        len(planned),
        lambda k: f"Logged {k} media item{'' if k == 1 else 's'}",
    ) as rec:
        for path, title, content in planned:
            rec.current = title
            # A slugged media path, as in the Media create route.
            await _write_note(state, path, content, create=True, check_name=False)
            rec.done(path)
            created.append({"path": path, "title": title})
            media_theme.enrich_soon(state, path)
    if rec.failed:
        return rec.failed
    return await _record(
        state,
        thread_id,
        "bulk_log_media",
        f"Logged {len(created)} media item{'' if len(created) == 1 else 's'}",
        [item["path"] for item in created],
        ["notes", "search", "media"],
        {"items": created, **already, **truncated},
    )


async def link_notes(state, thread_id, inp) -> dict:
    """Create a bidirectional [[wikilink]] between two notes."""
    a, b = inp["path_a"], inp["path_b"]
    if a == b:
        raise NoteError(400, "Give two different notes")
    for path in (a, b):
        if not state.store.exists(path):
            raise NoteError(404, f"Note not found: {path}")
    affected: list[str] = []
    paths = [n.path for n in state.store.list_notes()]
    for source, other in ((a, b), (b, a)):
        link = f"[[{qualified_target(other, paths)}]]"
        content = state.store.read(source)
        if link in content:
            continue
        updated = content + ("" if content.endswith("\n") else "\n") + f"\n{link}\n"
        await _write_note(state, source, updated, previous=content)
        affected.append(source)
    if not affected:
        return {"linked": False, "already_present": True}
    return await _record(
        state,
        thread_id,
        "link_notes",
        f"Linked {a} and {b}",
        affected,
        ["notes", "search"],
        {"path_a": a, "path_b": b},
    )


async def set_omnia_housekeeping(state, thread_id, inp) -> dict:
    """Turn OMNIA's nightly conversation-list housekeeping on or off."""
    enabled = bool(inp.get("enabled"))
    state.omnia_housekeeping = enabled
    config = _config(state)
    config["omnia_housekeeping"] = enabled
    atomic_json(state.settings.vault_dir / CONFIG_PATH, config)
    return await _record(
        state,
        thread_id,
        "set_omnia_housekeeping",
        f"Turned nightly housekeeping {'on' if enabled else 'off'}",
        [CONFIG_PATH],
        [],
        {"omnia_housekeeping": enabled},
    )


# --- Round 2: task & organization conveniences -----------------------------


async def _resolve_task_by_text(
    state, text: str, path: str | None = None, *, prefer_done: bool = False
) -> dict:
    """Find the task a phrase refers to. Best match then act: exact text beats
    partial, open beats done (done beats open when reopening), most-recent
    breaks ties."""
    needle = " ".join(str(text or "").split()).lower()
    if not needle:
        raise NoteError(400, "Give the task text to match")
    clauses = ["lower(text) LIKE ?"]
    values: list[Any] = [f"%{needle}%"]
    if path:
        clause, bound = _task_scope_clause(path)
        clauses.append(clause)
        values.extend(bound)
    rows = [
        dict(row)
        for row in await state.db.execute_fetchall(
            f"SELECT * FROM tasks WHERE {' AND '.join(clauses)}", values
        )
    ]
    if not rows:
        raise NoteError(404, f"No task matches “{text}”")
    rows.sort(
        key=lambda r: (
            r["text"].strip().lower() == needle,
            (r["status"] == "done") == prefer_done,
            str(r.get("created_at") or ""),
        ),
        reverse=True,
    )
    return rows[0]


async def complete_task(state, thread_id, inp) -> dict:
    """Check off (or reopen) a task addressed by its text. Toggles, unless
    `done` says which: a repeated or late "check off X" must not reopen it."""
    done = inp.get("done")
    row = await _resolve_task_by_text(
        state, inp.get("text"), inp.get("path"), prefer_done=done is False
    )
    if done is not None and (row["status"] == "done") == bool(done):
        key = "already_done" if done else "already_open"
        return {"task": public_task(row), key: True}
    completing = row["status"] != "done"
    task = await update_task_record(state, row["id"], status="done" if completing else "todo")
    return await _record(
        state,
        thread_id,
        "complete_task",
        f"{'Completed' if completing else 'Reopened'} task “{task['text']}”",
        [task["path"]],
        ["notes", "tasks", "calendar", "search"],
        {"task": task},
    )


async def reschedule_task(state, thread_id, inp) -> dict:
    """Move a task's due date, addressing the task by its text."""
    due = inp.get("due")
    if not due:
        raise NoteError(400, "Give the new due date (YYYY-MM-DD)")
    row = await _resolve_task_by_text(state, inp.get("text"), inp.get("path"))
    task = await update_task_record(state, row["id"], due=due, due_visible=True)
    return await _record(
        state,
        thread_id,
        "reschedule_task",
        f"Rescheduled task “{task['text']}” to {due}",
        [task["path"]],
        ["notes", "tasks", "calendar", "search"],
        {"task": task},
    )


async def add_tasks(state, thread_id, inp) -> dict:
    """Create several tasks at once, in one commit."""
    items = inp.get("tasks") or []
    cleaned: list[tuple[str, str | None]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        text = " ".join(str(item.get("text") or "").split())
        if text:
            cleaned.append((text, item.get("due")))
    if not cleaned:
        raise NoteError(400, "Give at least one task with text")
    # Every due is checked before the first task is written: a bad one used
    # to fail the call with the tasks before it created, and the model's
    # retry of the whole list duplicated them.
    bad = []
    for text, due in cleaned:
        try:
            _validate_due(due)
        except NoteError as exc:
            bad.append(f"“{text}”: {exc.detail}")
    if bad:
        raise NoteError(400, f"No tasks were added — {'; '.join(bad)}")
    path = inp.get("path") or await _default_task_path(state, thread_id)
    created = []
    async with _Recording(
        state,
        thread_id,
        "add_tasks",
        ["notes", "tasks", "calendar", "search"],
        len(cleaned),
        lambda k: f"Added {k} task{'' if k == 1 else 's'}",
    ) as rec:
        for text, due in cleaned:
            rec.current = text
            task = await create_task_record(state, text=text, path=path, due=due)
            created.append(task)
            rec.done(task["path"])
    if rec.failed:
        return rec.failed
    paths = sorted(rec.paths)
    return await _record(
        state,
        thread_id,
        "add_tasks",
        f"Added {len(created)} task{'' if len(created) == 1 else 's'}",
        paths,
        ["notes", "tasks", "calendar", "search"],
        {"tasks": created},
    )


async def reorder_sidebar(state, thread_id, inp) -> dict:
    """Arrange notes or folders in the sidebar. Splices the requested items to
    the front of the existing order (unlisted entries keep sorting after)."""
    kind = str(inp.get("kind") or "").lower()
    if kind not in ("notes", "folders"):
        raise NoteError(400, "kind must be 'notes' or 'folders'")
    order = [str(x).strip() for x in (inp.get("order") or []) if str(x).strip()]
    if not order:
        raise NoteError(400, "Give the order — the items to place first, top to bottom")
    key = "note_order" if kind == "notes" else "folder_order"
    config = _config(state)
    current = [str(x) for x in (config.get(key) or []) if isinstance(x, str)]
    merged: list[str] = []
    seen: set[str] = set()
    for item in [*order, *current]:
        if item not in seen:
            seen.add(item)
            merged.append(item)
    config[key] = merged
    atomic_json(state.settings.vault_dir / CONFIG_PATH, config)
    return await _record(
        state,
        thread_id,
        "reorder_sidebar",
        f"Reordered {kind} in the sidebar",
        [CONFIG_PATH],
        ["notes"],
        {"kind": kind, "order": merged},
    )


async def archive_note(state, thread_id, inp) -> dict:
    """Move a note into archive/ in one step."""
    path = inp["path"]
    if not state.store.exists(path):
        raise NoteError(404, "Note not found")
    dest = _unique_path(state, f"archive/{path.split('/')[-1]}")
    state.store.rename(path, dest)
    await state.indexer.rename_note(path, dest)
    paths = [path, dest]
    paths += await _rewire_links(state, [(path, dest)], paths)
    if sidebar_order.rename_paths(state.settings.vault_dir, notes=[(path, dest)]):
        paths.append(CONFIG_PATH)
    return await _record(
        state,
        thread_id,
        "archive_note",
        f"Archived {path}",
        paths,
        ["notes", "search", "tasks", "calendar"],
        {"path": dest, "old_path": path},
        navigate=dest,
    )


async def move_notes(state, thread_id, inp) -> dict:
    """Move several notes into a folder in one commit."""
    paths = [str(p) for p in (inp.get("paths") or []) if p]
    if not paths:
        raise NoteError(400, "Give the notes to move")
    folder = str(inp.get("folder") or "").strip("/")
    for path in paths:
        if not state.store.exists(path):
            raise NoteError(404, f"Note not found: {path}")
    affected: list[str] = []
    moved = []
    for path in paths:
        dest = _unique_path(state, f"{folder + '/' if folder else ''}{path.split('/')[-1]}")
        if dest == path:
            continue
        state.store.rename(path, dest)
        await state.indexer.rename_note(path, dest)
        affected.extend([path, dest])
        moved.append({"from": path, "to": dest})
    if not moved:
        return {"moved": 0}
    pairs = [(m["from"], m["to"]) for m in moved]
    affected += await _rewire_links(state, pairs, affected)
    if sidebar_order.rename_paths(state.settings.vault_dir, notes=pairs):
        affected.append(CONFIG_PATH)
    return await _record(
        state,
        thread_id,
        "move_notes",
        f"Moved {len(moved)} note{'' if len(moved) == 1 else 's'} to {folder or 'the vault root'}",
        affected,
        ["notes", "search", "tasks", "calendar"],
        {"moved": moved},
    )


async def list_pending(state, thread_id, inp) -> dict:
    """What OMNIA has staged in the review tray — so it doesn't re-propose the
    same change and can tell the user what's waiting."""
    changes = await state.pending.list_changes()
    out = []
    for change in changes:
        described = await state.pending.describe(change, changes)
        preview = str(described.get("new_content") or "")
        out.append(
            {
                "id": described["id"],
                "op": described["op"],
                "path": described["path"],
                "new_path": described.get("new_path"),
                "rationale": described.get("rationale"),
                "created_at": described.get("created_at"),
                "stale": described.get("stale"),
                "preview": preview[:200] + ("…" if len(preview) > 200 else ""),
            }
        )
    if not out:
        note = "The review tray is empty; nothing is waiting for approval."
        return {"pending": [], "count": 0, "note": note}
    return {"pending": out, "count": len(out)}


async def list_recent_actions(state, thread_id, inp) -> dict:
    """OMNIA's recent action log across threads, so it can answer 'what did you
    change earlier?'. Safe fields only — never the raw result payloads."""
    limit = max(1, min(int(inp.get("limit") or 20), 100))
    actions = state.actions.list(limit=limit)
    safe = [
        {
            key: action.get(key)
            for key in ("id", "tool", "summary", "paths", "created_at", "undoable", "undone_at", "thread_id")
        }
        for action in actions
    ]
    if not safe:
        return {"actions": [], "note": "No OMNIA actions are on record."}
    return {"actions": safe}


async def undo_last_action(state, thread_id, inp) -> dict:
    """Revert the most recent undoable change from THIS conversation (or a named
    action_id). Undo is scoped to the thread that made the change."""
    action_id = inp.get("action_id")
    if not action_id:
        candidates = [
            action
            for action in state.actions.list(limit=100)
            if action.get("thread_id") == thread_id
            and action.get("undoable")
            and not action.get("undone_at")
        ]
        if not candidates:
            raise NoteError(404, "Nothing undoable in this conversation")
        action_id = candidates[0]["id"]  # list() is newest-first
    return await state.actions.undo(action_id, thread_id)


async def list_queued_reminders(state, thread_id, inp) -> dict:
    """Peek at reminders/alerts queued for the user and confirmation cards still
    awaiting them — without consuming any of them."""
    queue = getattr(state, "alerts", None)
    alerts = queue.peek() if queue is not None else []
    confirmations = (
        state.actions.pending_confirmations() if hasattr(state, "actions") else []
    )
    if not alerts and not confirmations:
        return {
            "queued_alerts": [],
            "pending_confirmations": [],
            "note": "Nothing is queued: no undelivered alerts and no confirmation cards waiting.",
        }
    return {"queued_alerts": alerts, "pending_confirmations": confirmations}


async def list_templates(state, thread_id, inp) -> dict:
    templates = tpl.list_templates(state.settings.vault_dir)
    if not templates:
        note = "There are no templates; save_note_as_template makes one."
        return {"templates": [], "note": note}
    return {"templates": templates}


async def remove_flashcards(state, thread_id, inp) -> dict:
    """Drop specific cards. OMNIA could delete a whole deck but not one bad
    card, even though FlashcardStore.delete_cards existed with no callers."""
    deck = _resolve_deck(state, inp["deck"])
    ids = inp.get("card_ids") or []
    fronts = {str(f).strip().lower() for f in (inp.get("fronts") or [])}
    if fronts:
        ids = list(ids) + [
            c["id"] for c in deck.get("cards") or []
            if str(c.get("front", "")).strip().lower() in fronts
        ]
    if not ids:
        raise NoteError(400, "Provide card_ids or fronts to remove")
    before = len(deck.get("cards") or [])
    updated = state.flashcards.delete_cards(deck["id"], ids)
    removed = before - len(updated.get("cards") or [])
    state.autocommit.touch()
    return await _record(
        state,
        thread_id,
        "remove_flashcards",
        f"Removed {removed} card(s) from “{deck['name']}”",
        [_deck_dir(deck["id"])],
        ["flashcards"],
        {"deck": deck["name"], "removed": removed},
    )


async def review_flashcard(state, thread_id, inp) -> dict:
    """Grade a card. Exposed over HTTP for the study screen but never to OMNIA,
    so it could build a deck and never help work through one."""
    deck = _resolve_deck(state, inp["deck"])
    grade = str(inp.get("grade") or "").lower()
    if grade not in ("again", "hard", "good", "easy"):
        raise NoteError(400, "grade must be again, hard, good or easy")
    card_id = inp.get("card_id")
    if not card_id:
        front = str(inp.get("front") or "").strip().lower()
        match = next(
            (c for c in deck.get("cards") or []
             if str(c.get("front", "")).strip().lower() == front),
            None,
        )
        if match is None:
            raise NoteError(404, "Provide card_id, or a front that matches a card")
        card_id = match["id"]
    card = state.flashcards.review(deck["id"], card_id, grade)
    state.autocommit.touch()
    # This writes the deck, so it needs a scoped commit and an Undo entry like
    # every other write. Registered as read/suggestion it had neither, which let
    # a proactive pass advance the spaced-repetition schedule irreversibly.
    return await _record(
        state,
        thread_id,
        "review_flashcard",
        f"Graded a card in “{deck['name']}” as {grade}",
        [_deck_dir(deck["id"])],
        ["flashcards"],
        {"deck": deck["name"], "card": card, "grade": grade},
    )


# --- vision ----------------------------------------------------------------


async def list_attachments(state, thread_id, inp) -> dict:
    """Attachments referenced by a note, or every attachment in the vault."""
    path = inp.get("note_path")
    if path:
        return {"note": path, "attachments": attachments_in(state.store.read(path))}
    root = state.settings.vault_dir / NoteStore.ATTACHMENT_DIR
    names = sorted(p.name for p in root.glob("*") if p.is_file()) if root.is_dir() else []
    if not names:
        note = "The vault has no attachments."
        return {"attachments": [], "total": 0, "orphans": 0, "note": note}
    # Which files does any note still point at? A removed image otherwise
    # lingers invisibly forever — this is what makes orphans findable.
    referenced: set[str] = set()
    for info in state.store.list_notes():
        for rel in attachments_in(state.store.read(info.path)):
            referenced.add(rel.rsplit("/", 1)[-1])
    # The media hub's avatar/banner and their picker histories live in config,
    # not in any note.
    from ..api.media import _PROFILE_KEY

    profile = _config(state).get(_PROFILE_KEY)
    if isinstance(profile, dict):
        urls = [profile.get("avatar"), profile.get("banner")]
        for key in ("avatars", "banners"):
            history = profile.get(key)
            if isinstance(history, list):
                urls.extend(history)
        marker = "/api/attachments/"
        for url in urls:
            if isinstance(url, str) and url.strip().startswith(marker):
                referenced.add(url.strip()[len(marker):])
    return {
        "attachments": [
            {
                "path": f"{NoteStore.ATTACHMENT_DIR}/{n}",
                "referenced": n in referenced,
            }
            for n in names[:200]
        ],
        "total": len(names),
        "orphans": sum(1 for n in names if n not in referenced),
    }


async def delete_attachment(state, thread_id, inp) -> dict:
    """Remove an attachment file. Git history still holds the bytes."""
    rel = inp["path"]
    if not rel.startswith(f"{NoteStore.ATTACHMENT_DIR}/"):
        rel = f"{NoteStore.ATTACHMENT_DIR}/{rel}"
    abs_path = state.store.resolve_attachment(rel)
    if not abs_path.is_file():
        raise NoteError(404, f"Attachment not found: {rel}")
    abs_path.unlink()
    state.autocommit.touch()
    return await _record(
        state,
        thread_id,
        "delete_attachment",
        f"Deleted {rel}",
        [rel],
        [],
        {"deleted": rel},
    )


async def read_attachment(state, thread_id, inp) -> dict:
    """Load an image or PDF so it enters the conversation as a real block.
    `_images` is lifted out by the agent loop — the model never sees the key."""
    paths = inp.get("paths") or ([inp["path"]] if inp.get("path") else [])
    if not paths:
        raise NoteError(400, "Provide `path` or `paths`")
    if len(paths) > 4:
        raise NoteError(400, "Look at at most 4 files per call")
    loaded = []
    # PDF page counting and image downscaling are CPU work: off the event loop.
    for path in paths:
        if is_document(path):
            doc = await asyncio.to_thread(load_document, state.store, path)
            loaded.append({**doc, "kind": "document"})
        else:
            image = await asyncio.to_thread(load_image, state.store, path)
            loaded.append({**image, "kind": "image"})
    return {
        "viewing": paths,
        "note": "The file(s) follow this result — describe what you actually see.",
        "_images": loaded,
    }


# --- flashcards ------------------------------------------------------------
# Decks are only discoverable through OMNIA: there is no deck browser in the
# UI, so these tools are the whole entry point.


def _deck_dir(deck_id: str) -> str:
    return f"{NoteStore.INTERNAL_DIR}/flashcards/{deck_id}.json"


def _resolve_deck(state, reference: str) -> dict:
    deck = state.flashcards.find_by_name(reference)
    if deck is None:
        known = ", ".join(d["name"] for d in state.flashcards.summaries()) or "none yet"
        raise NoteError(404, f"No flashcard deck matching '{reference}'. Decks: {known}")
    return deck


async def list_flashcard_decks(state, thread_id, inp) -> dict:
    decks = state.flashcards.summaries()
    if not decks:
        return {"decks": [], "note": "There are no flashcard decks."}
    return {"decks": decks}


async def get_flashcard_deck(state, thread_id, inp) -> dict:
    deck = _resolve_deck(state, inp["deck"])
    return {
        "id": deck["id"],
        "name": deck["name"],
        "stats": flashcard_stats(deck),
        "cards": [
            {"id": c["id"], "front": c["front"], "back": c["back"], "state": card_state(c)}
            for c in deck.get("cards") or []
        ],
    }


async def create_flashcard_deck(state, thread_id, inp) -> dict:
    """Build a deck from explicit cards and/or by parsing `Q :: A` lines out of
    notes. The model is expected to supply cards it wrote itself."""
    cards = [
        new_flashcard(str(c.get("front", "")), str(c.get("back", "")))
        for c in inp.get("cards") or []
        if str(c.get("front", "")).strip() and str(c.get("back", "")).strip()
    ]
    sources = [p for p in inp.get("source_notes") or [] if p]
    for path in sources:
        cards.extend(cards_from_markdown(state.store.read(path)))
    if not cards:
        raise NoteError(
            400,
            "No cards to create. Supply `cards`, or source notes containing "
            "`Question :: Answer` lines.",
        )
    deck = state.flashcards.create(inp["name"], cards, sources)
    return await _record(
        state,
        thread_id,
        "create_flashcard_deck",
        f"Created flashcard deck “{deck['name']}” with {len(cards)} cards",
        [_deck_dir(deck["id"])],
        ["flashcards"],
        {"id": deck["id"], "name": deck["name"], "card_count": len(cards)},
        navigate=f"/study/{deck['id']}",
    )


async def add_flashcards(state, thread_id, inp) -> dict:
    deck = _resolve_deck(state, inp["deck"])
    cards = [
        new_flashcard(str(c.get("front", "")), str(c.get("back", "")))
        for c in inp.get("cards") or []
        if str(c.get("front", "")).strip() and str(c.get("back", "")).strip()
    ]
    if not cards:
        raise NoteError(400, "No cards supplied")
    before = len(deck.get("cards") or [])
    updated = state.flashcards.add_cards(deck["id"], cards)
    added = len(updated["cards"]) - before
    return await _record(
        state,
        thread_id,
        "add_flashcards",
        f"Added {added} card(s) to “{updated['name']}”",
        [_deck_dir(deck["id"])],
        ["flashcards"],
        {"id": deck["id"], "added": added, "total": len(updated["cards"])},
    )


async def rename_flashcard_deck(state, thread_id, inp) -> dict:
    deck = _resolve_deck(state, inp["deck"])
    updated = state.flashcards.rename(deck["id"], inp["name"])
    return await _record(
        state,
        thread_id,
        "rename_flashcard_deck",
        f"Renamed flashcard deck to “{updated['name']}”",
        [_deck_dir(deck["id"])],
        ["flashcards"],
        {"id": deck["id"], "name": updated["name"]},
    )


async def open_flashcard_deck(state, thread_id, inp) -> dict:
    """Put the user into the study screen for a deck."""
    deck = _resolve_deck(state, inp["deck"])
    order = inp.get("order") or "due"
    if order not in ("due", "least_known", "most_known"):
        raise NoteError(400, "order must be due, least_known, or most_known")
    stats = flashcard_stats(deck)
    return await _record(
        state,
        thread_id,
        "open_flashcard_deck",
        f"Opened “{deck['name']}” for study",
        [],
        [],
        {"id": deck["id"], "name": deck["name"], "stats": stats, "order": order},
        undoable=False,
        navigate=f"/study/{deck['id']}?order={order}",
    )


async def delete_flashcard_deck(state, thread_id, inp) -> dict:
    deck = _resolve_deck(state, inp["deck"])
    result = state.flashcards.delete(deck["id"])
    return await _record(
        state,
        thread_id,
        "delete_flashcard_deck",
        f"Deleted flashcard deck “{result['name']}”",
        [_deck_dir(deck["id"])],
        ["flashcards"],
        result,
    )


CARD_ARRAY = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {"front": {"type": "string"}, "back": {"type": "string"}},
        "required": ["front", "back"],
    },
}


EXPANDED_TOOLS: list[tuple[ToolDef, Executor]] = [
    _tool("list_folders", "List every folder, including empty and nested folders.", {}, [], list_folders),
    _tool("create_note", "Immediately create a note after an explicit user request.", {"path": {"type": "string"}, "content": {"type": "string"}, "rationale": RATIONALE}, ["path"], create_note, mode="direct"),
    _tool("edit_note", "Immediately replace a note after an explicit user request. Read it first.", {"path": {"type": "string"}, "new_content": {"type": "string"}, "base_revision": REVISION, "rationale": RATIONALE}, ["path", "new_content", "base_revision"], edit_note, mode="direct"),
    _tool("move_note", "Immediately rename or move a note after an explicit request.", {"path": {"type": "string"}, "new_path": {"type": "string"}, "base_revision": REVISION, "rationale": RATIONALE}, ["path", "new_path", "base_revision"], move_note, mode="direct"),
    _tool("delete_note", "Move a note to recoverable Trash. Requires separate confirmation.", {"path": {"type": "string"}, "base_revision": REVISION, "rationale": RATIONALE}, ["path", "base_revision"], delete_note, mode="confirmed"),
    _tool("edit_frontmatter", "Set or remove custom frontmatter fields without replacing the body.", {"path": {"type": "string"}, "base_revision": REVISION, "set": {"type": "object"}, "remove": {"type": "array", "items": {"type": "string"}}, "rationale": RATIONALE}, ["path", "base_revision"], edit_frontmatter, mode="direct"),
    _tool("propose_note_create", "Stage a proposed note creation in the review tray.", {"path": {"type": "string"}, "content": {"type": "string"}, "rationale": {"type": "string"}}, ["path", "content"], propose_note_create, mode="proposal"),
    _tool("propose_note_edit", "Stage a proposed note replacement in the review tray.", {"path": {"type": "string"}, "new_content": {"type": "string"}, "rationale": {"type": "string"}}, ["path", "new_content"], propose_note_edit, mode="proposal"),
    _tool("create_folder", "Create a folder.", {"path": {"type": "string"}}, ["path"], create_folder, mode="direct"),
    _tool("move_folder", "Move or rename a folder.", {"path": {"type": "string"}, "new_path": {"type": "string"}}, ["path", "new_path"], move_folder, mode="direct"),
    _tool("delete_folder", "Move a folder and its notes to recoverable Trash.", {"path": {"type": "string"}}, ["path"], delete_folder, mode="confirmed"),
    _tool("ensure_daily_note", "Open or create a daily note from the daily template.", {"date": DATE}, [], ensure_daily_note, mode="direct"),
    _tool("create_note_from_template", "Create a uniquely named note from a vault template.", {"path": {"type": "string"}, "template": {"type": "string"}, "date": DATE}, ["path", "template"], create_note_from_template, mode="direct"),
    _tool("set_note_pin", "Pin or unpin a note in the sidebar's Pinned section. At most 3 notes can be pinned — unpin one first if full.", {"path": {"type": "string"}, "pinned": {"type": "boolean"}}, ["path", "pinned"], set_note_pin, mode="direct"),
    _tool("add_wikilink", "Append a validated wikilink while preserving current content.", {"path": {"type": "string"}, "target": {"type": "string"}, "base_revision": REVISION}, ["path", "target", "base_revision"], add_wikilink, mode="direct"),
    _tool("list_tasks", "List tasks filtered by status, text, source note/board/folder, due range, or completion.", {"status": STATUS, "text": {"type": "string"}, "path": TASK_SCOPE, "due_start": DATE, "due_end": DATE, "include_completed": {"type": "boolean"}}, [], list_tasks),
    _tool("get_task_timeline", "Read a task's immutable lifecycle milestones.", {"task_id": {"type": "string"}}, ["task_id"], get_task_timeline),
    _tool("create_task", "Create a portable Markdown task by explicit request.", {"text": {"type": "string"}, "path": {"type": "string", "description": "Note to write the task into. Omit it and the task goes to the task board of the folder the user currently has open, or to Tasks Inbox.md when that folder has none."}, "status": STATUS, "due": DATE, "due_visible": {"type": "boolean"}, "description": {"type": "string"}}, ["text"], create_task, mode="direct"),
    _tool("update_task", "Update a task by stable ID, including its status and due date. An empty due or description clears it.", {"task_id": {"type": "string"}, "text": {"type": "string"}, "status": STATUS, "due": {"type": "string"}, "due_visible": {"type": "boolean"}, "description": {"type": "string"}}, ["task_id"], update_task, mode="direct"),
    _tool("delete_task", "Delete a task while retaining its calendar milestones.", {"task_id": {"type": "string"}}, ["task_id"], delete_task, mode="confirmed"),
    _tool("reorder_tasks", "Pin the order of a task column: pass its task IDs in the exact order they should appear, top to bottom. Reordering one card means sending the whole column's current order with that card moved.", {"task_ids": {"type": "array", "items": {"type": "string"}}}, ["task_ids"], reorder_tasks, mode="direct"),
    _tool("move_task", "Move a task to another note or board by stable ID, keeping its history and lifecycle milestones. The target note must already exist.", {"task_id": {"type": "string"}, "target_path": {"type": "string", "description": "Note or board to move the task into."}}, ["task_id", "target_path"], move_task, mode="direct"),
    _tool("create_task_board", "Give an existing folder its own task board (a note with task_board: true), so its tasks stay off the universal Tasks board. The vault root already has the universal board.", {"folder": {"type": "string", "description": "Existing folder to add the board to."}}, ["folder"], create_task_board, mode="direct"),
    _tool("set_task_reminder", "Set or clear a reminder on a task, so its due date actually alerts the user. Give remind_at as an explicit YYYY-MM-DDTHH:MM, or omit it to remind relative to the task's own due date (days_before, default 0, at at_time, default 09:00). Pass clear: true to remove the reminder.", {"task_id": {"type": "string"}, "remind_at": {"type": "string", "description": "Explicit local reminder time, YYYY-MM-DDTHH:MM. A bare YYYY-MM-DD fires at at_time (default 09:00)."}, "days_before": {"type": "integer", "description": "Days before the task's due date to remind. Needs the task to have a due date."}, "at_time": {"type": "string", "description": "Time of day HH:MM for a due-relative reminder. Default 09:00."}, "clear": {"type": "boolean"}}, ["task_id"], set_task_reminder, mode="direct"),
    _tool("bulk_edit_tasks", "Apply one change to every task matching a filter, in a single undoable step — e.g. push all of this week's open tasks to Monday, or clear a column. Filter by status/text/path/due range; then exactly one of set_status, set_due (empty clears), move_to (target note), or delete. Requires confirmation.", {"status": STATUS, "text": {"type": "string"}, "path": TASK_SCOPE, "due_start": DATE, "due_end": DATE, "include_completed": {"type": "boolean"}, "set_status": STATUS, "set_due": {"type": "string", "description": "New due date, or empty string to clear it."}, "move_to": {"type": "string", "description": "Note or board to move the matching tasks into."}, "delete": {"type": "boolean"}}, [], bulk_edit_tasks, mode="confirmed"),
    _tool("archive_completed_tasks", "Sweep a board's completed tasks into a dated archive note, keeping their completion record. Keeps a long-running board readable.", {"path": {"type": "string", "description": "Board (its note path or folder name) or note to archive completed tasks from."}, "archive_path": {"type": "string", "description": "Where to file them. Defaults to archive/<name> Done.md."}}, ["path"], archive_completed_tasks, mode="direct"),
    _tool("roll_over_tasks", "Carry overdue open tasks forward: set the due date of every unfinished, past-due task to today (or a given date). Scope to one board with path.", {"to": DATE, "path": TASK_SCOPE}, [], roll_over_tasks, mode="direct"),
    _tool("scaffold_project", "Stand up a whole project workspace in one step: a folder, its own task board, and seeded notes (Brief, Log, Resources by default). Use for 'start a project for X'.", {"name": {"type": "string"}, "parent": {"type": "string", "description": "Optional parent folder to nest the project under."}, "seed_notes": {"type": "array", "items": {"type": "string"}, "description": "Seed note titles. Defaults to Brief, Log, Resources."}}, ["name"], scaffold_project, mode="direct"),
    _tool("create_recurring_task", "Create a recurring task (a chore that reappears). It carries a recurrence and a first due date; when the user completes it, the next occurrence is created automatically with the next due date. Use for 'water the plants every 3 days', 'pay rent monthly'.", {"text": {"type": "string"}, "due": {"type": "string", "description": "First due date, YYYY-MM-DD."}, "recurrence": RECURRENCE, "path": {"type": "string"}, "due_visible": {"type": "boolean"}, "description": {"type": "string"}}, ["text", "due", "recurrence"], create_recurring_task, mode="direct"),
    _tool("board_digest", "A read-only rollup of one task board: counts by status, what's overdue, due today, and next up. Omit path to use the board of the folder the user has open. Answers 'where is this project at?'.", {"path": {"type": "string", "description": "Board (its note path or folder name) or note to summarize. Defaults to the current folder's board."}}, [], board_digest),
    _tool("generate_daily_briefing", "Compile the day's briefing — overdue and due-today tasks, today's events, event reminders, and open counts per board — as structured data to turn into a short brief. Read-only.", {"days": {"type": "integer", "description": "How many days of horizon to include. Default 1 (today); use 7 for a week-ahead brief."}}, [], generate_daily_briefing),
    _tool("notify_user", "Send the user a notification (title + optional body). This is how a scheduled or proactive run reaches them: it shows as an in-app toast and, when the user enabled notifications, is delivered as a Web Push to their devices. At most 2 new messages per scheduled run (or conversation) in 10 minutes and 10 a day; a refused send returns a note, not an error. Use it to deliver a briefing summary or flag something that needs attention.", {"title": {"type": "string"}, "body": {"type": "string"}}, ["title"], notify_user),
    _tool("schedule_agent_run", "Schedule OMNIA to run an instruction on a recurring basis — e.g. 'every weekday at 8am, summarize my overdue tasks and notify me'. Scheduled runs are proactive: read, compose, and notify only, never destructive actions. frequency is daily or weekly; at is HH:MM (24h); for weekly give weekday 0=Monday..6=Sunday; interval repeats every N days/weeks. Requires confirmation.", {"instruction": {"type": "string"}, "frequency": {"type": "string", "enum": ["daily", "weekly"]}, "at": {"type": "string", "description": "Time of day HH:MM (24-hour). Default 09:00."}, "interval": {"type": "integer", "description": "Every N days/weeks. Default 1."}, "weekday": {"type": "integer", "description": "For weekly runs: 0=Monday .. 6=Sunday."}}, ["instruction", "frequency"], schedule_agent_run, mode="confirmed"),
    _tool("manage_scheduled_runs", "List, pause, resume, or cancel OMNIA's scheduled runs. operation=list shows them with their ids and next run time; then pause/resume/cancel by schedule_id.", {"operation": {"type": "string", "enum": ["list", "pause", "resume", "cancel"]}, "schedule_id": {"type": "string"}}, [], manage_scheduled_runs, mode="direct"),
    _tool("get_calendar_range", "Read daily-note, note, task, activity, due-date, and event data for up to one year.", {"start": DATE, "end": DATE}, ["start", "end"], get_calendar_range),
    _tool("get_activity_summary", "Read activity entries and day/week/month totals.", {"date": DATE}, [], get_activity_summary),
    _tool("add_activity", "Log activity hours against a date.", {"date": DATE, "activity": {"type": "string"}, "hours": {"type": "number"}}, ["date", "activity", "hours"], add_activity, mode="direct"),
    _tool("update_activity", "Update an activity using optimistic concurrency.", {"entry_id": {"type": "string"}, "expected_updated_at": {"type": "string"}, "activity": {"type": "string"}, "hours": {"type": "number"}}, ["entry_id", "expected_updated_at"], update_activity, mode="direct"),
    _tool("delete_activity", "Delete an activity entry after confirmation.", {"entry_id": {"type": "string"}, "expected_updated_at": {"type": "string"}}, ["entry_id", "expected_updated_at"], delete_activity, mode="confirmed"),
    _tool("get_note_history", "List a note's Git-backed versions.", {"path": {"type": "string"}, "limit": {"type": "integer"}}, ["path"], get_note_history),
    _tool("inspect_note_version", "Read a historical note version or diff it against current.", {"path": {"type": "string"}, "sha": {"type": "string"}, "version_path": {"type": "string"}, "format": {"type": "string", "enum": ["content", "diff"]}}, ["path", "sha"], inspect_note_version),
    _tool("restore_note_version", "Restore a version as a copy directly; replacing current content requires confirmation.", {"path": {"type": "string"}, "sha": {"type": "string"}, "version_path": {"type": "string"}, "mode": {"type": "string", "enum": ["copy", "replace"]}, "target_path": {"type": "string"}, "base_revision": REVISION}, ["path", "sha", "mode"], restore_note_version, mode="direct"),
    _tool("list_trash", "List recoverable Trash items.", {}, [], list_trash),
    _tool("inspect_trash", "Preview a recoverable Trash item.", {"item_id": {"type": "string"}}, ["item_id"], inspect_trash),
    _tool("restore_trash", "Restore a Trash item to its original or chosen free path.", {"item_id": {"type": "string"}, "target_path": {"type": "string"}}, ["item_id"], restore_trash, mode="direct"),
    _tool("get_appearance", "Read theme, font, Intoxicate, custom palette, and saved themes.", {}, [], get_appearance),
    _tool("set_appearance", "Partially change safe appearance settings.", {"theme": {"type": "string"}, "font": {"type": "string"}, "intoxicate": {"type": "boolean"}, "custom_theme": PALETTE}, [], set_appearance, mode="direct"),
    _tool("manage_saved_theme", "Create, rename, apply, update, or delete a saved theme.", {"operation": {"type": "string", "enum": ["create", "rename", "apply", "update", "delete"]}, "theme_id": {"type": "string"}, "name": {"type": "string"}, "theme": PALETTE}, ["operation"], manage_saved_theme, mode="direct"),
    _tool("get_timezone", "Read the application IANA timezone.", {}, [], get_timezone),
    _tool("set_timezone", "Set the application IANA timezone.", {"timezone": {"type": "string"}}, ["timezone"], set_timezone, mode="direct"),
    _tool("get_backup_status", "Read redacted backup status.", {}, [], get_backup_status),
    _tool("push_backup", "Manually push the configured backup. This cannot be undone.", {}, [], push_backup, mode="direct"),
    _tool("rebuild_search_index", "Rebuild keyword and semantic indexes. This cannot be undone.", {}, [], rebuild_search_index, mode="direct"),
    _tool("get_security_overview", "Read sanitized devices, approximate locations, and failed login attempts.", {}, [], get_security_overview),
    _tool("list_events", "List expanded standalone event occurrences in a date range.", {"start": DATE, "end": DATE}, ["start", "end"], list_events),
    _tool("create_event", "Create a standalone or recurring calendar event. A start with a time and no end lasts one hour; a bare YYYY-MM-DD start is all-day unless all_day is false.", {"title": {"type": "string"}, "start": {"type": "string"}, "end": {"type": "string"}, "all_day": {"type": "boolean"}, "timezone": EVENT_TIMEZONE, "notes": {"type": "string"}, "note_path": {"type": "string"}, "recurrence": RECURRENCE, "reminders": {"type": "array", "items": {"type": "integer"}}}, ["title", "start"], create_event, mode="direct"),
    _tool("update_event", "Update an event using its revision. Empty notes/note_path and an empty recurrence object clear those fields. Changing only start keeps the duration; a bare YYYY-MM-DD start keeps the time of day.", {"event_id": {"type": "string"}, "expected_revision": REVISION, "title": {"type": "string"}, "start": {"type": "string"}, "end": {"type": "string"}, "all_day": {"type": "boolean"}, "timezone": EVENT_TIMEZONE, "notes": {"type": "string"}, "note_path": {"type": "string"}, "recurrence": {"type": "object"}, "reminders": {"type": "array", "items": {"type": "integer"}}}, ["event_id", "expected_revision"], update_event, mode="direct"),
    _tool("delete_event", "Delete a standalone event after separate confirmation.", {"event_id": {"type": "string"}, "expected_revision": REVISION}, ["event_id", "expected_revision"], delete_event, mode="confirmed"),
    _tool("create_reminder", "Set a standalone reminder that alerts the user at a chosen time — a plain nudge not tied to an existing task or event ('remind me to call the dentist at 3pm'). It is stored as a calendar event carrying a reminder, so it also shows on the calendar and can be updated or deleted with the event tools.", {"text": {"type": "string"}, "when": {"type": "string", "description": "YYYY-MM-DDTHH:MM, or YYYY-MM-DD for 09:00 that day."}, "reminders": {"type": "array", "items": {"type": "integer"}, "description": "Optional minutes-before offsets. Defaults to [0]: alert at the reminder's own time."}, "timezone": EVENT_TIMEZONE, "note_path": {"type": "string", "description": "Optional note to link the reminder to."}, "recurrence": RECURRENCE}, ["text", "when"], create_reminder, mode="direct"),
    _tool("capture", "FILE something the user just told you, in one step. Use this whenever they state a plan, a commitment, an appointment, or something to do — 'tomorrow I'm going to the library', 'I need to email the professor', 'exam on the 12th'. kind='event' for something happening at a time or on a day, kind='task' for something to do, kind='note' only when it is neither. It creates the task or calendar event AND writes a line into that day's daily note, so the written record and the structured record agree. Prefer this over ensure_daily_note + patch_note.", {"text": {"type": "string"}, "kind": {"type": "string", "enum": ["task", "event", "note"]}, "when": {"type": "string"}, "reminders": {"type": "array", "items": {"type": "integer"}}}, ["text", "kind"], capture, mode="direct"),
    _tool("search_conversations", "Search what was said in past conversations with the user. Use this when they refer to something you discussed before — 'what did we decide', 'like we talked about', 'you suggested'. Your own memory of earlier threads is not otherwise available.", {"query": {"type": "string"}, "limit": {"type": "integer"}}, ["query"], search_conversations),
    _tool("read_conversation", "Read a past conversation's transcript, by thread_id from search_conversations or list_conversations.", {"thread_id": {"type": "string"}, "limit": {"type": "integer"}}, ["thread_id"], read_conversation),
    _tool("list_conversations", "List recent conversation threads with titles and dates.", {"limit": {"type": "integer"}}, [], list_conversations),
    _tool("remember", "Record ONE durable fact about the user in your memory note — who they are, what they are working on, a preference, a convention they follow. Call this the moment something durable comes up; it files the entry under the right heading and dates it for you, so there is no need to rewrite the note. Not for trivia, not for anything the vault already says.", {"text": {"type": "string"}, "category": {"type": "string", "enum": sorted(MEMORY_CATEGORIES), "description": "Which section to file it under: about (who they are), preference (how they like things done), project (ongoing work), convention (how they keep the vault). Omit it and the entry goes to your own notes-to-self section."}}, ["text"], remember, mode="direct"),
    _tool("forget", "Remove memory entries matching a phrase, when something you recorded turns out wrong or stale. Quote the fact itself; the date each entry carries is ignored when matching.", {"text": {"type": "string"}}, ["text"], forget, mode="direct"),
    _tool("note_observation", "Stage ONE thing you noticed about the user for them to confirm or dismiss — the only way a background pass may record anything. kind='habit' for a testable pattern (give habit_key like 'daily_note.logs_hours' and habit_value); kind='memory' for durable prose. It becomes a real habit or memory entry only if the user confirms it. At most one is staged per day and a dismissed habit key is not raised again for months, so spend it on something that would actually change how you behave.", {"text": {"type": "string", "description": "One sentence, as the user will read it."}, "kind": {"type": "string", "enum": list(observations.KINDS)}, "habit_key": {"type": "string"}, "habit_value": {"type": "string"}}, ["text", "kind"], note_observation, mode="proposal"),
    _tool("rename_tag", "Rename an inline #tag everywhere it appears, in one operation. This is how you fix duplicate tags found by analyze_tags — never rewrite notes one by one for a tag merge. Set dry_run to preview which notes would change. Requires confirmation.", {"from_tag": {"type": "string"}, "to_tag": {"type": "string"}, "dry_run": {"type": "boolean"}}, ["from_tag", "to_tag"], rename_tag, mode="confirmed"),
    _tool("propose_edits", "Stage spelling, grammar and clarity fixes for a note so the user can accept or dismiss each one in the app's Review panel. THIS is how you answer \"check my spelling/grammar\" — never rewrite the note with edit_note or patch_note for proofreading. Quote `before` exactly as it appears, keeping it short but unique; if the same text occurs more than once, set `occurrence` (0-based). `after` is the replacement, or empty to delete.", {"path": {"type": "string"}, "suggestions": {"type": "array", "items": {"type": "object", "properties": {"category": {"type": "string", "enum": list(CATEGORIES)}, "before": {"type": "string", "description": "Exact text from the note to replace"}, "after": {"type": "string", "description": "Replacement text; empty string deletes"}, "reason": {"type": "string", "description": "One short phrase, e.g. 'subject-verb agreement'"}, "occurrence": {"type": "integer", "description": "0-based, only when `before` is not unique"}}, "required": ["category", "before", "after"]}}}, ["path", "suggestions"], propose_edits, mode="direct"),
    _tool("patch_note", "Make targeted find/replace edits to a note without rewriting the whole file. PREFER THIS over edit_note for small changes and for any long note — edit_note must re-emit the entire file, which does not fit for large notes. Each old_string must appear exactly once unless replace_all is set.", {"path": {"type": "string"}, "base_revision": REVISION, "edits": {"type": "array", "items": {"type": "object", "properties": {"old_string": {"type": "string"}, "new_string": {"type": "string"}, "replace_all": {"type": "boolean"}}, "required": ["old_string", "new_string"]}}}, ["path", "base_revision", "edits"], patch_note, mode="direct"),
    _tool("find_unlinked_mentions", "Find places where one note's title is written in another note's prose without a [[wikilink]]. Use this to connect an unlinked vault; pass note_path to check a single note.", {"note_path": {"type": "string"}, "limit": {"type": "integer"}}, [], find_unlinked_mentions),
    _tool("ask_vault", "Answer a question from the notebook: retrieve the most relevant notes (semantic + keyword, rank-fused) so you can answer and cite their paths. Use this for 'what did I conclude about X', 'what do my notes say about Y'.", {"query": {"type": "string"}, "k": {"type": "integer", "description": "How many source notes to retrieve. Default 6."}, "path_prefix": {"type": "string", "description": "Only notes under this folder prefix, e.g. 'School/'"}, "exclude_prefix": {"type": "string", "description": "Skip notes under this folder prefix, e.g. 'media/'"}}, ["query"], ask_vault),
    _tool("summarize_timespan", "Read what happened between two dates — notes created and touched, tasks completed, and events — for a weekly or monthly review. Read-only.", {"start": DATE, "end": DATE}, ["start", "end"], summarize_timespan),
    _tool("set_note_followup", "Resurface a note later: set a reminder linked to it ('remind me about this in two weeks'). when is a date/date-time, or use in_days.", {"path": {"type": "string"}, "when": {"type": "string", "description": "YYYY-MM-DDTHH:MM, or YYYY-MM-DD for 09:00 that day."}, "in_days": {"type": "integer", "description": "Days from today, if `when` is omitted; fires at 09:00."}}, ["path"], set_note_followup, mode="direct"),
    _tool("flashcards_from_note", "Turn a note into a spaced-repetition deck. Supply `cards` (Q/A you wrote from reading the note); any `Question :: Answer` lines already in the note are also included. The deck is named after the note unless deck_name is given.", {"path": {"type": "string"}, "cards": CARD_ARRAY, "deck_name": {"type": "string"}}, ["path"], flashcards_from_note, mode="direct"),
    _tool("bulk_tag", "Add or remove a frontmatter tag across many notes at once — every note under a folder, or every note that already has a given tag. Requires confirmation.", {"tag": {"type": "string"}, "operation": {"type": "string", "enum": ["add", "remove"]}, "folder": {"type": "string", "description": "Tag every note under this folder."}, "has_tag": {"type": "string", "description": "Tag every note that already has this tag."}}, ["tag", "operation"], bulk_tag, mode="confirmed"),
    _tool("resolve_unlinked_mentions", "Turn unlinked title mentions into real [[wikilinks]] across the vault (the ones find_unlinked_mentions reports). Pass note_path to limit to one note.", {"note_path": {"type": "string"}, "limit": {"type": "integer"}}, [], resolve_unlinked_mentions, mode="direct"),
    _tool("find_replace_across_vault", "Preview-then-apply a plain text find/replace across every note — 'rename this project everywhere', fix a recurring typo. Set dry_run to preview which notes would change. Requires confirmation.", {"find": {"type": "string"}, "replace": {"type": "string", "description": "Replacement text; empty string deletes the found text."}, "case_sensitive": {"type": "boolean"}, "dry_run": {"type": "boolean"}}, ["find", "replace"], find_replace_across_vault, mode="confirmed"),
    _tool("split_note", "Split a long note into one linked note per `## ` section, leaving the original holding a list of links. Read the note first for its base_revision.", {"path": {"type": "string"}, "base_revision": REVISION}, ["path", "base_revision"], split_note, mode="direct"),
    _tool("merge_notes", "Combine two or more notes into one target note, rewrite inbound [[wikilinks]] to point at the survivor, and move the merged-away originals to recoverable Trash. Requires confirmation.", {"sources": {"type": "array", "items": {"type": "string"}}, "target": {"type": "string", "description": "The note to merge into (created if it doesn't exist)."}}, ["sources", "target"], merge_notes, mode="confirmed"),
    _tool("export_notes", "Bundle selected notes into one markdown note under exports/ that the user can open or copy. Stays inside the vault.", {"paths": {"type": "array", "items": {"type": "string"}}, "name": {"type": "string", "description": "Export file name (without extension)."}}, ["paths"], export_notes, mode="direct"),
    _tool("list_templates", "List the available note templates by name, for create_note_from_template.", {}, [], list_templates),
    _tool("append_to_note", "Add text to a note without rewriting it: at the end (default), under a named ## heading, or at the top (position='top'). Prefer this over patch_note for 'add a line' / 'jot this under ## Ideas'.", {"path": {"type": "string"}, "text": {"type": "string"}, "base_revision": REVISION, "heading": {"type": "string", "description": "Insert under this ## heading (created if absent)."}, "position": {"type": "string", "enum": ["end", "top"], "description": "Where to add when no heading is given. Default end."}}, ["path", "text", "base_revision"], append_to_note, mode="direct"),
    _tool("duplicate_note", "Duplicate a note to a new path (fresh task IDs). Omit target_path to make a '… copy'. Use this instead of re-typing a note's content.", {"path": {"type": "string"}, "target_path": {"type": "string"}}, ["path"], duplicate_note, mode="direct"),
    _tool("rename_note", "Rename a note by giving it a new title — recomputes the filename in the same folder and rewrites inbound [[wikilinks]]. For daily/media/template notes, edit the title directly instead.", {"path": {"type": "string"}, "new_title": {"type": "string"}, "base_revision": REVISION}, ["path", "new_title", "base_revision"], rename_note, mode="direct"),
    _tool("get_outline", "Read a note's heading map, word count, length, and revision — to navigate a long note or jump to a section without pulling the whole body.", {"path": {"type": "string"}}, ["path"], get_outline),
    _tool("move_section", "Move a ## section (with its subsections) out of one note and into another. Read the source first for its base_revision.", {"path": {"type": "string"}, "heading": {"type": "string"}, "target_path": {"type": "string"}, "base_revision": REVISION}, ["path", "heading", "target_path", "base_revision"], move_section, mode="direct"),
    _tool("convert_bullets_to_tasks", "Turn plain '- item' bullets into '- [ ] item' checkboxes, optionally scoped to one ## heading section. Skips lines that are already tasks.", {"path": {"type": "string"}, "heading": {"type": "string", "description": "Limit to this section; omit to convert bullets in the whole note."}, "base_revision": REVISION}, ["path", "base_revision"], convert_bullets_to_tasks, mode="direct"),
    _tool("save_note_as_template", "Save an existing note as a reusable template (under templates/), stripping managed frontmatter. Then create_note_from_template can instantiate it.", {"path": {"type": "string"}, "name": {"type": "string", "description": "Template name. Defaults to the note's name."}}, ["path"], save_note_as_template, mode="direct"),
    _tool("complete_task", "Check off (or reopen) a task by describing it — 'check off the milk task' — instead of needing its id. Matches the best task by text (exact beats partial, open beats done); pass path to narrow to one board. Without done it toggles; pass done=true to only check off or done=false to only reopen.", {"text": {"type": "string"}, "path": {"type": "string"}, "done": {"type": "boolean", "description": "true checks the task off, false reopens it; an already-matching task is left alone."}}, ["text"], complete_task, mode="direct"),
    _tool("reschedule_task", "Move a task's due date, addressing the task by its text — 'push the milk task to tomorrow'. Resolve the date to YYYY-MM-DD yourself.", {"text": {"type": "string"}, "due": DATE, "path": {"type": "string"}}, ["text", "due"], reschedule_task, mode="direct"),
    _tool("add_tasks", "Create several tasks at once (one step, one commit) — capturing a list of todos. Omit path to use the current folder's board or the inbox.", {"tasks": {"type": "array", "items": {"type": "object", "properties": {"text": {"type": "string"}, "due": DATE}, "required": ["text"]}}, "path": {"type": "string"}}, ["tasks"], add_tasks, mode="direct"),
    _tool("reorder_sidebar", "Arrange notes or folders in the sidebar. Pass the items you want at the top, in order; they splice to the front and everything else keeps its place. kind is 'notes' (note paths) or 'folders' (folder paths).", {"kind": {"type": "string", "enum": ["notes", "folders"]}, "order": {"type": "array", "items": {"type": "string"}}}, ["kind", "order"], reorder_sidebar, mode="direct"),
    _tool("archive_note", "Move a note into archive/ in one step, out of the way but recoverable.", {"path": {"type": "string"}}, ["path"], archive_note, mode="direct"),
    _tool("move_notes", "Move several notes into a folder in one step. Use an empty folder for the vault root.", {"paths": {"type": "array", "items": {"type": "string"}}, "folder": {"type": "string"}}, ["paths", "folder"], move_notes, mode="direct"),
    _tool("reschedule_event", "Move a calendar event or reminder to a new time by naming it — 'push my dentist appointment to Friday'. Give the new start (YYYY-MM-DD keeps the time of day, or YYYY-MM-DDTHH:MM); duration is kept unless you give an end. Matches the best/nearest event by title.", {"title": {"type": "string"}, "start": {"type": "string"}, "end": {"type": "string"}}, ["title", "start"], reschedule_event, mode="direct"),
    _tool("edit_media_profile", "Set the media hub's name, location, and/or favorites (up to 4 tracked media items, by note path). Avatar/banner images are changed in the app, not here.", {"name": {"type": "string"}, "location": {"type": "string"}, "favorites": {"type": "array", "items": {"type": "string"}, "description": "Note paths of tracked media items to feature."}}, [], edit_media_profile, mode="direct"),
    _tool("bulk_log_media", "Track several media items at once — 'log these five books'. Each item needs a title and media_type (book/movie/tv/game); optional status (planned/active/finished/dropped), creator, year, total, rating. Logged bare; add covers later with set_media_art. Already-tracked titles are skipped. At most 50 per call.", {"items": {"type": "array", "maxItems": MAX_BULK_MEDIA, "items": {"type": "object", "properties": {"title": {"type": "string"}, "media_type": {"type": "string", "enum": ["book", "movie", "tv", "game"]}, "status": {"type": "string"}, "creator": {"type": "string"}, "year": {"type": "integer"}, "total": {"type": "integer"}, "rating": {"type": "integer"}}, "required": ["title", "media_type"]}}}, ["items"], bulk_log_media, mode="direct"),
    _tool("link_notes", "Create a two-way [[wikilink]] between two notes, so each references the other. Use for 'connect these two notes'.", {"path_a": {"type": "string"}, "path_b": {"type": "string"}}, ["path_a", "path_b"], link_notes, mode="direct"),
    _tool("set_omnia_housekeeping", "Turn OMNIA's nightly conversation-list housekeeping on or off. (Full-auto mode is Settings-only and cannot be changed here.)", {"enabled": {"type": "boolean"}}, ["enabled"], set_omnia_housekeeping, mode="direct"),
    _tool("edit_selection", "Replace the text the user has selected in the editor with new_string — this is how you act on 'rewrite this' / 'fix this'. With no selection it inserts at the cursor. Acts on the exact live selection; if the selection is stale or came from outside the editor, pass old_string (the exact text to replace) as a fallback.", {"new_string": {"type": "string"}, "old_string": {"type": "string", "description": "Fallback: the exact text to replace when there's no live selection."}, "path": {"type": "string", "description": "Fallback note path for old_string."}}, ["new_string"], edit_selection, mode="direct"),
    _tool("format_selection", "Format the user's current selection: style = heading | quote | callout | bold | italic | code | bullet | task (for 'make this a heading', 'quote this', 'turn these lines into a checklist'). Give level 1-6 for heading. Falls back to old_string when the selection is stale.", {"style": {"type": "string", "enum": ["heading", "quote", "callout", "bold", "italic", "code", "bullet", "task"]}, "level": {"type": "integer", "description": "Heading level 1-6 (default 2)."}, "old_string": {"type": "string"}, "path": {"type": "string"}}, ["style"], format_selection, mode="direct"),
    _tool("extract_to_note", "Cut the user's selection out into a new note and leave a [[wikilink]] in its place — 'pull this into its own note'. Title defaults to the selection's first line. For whole ## sections prefer move_section.", {"title": {"type": "string"}, "old_string": {"type": "string"}, "path": {"type": "string"}}, [], extract_to_note, mode="direct"),
    _tool("open", "Open something in the app for the user — 'pull up my groceries note', 'go to my calendar', 'open the School board', 'show me my thermodynamics notebook', 'search for entropy'. Give ONE target: `note` (a path or name), `view`, `folder` (that folder's task board), `paper` (a notebook name or path, or a page path from search_handwriting / list_notebooks), `query` (the search page with those results) or `tag` (the notes carrying it). view=history with a `note` shows that note's versions; alone it shows what you have done. Flashcard decks open with the flashcards domain. Navigation only; changes nothing.", {"note": {"type": "string", "description": "Note path or name to open."}, "view": {"type": "string", "enum": list(_APP_VIEWS)}, "folder": {"type": "string", "description": "Folder whose task board to open, e.g. 'School'."}, "paper": {"type": "string", "description": "Paper notebook title or path, or a page path like paper/Work/Ideas.paper/pages/<id>."}, "query": {"type": "string", "description": "Search text to open the search page with."}, "tag": {"type": "string", "description": "Tag without the #."}}, [], open_in_app, mode="direct"),
    _tool("close_tabs", "Close open editor tabs — pass `match` to close tabs whose name contains it, or `others: true` to close every tab except the current one.", {"match": {"type": "string"}, "others": {"type": "boolean"}}, [], close_tabs, mode="direct"),
    _tool("related_notes", "Find the notes most similar to a given note — 'what else did I write about this?'. Semantic neighbours, not explicit links.", {"path": {"type": "string"}, "k": {"type": "integer", "description": "How many to return. Default 8."}}, ["path"], related_notes),
    _tool("get_forward_links", "List the notes a given note links OUT to (the complement of get_backlinks), flagging any whose target doesn't exist.", {"path": {"type": "string"}}, ["path"], get_forward_links),
    _tool("find_notes", "Vault hygiene sweep. filter = orphan (nothing links to or from it) | stale (modified before a date — pass `before`) | empty | large. For 'find my orphan notes', 'notes I haven't touched since June'.", {"filter": {"type": "string", "enum": ["orphan", "stale", "empty", "large"]}, "before": DATE, "min_chars": {"type": "integer"}, "limit": {"type": "integer"}}, ["filter"], find_notes),
    _tool("find_broken_links", "Find [[wikilinks]] that point to notes which don't exist — dead links to fix.", {"limit": {"type": "integer"}}, [], find_broken_links),
    _tool("vault_overview", "A quick snapshot of the whole vault: how many notes, open tasks, tags, task boards, media by status, and notes per folder. The 'what've I got?' read.", {}, [], vault_overview),
    _tool("summarize_notes", "Gather the contents of a folder or a set of notes (capped) so you can summarize them and cite the paths. For a single note, use read_notes.", {"folder": {"type": "string"}, "paths": {"type": "array", "items": {"type": "string"}}}, [], summarize_notes),
    _tool("note_changes", "Show what changed in a note since a date — a diff against how it looked then. For 'what did I edit in this note this week?'.", {"path": {"type": "string"}, "since": DATE}, ["path"], note_changes),
    _tool("restore_note_to", "Restore a note to how it was at or before a date — 'put this back to how it was Monday'. Replaces the whole note: read it first. Requires confirmation. Undoable.", {"path": {"type": "string"}, "date": DATE, "base_revision": REVISION}, ["path", "date", "base_revision"], restore_note_to, mode="direct"),
    _tool("find_free_time", "Find open time slots between calendar events in a date range — 'am I free Thursday afternoon', 'find a 1h slot this week'. Honours working hours (day_start_hour/day_end_hour, default 9-18) and a minimum slot length (min_minutes, default 30).", {"start": DATE, "end": DATE, "day_start_hour": {"type": "integer"}, "day_end_hour": {"type": "integer"}, "min_minutes": {"type": "integer"}}, ["start", "end"], find_free_time),
    _tool("week_agenda", "Lay out events and due tasks day by day across a date range — 'what's my week look like'.", {"start": DATE, "end": DATE}, ["start", "end"], week_agenda),
    _tool("list_media", "List tracked media by status — 'what am I currently watching' (status=active), 'my watchlist' (status=watchlist), 'what have I finished'. Optional media_type book/movie/tv/game.", {"status": {"type": "string", "description": "active | planned/watchlist | finished | dropped"}, "media_type": {"type": "string", "enum": ["book", "movie", "tv", "game"]}}, [], list_media),
    _tool("media_diary", "Recent media activity — what the user has been watching/reading/playing lately, newest first.", {"limit": {"type": "integer"}}, [], media_diary),
    _tool("update_media_by_title", "Update a tracked media item by its TITLE (no path needed) — set status (active/planned/watchlist/finished/dropped), log progress ('S01E05' or a page number), or set a rating (1-10). For 'I'm on episode 5 of X', 'add X to my watchlist', 'mark X finished'.", {"title": {"type": "string"}, "status": {"type": "string"}, "progress": {"type": "string", "description": "A page number or SxxEyy to log."}, "rating": {"type": "integer"}}, ["title"], update_media_by_title, mode="direct"),
    _tool("format_table", "Align, sort, or add a row to a markdown table in a note (scoped to a heading if given). operation = align (tidy columns) | sort (by column index) | add_row (give `row`, an array of cell values).", {"path": {"type": "string"}, "base_revision": REVISION, "operation": {"type": "string", "enum": ["align", "sort", "add_row"]}, "column": {"type": "integer"}, "row": {"type": "array", "items": {"type": "string"}}, "heading": {"type": "string"}}, ["path", "base_revision"], format_table, mode="direct"),
    _tool("sort_list", "Sort a bullet list in a note alphabetically (scoped to a heading if given). Set dedupe to drop duplicate lines, descending to reverse.", {"path": {"type": "string"}, "base_revision": REVISION, "heading": {"type": "string"}, "dedupe": {"type": "boolean"}, "descending": {"type": "boolean"}}, ["path", "base_revision"], sort_list, mode="direct"),
    _tool("insert_toc", "Insert a table of contents (built from the note's ## headings) near the top of a note.", {"path": {"type": "string"}, "base_revision": REVISION}, ["path", "base_revision"], insert_toc, mode="direct"),
    _tool("embed_attachment", "Insert an image/attachment reference into a note — resolve the attachment by name, then embed it (at the end, or under a heading).", {"path": {"type": "string"}, "attachment": {"type": "string", "description": "Attachment file name (or part of it)."}, "alt": {"type": "string"}, "heading": {"type": "string"}}, ["path", "attachment"], embed_attachment, mode="direct"),
    _tool("extract_action_items", "Pull the checkbox (and optionally bullet) action items out of a note (or one ## section) onto a different task board — 'turn the todos in this note into tasks on my board'. Existing tasks move with their id, reminder and history; bare checkboxes become tasks in the same state. By default the lines are removed from the note (remove=false to keep them and put copies on the board). To make tasks inside the same note, use convert_bullets_to_tasks.", {"path": {"type": "string"}, "board": {"type": "string", "description": "Destination board note path; must be a different note."}, "base_revision": REVISION, "heading": {"type": "string"}, "remove": {"type": "boolean"}, "include_done": {"type": "boolean", "description": "Also take checked-off items; by default they stay in the note."}, "include_bullets": {"type": "boolean", "description": "Also turn plain '- item' bullets into tasks. Defaults to true only when the scope has no checkboxes."}}, ["path", "board", "base_revision"], extract_action_items, mode="direct"),
    _tool("study_stats", "Cross-deck flashcard stats: total cards, how many are due, review count, retention, and a per-deck breakdown. 'How's my studying going?'.", {}, [], study_stats),
    _tool("list_pending", "List what you've staged in the review tray (propose_* changes awaiting the user). Check this before proposing, so you don't re-stage something already waiting, and to tell the user what's pending.", {}, [], list_pending),
    _tool("list_recent_actions", "List your own recent actions across conversations (what you changed, when, and whether each is still undoable). Use for 'what did you change earlier?'.", {"limit": {"type": "integer", "description": "How many to return, newest first. Default 20."}}, [], list_recent_actions),
    _tool("undo_last_action", "Undo the most recent undoable change you made in THIS conversation — 'undo that', 'revert the last change'. Pass action_id (from list_recent_actions) to undo a specific one. Only actions from this thread can be undone.", {"action_id": {"type": "string"}}, [], undo_last_action, mode="direct"),
    _tool("list_queued_reminders", "Peek at reminders/alerts queued for the user and any confirmation cards still awaiting them, without consuming them. Use for 'do I have anything pending?'.", {}, [], list_queued_reminders),
    _tool("propose_note_delete", "Suggest deleting a note, staged in the review tray with a diff rather than executed. Use when recommending a cleanup the user should look over; delete_note is for an explicit instruction to delete now.", {"path": {"type": "string"}, "rationale": {"type": "string"}}, ["path"], propose_note_delete, mode="proposal"),
    _tool("propose_note_move", "Suggest moving or renaming a note, staged in the review tray. Use when recommending a reorganization; move_note is for an explicit instruction.", {"path": {"type": "string"}, "new_path": {"type": "string"}, "rationale": {"type": "string"}}, ["path", "new_path"], propose_note_move, mode="proposal"),
    _tool("review_flashcard", "Grade a flashcard after the user answers it — again, hard, good or easy — so the spaced-repetition schedule advances. Identify the card by card_id, or by its exact front text.", {"deck": {"type": "string"}, "card_id": {"type": "string"}, "front": {"type": "string"}, "grade": {"type": "string", "enum": ["again", "hard", "good", "easy"]}}, ["deck", "grade"], review_flashcard, mode="direct"),
    _tool("remove_flashcards", "Remove specific cards from a deck by card_ids or by their exact front text — for a card that is wrong, duplicated or no longer useful.", {"deck": {"type": "string"}, "card_ids": {"type": "array", "items": {"type": "string"}}, "fronts": {"type": "array", "items": {"type": "string"}}}, ["deck"], remove_flashcards, mode="confirmed"),
    _tool("list_attachments", "List image/PDF attachments — those referenced by one note when note_path is given, otherwise the whole vault with a `referenced` flag per file (unreferenced ones are orphans no note points at).", {"note_path": {"type": "string"}}, [], list_attachments),
    _tool("delete_attachment", "Delete an attachment file (e.g. an orphan no note references). Requires separate confirmation; the bytes remain in git history.", {"path": {"type": "string"}, "rationale": RATIONALE}, ["path"], delete_attachment, mode="confirmed"),
    _tool("read_attachment", "LOOK AT an image or PDF attachment. Use this whenever a note contains an image and the answer depends on what it shows — a lecture slide, a whiteboard photo, a diagram, a screenshot of a problem set, or a PDF of slides or a problem sheet. Reading the note only gives you the markdown link, never the contents. Accepts up to 4 vault paths like 'attachments/photo-a1b2.png' or 'attachments/slides-c3.pdf'.", {"path": {"type": "string"}, "paths": {"type": "array", "items": {"type": "string"}}}, [], read_attachment),
    _tool("list_flashcard_decks", "List every flashcard deck with its card counts and how many are due.", {}, [], list_flashcard_decks),
    _tool("get_flashcard_deck", "Read one deck's cards. `deck` accepts a deck id or a loose name like 'microecon'.", {"deck": {"type": "string"}}, ["deck"], get_flashcard_deck),
    _tool("create_flashcard_deck", "Create a flashcard deck when the user asks for one (e.g. 'make flashcards from this note'). Write the cards yourself from the note's content — good cards are one fact each, question on the front. You may also pass source_notes to harvest existing 'Question :: Answer' lines. Opens the study screen automatically.", {"name": {"type": "string"}, "cards": CARD_ARRAY, "source_notes": {"type": "array", "items": {"type": "string"}}}, ["name"], create_flashcard_deck, mode="direct"),
    _tool("add_flashcards", "Add cards to an existing deck. Duplicate fronts are skipped.", {"deck": {"type": "string"}, "cards": CARD_ARRAY}, ["deck", "cards"], add_flashcards, mode="direct"),
    _tool("rename_flashcard_deck", "Rename a flashcard deck.", {"deck": {"type": "string"}, "name": {"type": "string"}}, ["deck", "name"], rename_flashcard_deck, mode="direct"),
    _tool("open_flashcard_deck", "Open a deck in the full-screen study screen — use this whenever the user asks to study, review, quiz, or 'pull up' cards. order: 'due' for spaced repetition (default), 'least_known' to drill the weakest cards first, 'most_known' to review the strongest.", {"deck": {"type": "string"}, "order": {"type": "string"}}, ["deck"], open_flashcard_deck, mode="direct"),
    _tool("delete_flashcard_deck", "Permanently delete a flashcard deck. Requires separate confirmation.", {"deck": {"type": "string"}}, ["deck"], delete_flashcard_deck, mode="confirmed"),
    _tool("lookup_media", "Search structured media databases — Open Library for books, TMDB for movies/TV, and IGDB / RAWG / Steam for games (tried in that order; Steam needs no key) — for cover art, creator, year, and page/episode counts. Call this FIRST when the user asks to log or track media, then pass the best candidate's fields to log_media. Returns {candidates, error}; on error or no match, log the item anyway with only what the user actually said.", {"media_type": {"type": "string", "enum": list(media_items.MEDIA_TYPES)}, "query": {"type": "string"}}, ["media_type", "query"], lookup_media),
    _tool("log_media", "Start tracking a book, movie, TV show, or game — creates its note in the hidden media/ folder (shown in the Media tab) and downloads cover_url into the vault. Pass metadata from a lookup_media candidate; NEVER invent creator/year/total — omit anything the lookup did not return. status defaults to active; use finished (with a rating) when the user already consumed it. Media-item frontmatter is the interface here: the inline-hashtag rule does not apply.", {"media_type": {"type": "string", "enum": list(media_items.MEDIA_TYPES)}, "title": {"type": "string"}, "creator": {"type": "string", "description": "Author / director / studio"}, "year": {"type": "integer"}, "total": {"type": "integer", "description": "Pages for books, episode count for TV"}, "cover_url": {"type": "string", "description": "https image URL from lookup_media"}, "status": {"type": "string", "enum": list(media_items.STATUSES)}, "rating": {"type": "integer", "description": "Whole number 1-10"}, "source": {"type": "string"}, "rationale": RATIONALE}, ["media_type", "title"], log_media, mode="direct"),
    _tool("search_media_images", "Search for images and SHOW them to the user as a numbered gallery inside the chat — the way to change a media item's backdrop or cover art. Pass the item's `source` id when it has one (that unlocks TMDB stills/posters); always pass a concrete visual `query` too (Wikimedia Commons + web tier). kind='backdrop' finds wide scene imagery, kind='cover' allows portrait poster/cover art. After the gallery appears, the user picks by number — then call set_media_art with that EXACT numbered URL from this tool's output. Never invent or guess an image URL.", {"query": {"type": "string", "description": "Concrete visual search phrase"}, "media_type": {"type": "string", "enum": list(media_items.MEDIA_TYPES)}, "source": {"type": "string", "description": "The item's source id from its frontmatter, e.g. tmdb:movie:603"}, "kind": {"type": "string", "enum": ["backdrop", "cover"]}, "limit": {"type": "integer", "description": "1-12, default 8"}}, ["query"], search_media_images),
    _tool("update_media", "Update a tracked media item: log progress, change status, rate it 1-10, or give it an alias (a custom display name shown in place of the real title). Find the note under media/ (read_structure or keyword_search), read_notes for its base_revision, then call this. entry kinds: started; progress (value = a page number for books, SxxEyy for TV); watched (movies); finished; note (free text for games or anything else). Finishing stamps the date and status automatically, and a rating passed with it lands in the same log line. Pass alias to rename the item's display name (an empty string clears it back to the real title); the underlying note title is left untouched. Pass remove_entry to DELETE a single existing log line (e.g. a stray watch) — read_notes first to copy the entry's exact date and text verbatim.", {"path": {"type": "string"}, "base_revision": REVISION, "entry": {"type": "object", "properties": {"kind": {"type": "string", "enum": list(media_items.ENTRY_KINDS)}, "value": {"type": "string"}, "text": {"type": "string"}, "date": DATE}, "required": ["kind"]}, "remove_entry": {"type": "object", "description": "Delete one log line, matched exactly", "properties": {"at": {"type": "string", "description": "The entry's date, YYYY-MM-DD"}, "text": {"type": "string", "description": "The entry text after the dash, verbatim, e.g. 'watched' or 'finished, rated 8'"}}, "required": ["at", "text"]}, "status": {"type": "string", "enum": list(media_items.STATUSES)}, "rating": {"type": "integer", "description": "Whole number 1-10"}, "alias": {"type": "string", "description": "Custom display name (empty string clears it)"}, "rationale": RATIONALE}, ["path", "base_revision"], update_media, mode="direct"),
    _tool("set_media_art", "Apply a chosen image as a media item's backdrop or cover: downloads the URL into the vault, updates the frontmatter, and the page repaints live. The URL must come verbatim from a search_media_images result the user picked — never invented.", {"path": {"type": "string"}, "kind": {"type": "string", "enum": ["backdrop", "cover"]}, "url": {"type": "string", "description": "Exact https image URL from search_media_images"}, "rationale": RATIONALE}, ["path", "kind", "url"], set_media_art, mode="direct"),
    _tool("set_media_review", "Write or replace a media item's review — the note body above its ## Log section, which the media page displays. Use after drafting a review the user approved (e.g. composed from their log notes and what they said in chat). The log and everything below it are preserved untouched.", {"path": {"type": "string"}, "base_revision": REVISION, "review": {"type": "string", "description": "The full review, markdown"}, "rationale": RATIONALE}, ["path", "base_revision", "review"], set_media_review, mode="direct"),
    _tool("media_details", "Rich detail about one work — synopsis, genres, runtime/episodes, top cast (films/TV via TMDB), description and subjects (books), summary and genres (games). Use BEFORE answering \"what is this about?\" or recommending — never answer such questions purely from memory. Pass the item's source id when it has one, else a title query.", {"media_type": {"type": "string", "enum": list(media_items.MEDIA_TYPES)}, "source": {"type": "string"}, "query": {"type": "string"}}, ["media_type"], media_details),
    _tool("similar_media", "Recommendations grounded in TMDB's similar/recommended data for a film or TV item (by its source id). Combine with media_stats and the user's own ratings when advising what to watch next; offer to add picks to the watchlist (log_media status=planned). Books and games have no provider — the result says so and you reason from the library instead.", {"media_type": {"type": "string", "enum": list(media_items.MEDIA_TYPES)}, "source": {"type": "string", "description": "e.g. tmdb:movie:603"}}, ["media_type"], similar_media),
    _tool("media_stats", "The user's media library in numbers: counts by type and status, the rating histogram and per-type averages, finishes per month, top-rated items. THE tool for \"how many books this year?\", taste questions, and data for show_graphic charts.", {}, [], media_stats),
    _tool("list_notebooks", "List the user's Paper notebooks — handwritten Apple Pencil notebooks written on the iPad and stored in the vault under paper/. Returns each notebook's path, title, folder, page count and favourite flag. THE starting point for anything about handwritten notes, sketches, or 'my notebook'; the page paths it implies are what the per-page tools take. Filter by folder, or favorites_only for the ones the user starred.", {"folder": {"type": "string", "description": "Library folder, e.g. 'Work' or 'paper/Work'"}, "favorites_only": {"type": "boolean"}}, [], list_notebooks),
    _tool("search_handwriting", "Search the text recognised from the user's handwritten Paper pages. Use this whenever a question might be answered by something they wrote by hand — lecture notes, a sketched plan, a whiteboard copy — and ALWAYS before saying you cannot find something in the vault, since handwriting never appears in keyword_search or semantic_search. Returns page paths with snippets; scope it to one notebook with `notebook`.", {"query": {"type": "string"}, "notebook": {"type": "string", "description": "Notebook path, e.g. paper/Work/Ideas.paper"}, "limit": {"type": "integer", "description": "1-50, default 10"}}, ["query"], search_handwriting),
    _tool("read_page", "LOOK AT one handwritten Paper page: returns any recognised text AND the page render as a real image, so you read the handwriting with your own eyes. Use it whenever the answer depends on what is actually on the page — a diagram, a sketch, maths, anything the recognised text mangles. `path` is the page directory from search_handwriting or list_notebooks, e.g. paper/Work/Ideas.paper/pages/<id>.", {"path": {"type": "string"}}, ["path"], read_page),
    _tool("transcribe_page", "Have the model read a handwritten Paper page and store its reading as the page's AI transcript, making it searchable. Costs one vision call, so use it when the user asks for a page to be transcribed or when a page has no usable text yet — not routinely. The stored text is YOUR reading, not the user's words; the ink is never changed.", {"path": {"type": "string"}}, ["path"], transcribe_page),
    _tool("note_from_page", "Turn a handwritten Paper page into a normal markdown note: its transcript, the page image, and a link back to the notebook. Use when the user wants a handwritten page 'as a note', typed up, or somewhere they can edit and link it. The page needs a transcript first.", {"path": {"type": "string"}, "target_path": {"type": "string", "description": "Where to create it; defaults to '<Notebook> — Page N.md'"}, "rationale": RATIONALE}, ["path"], note_from_page, mode="direct"),
]
