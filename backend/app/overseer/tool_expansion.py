"""OMNIA's direct productivity, recovery, settings, and calendar tools."""

from __future__ import annotations

import asyncio
import difflib
import json
import re
import secrets
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..api.meta import AppearanceSettingsBody, _appearance, _redact_remote
from ..flashcards import card_state, cards_from_markdown
from ..flashcards import deck_stats as flashcard_stats
from ..flashcards import new_card as new_flashcard
from ..index import parse as note_parse
from .. import media_items, media_lookup, media_theme, task_boards
from ..scheduler import SCHEDULE_PATH, ScheduleStore, clean_cadence, schedule_tz
from .vision import attachments_in, is_document, load_document, load_image
from ..index.db import content_hash
from ..task_service import (
    clean_task_recurrence,
    create_task as create_task_record,
    delete_task as delete_task_record,
    move_task as move_task_record,
    public_task,
    reorder_tasks as reorder_tasks_record,
    set_task_reminder as set_task_reminder_record,
    update_task as update_task_record,
)
from ..vault import fm
from ..vault import templates as tpl
from ..vault.internal import atomic_json, iso_utc, read_json
from ..index.db import content_hash
from ..proofread import CATEGORIES, anchor
from ..vault.notes import NoteError, NoteStore
from .provider import ToolDef

Executor = Callable[[Any, str, dict], Awaitable[dict]]
PROTECTED_FOLDERS = {"daily", "omnia", "templates", "media"}
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


async def propose_note_create(state, thread_id, inp) -> dict:
    path, content = inp["path"], inp["content"]
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
    normalized = state.store.create(path, inp.get("content"))
    normalized, added = note_parse.normalize_task_metadata(normalized)
    if added:
        state.store.resolve(path).write_text(normalized, encoding="utf-8")
    await state.indexer.index_note(path, normalized)
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
    normalized, _ = note_parse.normalize_task_metadata(
        inp["new_content"], previous_content=current
    )
    normalized = state.store.write(path, normalized)
    await state.indexer.index_note(path, normalized)
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
    return await _record(
        state,
        thread_id,
        "move_note",
        f"Moved {old} to {new}",
        [old, new],
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
    normalized = state.store.write(path, fm.serialize(meta, body))
    await state.indexer.index_note(path, normalized)
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
    return {"folders": state.store.list_folders()}


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
    old, new = inp["path"], inp["new_path"]
    if old in PROTECTED_FOLDERS:
        raise NoteError(400, f"{old} is a built-in folder")
    moved = state.store.rename_folder(old, new)
    for old_note, new_note in moved:
        await state.indexer.rename_note(old_note, new_note)
    return await _record(
        state,
        thread_id,
        "move_folder",
        f"Moved folder {old} to {new}",
        [old, new],
        ["notes", "search", "tasks", "calendar"],
        {"path": new, "old_path": old, "moved_notes": len(moved)},
    )


async def delete_folder(state, thread_id, inp) -> dict:
    path = inp["path"]
    if path in PROTECTED_FOLDERS:
        raise NoteError(400, f"{path} is a built-in folder")
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
        day = date.fromisoformat(requested) if requested else datetime.now(
            ZoneInfo(state.timezone)
        ).date()
    except ValueError as exc:
        raise NoteError(400, "Daily date must be YYYY-MM-DD") from exc
    path = f"daily/{day.isoformat()}.md"
    if state.store.exists(path):
        content = state.store.read(path)
        return {"path": path, "revision": content_hash(content), "created": False}
    content = tpl.render(
        state.settings.vault_dir, "daily", title=day.isoformat(), date=day.isoformat()
    )
    normalized = state.store.create(path, content)
    await state.indexer.index_note(path, normalized)
    return await _record(
        state,
        thread_id,
        "ensure_daily_note",
        f"Created daily note {day.isoformat()}",
        [path],
        ["notes", "calendar"],
        {"path": path, "revision": content_hash(normalized), "created": True},
    )


def _unique_path(state, path: str) -> str:
    original = Path(path)
    candidate = path
    number = 2
    while state.store.exists(candidate):
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
    )
    if content is None:
        raise NoteError(404, "Template not found")
    normalized = state.store.create(path, content)
    normalized, added = note_parse.normalize_task_metadata(normalized)
    if added:
        state.store.resolve(path).write_text(normalized, encoding="utf-8")
    await state.indexer.index_note(path, normalized)
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
    link = f"[[{Path(target).stem}]]"
    if link in current:
        return {"path": path, "linked": target, "already_present": True}
    content = current + ("" if current.endswith("\n") else "\n") + f"\n{link}\n"
    normalized = state.store.write(path, content)
    await state.indexer.index_note(path, normalized)
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


async def list_tasks(state, thread_id, inp) -> dict:
    clauses = ["1=1"]
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
        values.append(inp["due_start"])
    if inp.get("due_end"):
        clauses.append("due <= ?")
        values.append(inp["due_end"])
    rows = await state.db.execute_fetchall(
        f"SELECT * FROM tasks WHERE {' AND '.join(clauses)} "
        "ORDER BY due IS NULL, due, path, line LIMIT 500",
        values,
    )
    tasks = [public_task(dict(row)) for row in rows]
    if not inp.get("include_completed", True):
        tasks = [task for task in tasks if task["status"] != "done"]
    if not tasks:
        return {"tasks": [], "note": await _empty_tasks_note(state, inp)}
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
        remind_at = inp.get("remind_at")
        if not remind_at:
            due = row["due"]
            if not due:
                raise NoteError(
                    400,
                    "This task has no due date — give an explicit remind_at, or set a due date first",
                )
            days_before = int(inp.get("days_before") or 0)
            at_time = str(inp.get("at_time") or "09:00")
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
    state.store.resolve_folder(folder)  # traversal / reserved-name guard
    # Canonicalize to the folder's on-disk casing (case-insensitive filesystems
    # would otherwise orphan the board flag) and refuse a folder that isn't real.
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
        folder, {note.path for note in state.store.list_notes()}
    )
    content = state.store.create(path, task_boards.BOARD_TEMPLATE)
    await state.indexer.index_note(path, content)
    return await _record(
        state,
        thread_id,
        "create_task_board",
        f"Created a task board in {folder}",
        [path],
        ["notes", "search", "tasks"],
        {"path": path, "revision": content_hash(content)},
        navigate=path,
    )


def _task_filter(inp: dict) -> tuple[list[str], list[Any]]:
    """The list_tasks filter, shared by the bulk operations. Returns SQL
    clauses and their bound values; excludes completed tasks unless asked."""
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
        values.append(inp["due_start"])
    if inp.get("due_end"):
        clauses.append("due <= ?")
        values.append(inp["due_end"])
    if not inp.get("include_completed"):
        clauses.append("status != 'done'")
    return clauses, values


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
        return {"changed": 0, "tasks": []}
    if len(rows) > BULK_TASK_LIMIT:
        raise NoteError(
            400, f"That matches {len(rows)} tasks — narrow the filter (max {BULK_TASK_LIMIT} per bulk edit)"
        )

    tomb = getattr(state, "task_tombstones", None)
    affected: set[str] = set()
    for row in rows:
        task_id = row["id"]
        if change == "delete":
            if tomb is not None:
                tomb.record([dict(row)])
                affected.add(TOMBSTONES_PATH)
            await delete_task_record(state, task_id)
            affected.add(row["path"])
        elif change == "set_status":
            task = await update_task_record(state, task_id, status=inp["set_status"])
            affected.add(task["path"])
        elif change == "set_due":
            task = await update_task_record(state, task_id, due=inp.get("set_due") or None)
            affected.add(task["path"])
        else:  # move_to
            task = await move_task_record(state, task_id, inp["move_to"])
            affected.add(row["path"])
            affected.add(task["path"])

    count = len(rows)
    labels = {
        "set_status": f"set status to {inp.get('set_status')}",
        "set_due": "clear the due date" if not inp.get("set_due") else f"set due {inp.get('set_due')}",
        "move_to": f"move to {inp.get('move_to')}",
        "delete": "delete",
    }
    return await _record(
        state,
        thread_id,
        "bulk_edit_tasks",
        f"Bulk {labels[change]} on {count} task{'' if count == 1 else 's'}",
        sorted(affected),
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
        "SELECT id FROM tasks WHERE path = ? AND status = 'done'", (source,)
    )
    if not rows:
        return {"archived": 0, "archive_path": None}
    stem = Path(source).stem
    archive_path = (inp.get("archive_path") or f"archive/{stem} Done.md").strip()
    if not state.store.exists(archive_path):
        created = state.store.create(
            archive_path, f'---\ntitle: "{stem} — Done"\n---\n\n# {stem} — Done\n'
        )
        await state.indexer.index_note(archive_path, created)
    for row in rows:
        await move_task_record(state, row["id"], archive_path)
    count = len(rows)
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
    try:
        today = datetime.now(ZoneInfo(state.timezone)).date().isoformat()
    except (ZoneInfoNotFoundError, AttributeError):
        today = date.today().isoformat()
    target = (inp.get("to") or today).strip()
    try:
        datetime.strptime(target, "%Y-%m-%d")
    except ValueError:
        raise NoteError(400, "Roll-over date must be YYYY-MM-DD")
    clauses = ["status != 'done'", "due IS NOT NULL", "due < ?"]
    values: list[Any] = [target]
    if inp.get("path"):
        clause, bound = _task_scope_clause(inp["path"])
        clauses.append(clause)
        values.extend(bound)
    rows = await state.db.execute_fetchall(
        f"SELECT id FROM tasks WHERE {' AND '.join(clauses)}", values
    )
    if not rows:
        return {"rolled": 0}
    affected: set[str] = set()
    for row in rows:
        task = await update_task_record(state, row["id"], due=target)
        affected.add(task["path"])
    count = len(rows)
    return await _record(
        state,
        thread_id,
        "roll_over_tasks",
        f"Rolled {count} overdue task{'' if count == 1 else 's'} forward to {target}",
        sorted(affected),
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
    board_content = state.store.create(board_path, task_boards.BOARD_TEMPLATE)
    await state.indexer.index_note(board_path, board_content)
    paths.append(board_path)

    seeds = inp.get("seed_notes") or ["Brief", "Log", "Resources"]
    for seed in seeds:
        title = str(seed).strip()
        if not title:
            continue
        note_path = f"{folder}/{title}.md"
        if state.store.exists(note_path):
            continue
        content = state.store.create(note_path, f'---\ntitle: "{title}"\n---\n\n# {title}\n')
        await state.indexer.index_note(note_path, content)
        paths.append(note_path)

    return await _record(
        state,
        thread_id,
        "scaffold_project",
        f"Scaffolded project “{name}”",
        paths,
        ["notes", "search", "tasks"],
        {"folder": folder, "board": board_path, "notes": paths[2:]},
        navigate=board_path,
    )


def _local_today(state) -> str:
    try:
        return datetime.now(ZoneInfo(state.timezone)).date().isoformat()
    except (ZoneInfoNotFoundError, AttributeError):
        return date.today().isoformat()


async def board_digest(state, thread_id, inp) -> dict:
    """A read-only rollup of one board: counts by status, what's overdue/due,
    and what's next. Defaults to the board of the folder in use."""
    board = (inp.get("path") or "").strip()
    if board:
        board = await _resolve_board_arg(state, board)
    else:
        board = await task_boards.nearest_board(
            state.db, await _context_note(state, thread_id)
        ) or "Tasks Inbox.md"
    today = _local_today(state)
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
    return {
        "board": board,
        "total": len(rows),
        "counts": counts,
        "open": counts["todo"] + counts["in_progress"],
        "overdue": overdue,
        "due_today": due_today,
        "next": sorted(upcoming, key=lambda r: r["due"])[:5],
    }


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


async def notify_user(state, thread_id, inp) -> dict:
    """Surface a notification to the user. This is the channel a scheduled or
    proactive run uses to reach them: it shows as an in-app toast when the app
    is open, and — if the user enabled notifications — is also delivered as a
    Web Push so it lands on their device when the app is closed."""
    from .. import push_service

    title = " ".join(str(inp.get("title") or "").split()).strip()
    if not title:
        raise NoteError(400, "A notification needs a title")
    body = str(inp.get("body") or "").strip()[:500] or None
    queue = getattr(state, "alerts", None)
    if queue is None:
        return {"delivered": False, "reason": "no alert channel available"}
    key = f"omnia:{iso_utc()}"
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
    try:
        local_tz = ZoneInfo(state.timezone)
    except ZoneInfoNotFoundError:
        local_tz = ZoneInfo("America/New_York")
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
    day = _activity_day(inp.get("date") or datetime.now(ZoneInfo(state.timezone)).date())
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
        _require_revision(state, path, inp.get("base_revision"))
        target = path
        normalized, _ = note_parse.normalize_task_metadata(
            content, previous_content=state.store.read(path)
        )
        normalized = state.store.write(path, normalized)
    elif mode == "copy":
        target = _unique_path(
            state,
            inp.get("target_path")
            or str(Path(path).with_name(f"{Path(path).stem} restored.md")),
        )
        normalized, _ = note_parse.normalize_task_metadata(content)
        normalized = state.store.create(target, normalized)
    else:
        raise NoteError(400, "Restore mode must be copy or replace")
    await state.indexer.index_note(target, normalized)
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
    return {"items": state.trash.list(), "retention_days": 30}


async def inspect_trash(state, thread_id, inp) -> dict:
    return state.trash.preview(inp["item_id"])


async def restore_trash(state, thread_id, inp) -> dict:
    item = state.trash.preview(inp["item_id"])
    restored = state.trash.restore(inp["item_id"], inp.get("target_path"))
    for path in restored["notes"]:
        await state.indexer.index_note(path, state.store.read(path))
    trash_path = f"{NoteStore.INTERNAL_DIR}/trash/{item['id']}"
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
    raw = _config(state).get("appearance")
    try:
        normalized = _appearance(
            AppearanceSettingsBody.model_validate(raw or {})
        )
    except Exception:
        normalized = _appearance(AppearanceSettingsBody())
    return {"configured": bool(raw), **normalized}


async def set_appearance(state, thread_id, inp) -> dict:
    config = _config(state)
    current = await get_appearance(state, thread_id, {})
    current.pop("configured", None)
    for key in ("theme", "font", "intoxicate", "custom_theme"):
        if key in inp:
            current[key] = inp[key]
    if "theme" in inp or "custom_theme" in inp:
        current["active_saved_theme_id"] = None
    normalized = _appearance(AppearanceSettingsBody.model_validate(current))
    config["appearance"] = normalized
    _write_config(state, config)
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
        "set_appearance",
        "Updated appearance",
        [CONFIG_PATH],
        ["appearance", "settings"],
        {"appearance": normalized},
    )


async def manage_saved_theme(state, thread_id, inp) -> dict:
    config = _config(state)
    current = await get_appearance(state, thread_id, {})
    current.pop("configured", None)
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
    except ZoneInfoNotFoundError as exc:
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
    semantic_errors = 0
    if state.semantic.available:
        for note in notes:
            try:
                await state.semantic.index_note(note.path, state.store.read(note.path))
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


def _event_values(state, inp: dict) -> dict:
    note_path = inp.get("note_path")
    if note_path and not state.store.exists(note_path):
        raise NoteError(404, "Linked note not found")
    return state.events.normalize(
        title=inp["title"],
        start=inp["start"],
        end=inp.get("end"),
        all_day=bool(inp.get("all_day", False)),
        timezone_name=inp.get("timezone") or state.timezone,
        notes=inp.get("notes"),
        note_path=note_path,
        recurrence=inp.get("recurrence"),
        reminders=inp.get("reminders"),
    )


async def list_events(state, thread_id, inp) -> dict:
    try:
        start = date.fromisoformat(inp["start"])
        end = date.fromisoformat(inp["end"])
    except ValueError as exc:
        raise NoteError(400, "Event dates must be YYYY-MM-DD") from exc
    if end < start or (end - start).days > 366:
        raise NoteError(400, "Event range must be at most one year")
    return {"events": state.events.occurrences(start, end)}


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
            "timezone",
            "notes",
            "note_path",
            "recurrence",
            "reminders",
        )
    }
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
    # A bare YYYY-MM-DD is an all-day reminder; anything with a time is timed.
    all_day = "T" not in when and ":" not in when
    end = None
    if not all_day:
        # The event store insists a timed event outlast its start, so a reminder
        # gets a short default block rather than a zero-length one.
        try:
            start_dt = datetime.fromisoformat(when).replace(tzinfo=None)
        except ValueError as exc:
            raise NoteError(
                400, "Reminder time must be YYYY-MM-DD or YYYY-MM-DDTHH:MM"
            ) from exc
        end = (start_dt + timedelta(minutes=15)).isoformat(timespec="minutes")
    reminders = inp.get("reminders") or [0]  # default: alert at the reminder's own time
    values = state.events.normalize(
        title=text,
        start=when,
        end=end,
        all_day=all_day,
        timezone_name=inp.get("timezone") or state.timezone,
        notes=None,
        note_path=inp.get("note_path"),
        recurrence=inp.get("recurrence"),
        reminders=reminders,
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
    "description": "Exact revision returned by read_notes; required for concurrency safety.",
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


# --- media tracker ---------------------------------------------------------


def _media_today(state) -> str:
    try:
        return datetime.now(ZoneInfo(state.timezone)).date().isoformat()
    except (ZoneInfoNotFoundError, AttributeError):
        return date.today().isoformat()


async def lookup_media(state, thread_id, inp) -> dict:
    keys = media_lookup.load_media_keys(state.settings.data_dir)
    return await media_lookup.search_media(inp["media_type"], inp["query"], keys)


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
            today=_media_today(state),
        )
    except ValueError as exc:
        raise NoteError(400, str(exc))
    path = media_items.next_media_path(title, taken)
    written = state.store.create(path, content)
    await state.indexer.index_note(path, written)
    # The Media job sources the page backdrop and palette in the background.
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
    content = state.store.read(path)
    meta, body = fm.parse(content)
    if not media_items.is_media_item(meta):
        raise NoteError(400, f"Not a media note: {path}")
    title = str(meta.get("title") or Path(path).stem)
    ingested = await media_lookup.ingest_cover(
        state, inp["url"], f"{media_items.media_slug(title)}-{kind}"
    )
    meta[kind] = ingested["url"]
    written = state.store.write(path, fm.serialize(meta, body))
    await state.indexer.index_note(path, written)
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
    written = state.store.write(path, updated)
    await state.indexer.index_note(path, written)
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
                today=_media_today(state),
            )
    except ValueError as exc:
        raise NoteError(400, str(exc))
    written = state.store.write(path, updated)
    await state.indexer.index_note(path, written)
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
                start = when
                day = datetime.fromisoformat(when).date()
            else:
                day = date.fromisoformat(when)
        except ValueError as exc:
            raise NoteError(
                400, "when must be YYYY-MM-DD or YYYY-MM-DDTHH:MM"
            ) from exc
    today = datetime.now(ZoneInfo(state.timezone)).date()
    target_day = day or today

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
        state.store.create(daily_path, content)

    board: str | None = None
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
        event = state.events.add(_event_values(state, values))
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
            body = body.rstrip("\n") + f"\n{marker}\n"
            normalized = state.store.write(daily_path, body)
            await state.indexer.index_note(daily_path, normalized)
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


def _thread_matches(thread: dict, needle: str) -> list[dict]:
    hits = []
    for message in thread.get("messages") or []:
        text = str(message.get("text") or "")
        if needle in text.lower():
            at = text.lower().index(needle)
            hits.append(
                {
                    "role": message.get("role"),
                    "excerpt": text[max(0, at - 80) : at + 160].replace("\n", " "),
                }
            )
    return hits


async def search_conversations(state, thread_id, inp) -> dict:
    """Search what was actually said in past conversations."""
    needle = str(inp.get("query") or "").strip().lower()
    if not needle:
        raise NoteError(400, "Provide a query")
    limit = max(1, min(int(inp.get("limit", 10)), 40))
    results = []
    # list() already parses each thread in full, so re-fetching with get() was
    # a second complete parse of files that can carry 400k chars of history.
    for thread in state.threads.list(include_messages=True):
        hits = _thread_matches(thread, needle)
        if hits:
            results.append(
                {
                    "thread_id": thread["id"],
                    "title": thread.get("title"),
                    "updated_at": thread.get("updated_at"),
                    "match_count": len(hits),
                    "matches": hits[:3],
                }
            )
        if len(results) >= limit:
            break
    results.sort(key=lambda r: str(r.get("updated_at") or ""), reverse=True)
    return {"conversations": results, "query": needle}


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
    return {"conversations": state.threads.list()[: max(1, min(int(inp.get("limit", 20)), 100))]}


# --- memory ----------------------------------------------------------------

MEMORY_NOTE = "omnia/memory.md"
MEMORY_SECTION = "## Notes to self"


async def remember(state, thread_id, inp) -> dict:
    """Append one durable fact to the memory note.

    Memory previously required composing and staging the WHOLE file through the
    review tray — an extra, unrequested tool call competing with a persona that
    says be brief and act. It never once happened. This is one cheap call.
    """
    text = " ".join(str(inp.get("text") or "").split())
    if not text:
        raise NoteError(400, "Provide the fact to remember")
    if len(text) > 400:
        raise NoteError(400, "Keep a memory entry under 400 characters")

    try:
        content = state.store.read(MEMORY_NOTE)
    except NoteError:
        content = state.store.create(
            MEMORY_NOTE, "---\ntitle: OMNIA Memory\n---\n\n"
        )

    entry = f"- {text}"
    if entry in content:
        return {"remembered": False, "reason": "already recorded", "text": text}
    lines = content.split("\n")
    if MEMORY_SECTION in lines:
        # Newest first, directly under the heading.
        at = lines.index(MEMORY_SECTION)
        lines.insert(at + 1, entry)
        content = "\n".join(lines)
    else:
        content = content.rstrip("\n") + f"\n\n{MEMORY_SECTION}\n{entry}\n"

    normalized = state.store.write(MEMORY_NOTE, content)
    await state.indexer.index_note(MEMORY_NOTE, normalized)
    return await _record(
        state,
        thread_id,
        "remember",
        f"Noted: {text[:80]}",
        [MEMORY_NOTE],
        ["notes"],
        {"remembered": True, "text": text},
    )


async def forget(state, thread_id, inp) -> dict:
    """Drop memory lines matching a phrase.

    This used to delete every bullet *containing* the phrase, so forgetting "e"
    took the whole note with it. Exact entries win; a partial match is honoured
    only when it is unambiguous, so a vague call now fails instead of
    over-deleting.
    """
    needle = " ".join(str(inp.get("text") or "").split()).lower()
    if not needle:
        raise NoteError(400, "Provide the text to forget")
    content = state.store.read(MEMORY_NOTE)
    lines = content.split("\n")
    entries = [
        (index, " ".join(line.strip()[2:].split()).lower())
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
    normalized = state.store.write(MEMORY_NOTE, "\n".join(kept))
    await state.indexer.index_note(MEMORY_NOTE, normalized)
    return await _record(
        state,
        thread_id,
        "forget",
        f"Forgot {len(removed)} memory entr(y/ies)",
        [MEMORY_NOTE],
        ["notes"],
        {"forgot": removed},
    )


# --- bulk librarian work ---------------------------------------------------


def _tag_pattern(tag: str) -> re.Pattern:
    # Inline #tags only; never touches a "# Heading" or a longer tag that
    # merely starts with this one (#ml must not match #mlops).
    return re.compile(rf"(?<![\w/#])#{re.escape(tag)}(?![\w/-])")


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
        replaced, count = pattern.subn(f"#{new}", content)
        if count:
            hits.append({"path": path, "occurrences": count, "content": replaced})

    if not hits:
        return {"renamed": 0, "note": f"No notes contain the inline tag #{old}."}
    if inp.get("dry_run"):
        return {
            "dry_run": True,
            "from": old,
            "to": new,
            "would_change": [{"path": h["path"], "occurrences": h["occurrences"]} for h in hits],
            "note_count": len(hits),
        }

    for hit in hits:
        normalized = state.store.write(hit["path"], hit["content"])
        await state.indexer.index_note(hit["path"], normalized)
    return await _record(
        state,
        thread_id,
        "rename_tag",
        f"Renamed #{old} to #{new} across {len(hits)} note(s)",
        [h["path"] for h in hits],
        ["notes", "tags"],
        {
            "from": old,
            "to": new,
            "note_count": len(hits),
            "paths": [h["path"] for h in hits],
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
    _require_revision(state, path, inp.get("base_revision"))

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

    normalized = state.store.write(path, content)
    await state.indexer.index_note(path, normalized)
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
    return {"unlinked_mentions": found, "truncated": False}


# --- Phase 4: knowledge & power vault operations ---------------------------


async def ask_vault(state, thread_id, inp) -> dict:
    """Retrieve the notes most relevant to a question so the answer can cite
    them. Semantic first, keyword to fill in — the caller synthesizes."""
    query = " ".join(str(inp.get("query") or "").split()).strip()
    if not query:
        raise NoteError(400, "Ask a question")
    k = max(1, min(int(inp.get("k") or 6), 12))
    sources: list[dict] = []
    seen: set[str] = set()
    if getattr(state.semantic, "available", False):
        try:
            for result in await state.semantic.search(query, k=k):
                if result["path"] in seen:
                    continue
                seen.add(result["path"])
                sources.append(
                    {
                        "path": result["path"],
                        "heading": result.get("heading"),
                        "excerpt": (result.get("text") or "")[:800],
                    }
                )
        except Exception:
            pass
    if len(sources) < k:
        try:
            from .tools import keyword_search

            kw = await keyword_search(state, thread_id, {"query": query, "limit": k})
            for result in kw.get("results") or kw.get("notes") or []:
                path = result.get("path")
                if not path or path in seen:
                    continue
                seen.add(path)
                sources.append(
                    {"path": path, "title": result.get("title"), "snippet": result.get("snippet")}
                )
        except Exception:
            pass
    return {
        "query": query,
        "sources": sources[:k],
        "note": "Answer only from these sources and cite the note paths; say so if they don't cover it.",
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
        try:
            today = datetime.now(ZoneInfo(state.timezone)).date()
        except (ZoneInfoNotFoundError, AttributeError):
            today = date.today()
        when = (today + timedelta(days=days)).isoformat()
    all_day = "T" not in when and ":" not in when
    end = None
    if not all_day:
        try:
            start_dt = datetime.fromisoformat(when).replace(tzinfo=None)
        except ValueError as exc:
            raise NoteError(400, "when must be YYYY-MM-DD or YYYY-MM-DDTHH:MM") from exc
        end = (start_dt + timedelta(minutes=15)).isoformat(timespec="minutes")
    meta, _body = fm.parse(state.store.read(path))
    title = str(meta.get("title") or Path(path).stem)
    values = state.events.normalize(
        title=f"Follow up: {title}",
        start=when,
        end=end,
        all_day=all_day,
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
    for info in state.store.list_notes():
        path = info.path
        if path.startswith("templates/"):
            continue
        if folder and not path.startswith(folder + "/"):
            continue
        if tagged is not None and path not in tagged:
            continue
        meta, body = fm.parse(state.store.read(path))
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
        normalized = state.store.write(path, fm.serialize(meta, body))
        await state.indexer.index_note(path, normalized)
        changed.append(path)
    if not changed:
        return {"changed": 0}
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
        content = state.store.read(source)
        touched = False
        for item in items:
            title = item["title"]
            pattern = re.compile(rf"(?<![\w\[]){re.escape(title)}(?![\w\]])")
            content, count = pattern.subn(f"[[{title}]]", content, count=1)
            if count:
                touched = True
                linked += 1
        if touched:
            normalized = state.store.write(source, content)
            await state.indexer.index_note(source, normalized)
            changed.append(source)
    if not changed:
        return {"linked": 0}
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
    hits: list[dict] = []
    for info in state.store.list_notes():
        if info.path.startswith("templates/"):
            continue
        try:
            content = state.store.read(info.path)
        except NoteError:
            continue
        if case_sensitive:
            count = content.count(find)
            new = content.replace(find, replace) if count else content
        else:
            new, count = pattern.subn(replace, content)
        if count:
            hits.append({"path": info.path, "occurrences": count, "content": new})
    if not hits:
        return {"changed": 0, "note": f"No notes contain “{find}”."}
    total = sum(h["occurrences"] for h in hits)
    if inp.get("dry_run"):
        return {
            "dry_run": True,
            "find": find,
            "replace": replace,
            "note_count": len(hits),
            "total_occurrences": total,
            "would_change": [{"path": h["path"], "occurrences": h["occurrences"]} for h in hits],
        }
    for hit in hits:
        normalized = state.store.write(hit["path"], hit["content"])
        await state.indexer.index_note(hit["path"], normalized)
    return await _record(
        state,
        thread_id,
        "find_replace_across_vault",
        f"Replaced “{find}” in {len(hits)} note(s)",
        [h["path"] for h in hits],
        ["notes", "search", "tasks"],
        {"note_count": len(hits), "total_occurrences": total},
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
    for line in body.splitlines():
        heading = re.match(r"^##\s+(.+?)\s*$", line)
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
    links: list[str] = []
    for title, section_lines in sections:
        slug = re.sub(r"[^\w\- ]", "", title).strip() or "Section"
        child_path = _unique_path(state, f"{folder + '/' if folder else ''}{slug}.md")
        child_body = "\n".join(section_lines).strip() + "\n"
        child = state.store.create(child_path, fm.serialize({"title": title}, child_body))
        await state.indexer.index_note(child_path, child)
        made.append(child_path)
        links.append(f"- [[{title}]]")
    lead = "\n".join(preamble).strip()
    new_body = (lead + "\n\n" if lead else "") + "\n".join(links) + "\n"
    normalized = state.store.write(path, fm.serialize(meta, new_body))
    await state.indexer.index_note(path, normalized)
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

    titles: dict[str, str] = {}
    blocks: list[str] = []
    for path in sources:
        meta, body = fm.parse(state.store.read(path))
        title = str(meta.get("title") or Path(path).stem)
        titles[path] = title
        blocks.append(f"## {title}\n\n{body.strip()}\n")
    combined = "\n".join(blocks)

    if state.store.exists(target):
        emeta, ebody = fm.parse(state.store.read(target))
        target_title = str(emeta.get("title") or Path(target).stem)
        state.store.write(target, fm.serialize(emeta, ebody.rstrip() + "\n\n" + combined))
    else:
        target_title = Path(target).stem
        state.store.create(target, fm.serialize({"title": target_title}, combined))
    await state.indexer.index_note(target, state.store.read(target))
    affected = [target]

    for path in sources:
        source_title = titles[path]
        # Wikilinks resolve case-insensitively, so match that way too.
        pattern = re.compile(
            rf"\[\[{re.escape(source_title)}((?:\|[^\]]*|#[^\]]*)?)\]\]", re.IGNORECASE
        )
        rows = await state.db.execute_fetchall(
            "SELECT DISTINCT source FROM links WHERE target_lower = ?", (source_title.lower(),)
        )
        for row in rows:
            linker = row["source"]
            if linker in sources or linker == target:
                continue
            try:
                content = state.store.read(linker)
            except NoteError:
                continue
            new = pattern.sub(lambda m: f"[[{target_title}{m.group(1)}]]", content)
            if new != content:
                normalized = state.store.write(linker, new)
                await state.indexer.index_note(linker, normalized)
                affected.append(linker)

    for path in sources:
        state.trash.trash_note(path)
        await state.indexer.remove_note(path)
        affected.append(path)

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
    created = state.store.create(out_path, combined)
    await state.indexer.index_note(out_path, created)
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
            frm = int(sel.get("from") or 0)
            to = int(sel.get("to") or 0)
            if int(sel.get("doc_len") or -1) == len(content) and 0 <= frm <= to <= len(content):
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
            f"{'#' * lv} {re.sub(r'^#+\\s*', '', line).strip()}" if line.strip() else line
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
            f"- [ ] {line.lstrip('-*[ ]x').strip()}" if line.strip() else line for line in lines
        )
    return text


async def edit_selection(state, thread_id, inp) -> dict:
    """Replace the user's selection with new text; an empty selection inserts at
    the cursor. Acts on the exact live range, or on old_string as a fallback."""
    new_text = str(inp.get("new_string") or "")
    path, content, frm, to = await _resolve_selection(state, thread_id, inp)
    updated = content[:frm] + new_text + content[to:]
    normalized = state.store.write(path, updated)
    await state.indexer.index_note(path, normalized)
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
    normalized = state.store.write(path, updated)
    await state.indexer.index_note(path, normalized)
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
    new_content = state.store.create(new_path, fm.serialize({"title": slug}, extracted + "\n"))
    await state.indexer.index_note(new_path, new_content)
    updated = content[:frm] + f"[[{slug}]]" + content[to:]
    normalized = state.store.write(path, updated)
    await state.indexer.index_note(path, normalized)
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
    "search": "/search",
    "graph": "/graph",
    "trash": "/trash",
    "pending": "/pending",
    "settings": "/settings",
    "omnia": "/omnia",
}


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


async def open_in_app(state, thread_id, inp) -> dict:
    """Open a note or a view in the app (navigation only — changes no data)."""
    view = str(inp.get("view") or "").strip().lower()
    note = str(inp.get("note") or "").strip()
    if view:
        route = _APP_VIEWS.get(view)
        if not route:
            raise NoteError(400, f"Unknown view — choose from: {', '.join(sorted(_APP_VIEWS))}")
        return await _record(
            state, thread_id, "open", f"Opened {view}", [], [], {"view": view},
            undoable=False, navigate=route,
        )
    if not note:
        raise NoteError(400, "Give a note to open, or a view (calendar, tasks, media, …)")
    path = await _resolve_note_path(state, note)
    return await _record(
        state, thread_id, "open", f"Opened {path}", [], [], {"path": path},
        undoable=False, navigate=f"/note/{path}",
    )


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
    return {"related": related[:k]}


async def get_forward_links(state, thread_id, inp) -> dict:
    """What this note links out to (the complement of get_backlinks)."""
    path = inp["path"]
    rows = await state.db.execute_fetchall(
        "SELECT DISTINCT target, target_lower FROM links WHERE source = ?", (path,)
    )
    notes = await state.db.execute_fetchall("SELECT path FROM notes")
    by_stem: dict[str, str] = {}
    for note in notes:
        by_stem.setdefault(_note_stem(note["path"]), note["path"])
    out = []
    for row in rows:
        resolved = by_stem.get(str(row["target_lower"]))
        out.append({"target": row["target"], "path": resolved, "exists": resolved is not None})
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
        targets = {r["target_lower"] for r in await state.db.execute_fetchall("SELECT DISTINCT target_lower FROM links")}
        result = [n for n in notes if n["path"] not in sources and _note_stem(n["path"]) not in targets]
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
    return {"filter": kind, "count": len(result), "notes": trimmed}


async def find_broken_links(state, thread_id, inp) -> dict:
    """[[wikilinks]] whose target note doesn't exist."""
    stems = {_note_stem(r["path"]) for r in await state.db.execute_fetchall("SELECT path FROM notes")}
    rows = await state.db.execute_fetchall("SELECT DISTINCT source, target, target_lower FROM links")
    broken = [
        {"in_note": r["source"], "target": r["target"]}
        for r in rows
        if str(r["target_lower"]) not in stems
    ]
    limit = max(1, min(int(inp.get("limit") or 100), 500))
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
    normalized, _ = note_parse.normalize_task_metadata(content, previous_content=state.store.read(path))
    written = state.store.write(path, normalized)
    await state.indexer.index_note(path, written)
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
    return {"items": [dict(r) for r in rows], "count": len(rows)}


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
    try:
        updated, summary = media_items.apply_update(
            state.store.read(path),
            rating=inp.get("rating"),
            status=status,
            entry=entry,
            alias=None,
            today=_media_today(state),
        )
    except ValueError as exc:
        raise NoteError(400, str(exc))
    written = state.store.write(path, updated)
    await state.indexer.index_note(path, written)
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
    _require_revision(state, path, inp.get("base_revision"))
    meta, body = fm.parse(state.store.read(path))
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
    normalized = state.store.write(path, fm.serialize(meta, "\n".join(new_lines) + trailing))
    await state.indexer.index_note(path, normalized)
    return await _record(
        state, thread_id, "format_table", f"Updated a table in {path}", [path],
        ["notes", "search"], {"path": path, "revision": content_hash(normalized)},
    )


async def sort_list(state, thread_id, inp) -> dict:
    """Sort (and optionally dedupe) a bullet list, scoped to a heading."""
    path = inp["path"]
    _require_revision(state, path, inp.get("base_revision"))
    meta, body = fm.parse(state.store.read(path))
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
    normalized = state.store.write(path, fm.serialize(meta, "\n".join(new_lines) + trailing))
    await state.indexer.index_note(path, normalized)
    return await _record(
        state, thread_id, "sort_list", f"Sorted a list in {path}", [path],
        ["notes", "search"], {"path": path, "revision": content_hash(normalized)},
    )


async def insert_toc(state, thread_id, inp) -> dict:
    """Insert a table of contents built from the note's headings."""
    path = inp["path"]
    _require_revision(state, path, inp.get("base_revision"))
    meta, body = fm.parse(state.store.read(path))
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
    normalized = state.store.write(path, fm.serialize(meta, "\n".join(new_lines) + trailing))
    await state.indexer.index_note(path, normalized)
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
    meta, body = fm.parse(state.store.read(path))
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
    normalized = state.store.write(path, fm.serialize(meta, body))
    await state.indexer.index_note(path, normalized)
    return await _record(
        state, thread_id, "embed_attachment", f"Embedded {match} in {path}", [path],
        ["notes", "search"], {"path": path, "attachment": match, "revision": content_hash(normalized)},
    )


async def extract_action_items(state, thread_id, inp) -> dict:
    """Pull bullet/checkbox action items out of a note onto a task board."""
    path = inp["path"]
    board = str(inp.get("board") or "").strip()
    if not board:
        raise NoteError(400, "Give the destination board (a note path)")
    if not state.store.exists(board):
        raise NoteError(404, "Destination board not found")
    _require_revision(state, path, inp.get("base_revision"))
    meta, body = fm.parse(state.store.read(path))
    lines = body.splitlines()
    lo, hi = _scope_bounds(lines, str(inp.get("heading") or "").strip())
    item = re.compile(r"^\s*[-*]\s+(?:\[[ xX/]\]\s+)?(.*\S)\s*$")
    picked: list[int] = []
    texts: list[str] = []
    in_fence = False
    for i in range(lo, hi):
        stripped = lines[i].strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = item.match(lines[i])
        if match and match.group(1).strip():
            texts.append(match.group(1).strip())
            picked.append(i)
    if not texts:
        return {"extracted": 0}
    created = [await create_task_record(state, text=text, path=board) for text in texts]
    paths = [board]
    if inp.get("remove", True):
        drop = set(picked)
        trailing = "\n" if body.endswith("\n") else ""
        kept = "\n".join(line for i, line in enumerate(lines) if i not in drop) + trailing
        normalized = state.store.write(path, fm.serialize(meta, kept))
        await state.indexer.index_note(path, normalized)
        paths = [path, board]
    return await _record(
        state,
        thread_id,
        "extract_action_items",
        f"Extracted {len(created)} action item(s) from {path} to {board}",
        paths,
        ["notes", "tasks", "calendar", "search"],
        {"extracted": len(created), "board": board},
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
    normalized = state.store.write(path, fm.serialize(meta, body))
    await state.indexer.index_note(path, normalized)
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
        new_path = target if not state.store.exists(target) else _unique_path(state, target)
    else:
        stem = path[:-3] if path.endswith(".md") else path
        new_path = _unique_path(state, f"{stem} copy.md")
    fresh = note_parse.regenerate_task_ids(content)
    created = state.store.create(new_path, fresh)
    await state.indexer.index_note(new_path, created)
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
    new_path = _unique_path(state, new_path)
    old_title = Path(path).stem
    state.store.rename(path, new_path)
    await state.indexer.rename_note(path, new_path)
    affected = [path, new_path]
    pattern = re.compile(
        rf"\[\[{re.escape(old_title)}((?:\|[^\]]*|#[^\]]*)?)\]\]", re.IGNORECASE
    )
    rows = await state.db.execute_fetchall(
        "SELECT DISTINCT source FROM links WHERE target_lower = ?", (old_title.lower(),)
    )
    for row in rows:
        linker = row["source"]
        if linker in (path, new_path):
            continue
        try:
            content = state.store.read(linker)
        except NoteError:
            continue
        rewritten = pattern.sub(lambda m: f"[[{slug}{m.group(1)}]]", content)
        if rewritten != content:
            normalized = state.store.write(linker, rewritten)
            await state.indexer.index_note(linker, normalized)
            affected.append(linker)
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


async def get_outline(state, thread_id, inp) -> dict:
    """A note's heading map + word count, to navigate long notes without pulling
    the whole body."""
    path = inp["path"]
    meta, body = fm.parse(state.store.read(path))
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
    return {
        "path": path,
        "title": str(meta.get("title") or Path(path).stem),
        "headings": headings,
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
    _require_revision(state, source, inp.get("base_revision"))
    meta, body = fm.parse(state.store.read(source))
    lines = body.splitlines()
    section = _find_heading_section(lines, heading)
    if section is None:
        raise NoteError(404, f"No section titled “{heading}” in {source}")
    start, _level, end = section
    section_text = "\n".join(lines[start:end]).strip() + "\n"
    remaining = "\n".join(lines[:start] + lines[end:]).rstrip("\n") + "\n"
    norm_source = state.store.write(source, fm.serialize(meta, remaining))
    await state.indexer.index_note(source, norm_source)
    tmeta, tbody = fm.parse(state.store.read(target))
    norm_target = state.store.write(
        target, fm.serialize(tmeta, tbody.rstrip("\n") + "\n\n" + section_text)
    )
    await state.indexer.index_note(target, norm_target)
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
        return {"converted": 0}
    new_body = "\n".join(lines) + ("\n" if body.endswith("\n") else "")
    minted, _added = note_parse.normalize_task_metadata(fm.serialize(meta, new_body))
    written = state.store.write(path, minted)
    await state.indexer.index_note(path, written)
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
    if not new_end:
        try:
            if all_day:
                span = date.fromisoformat(event["end"]) - date.fromisoformat(event["start"])
                new_end = (date.fromisoformat(new_start) + span).isoformat()
            else:
                span = datetime.fromisoformat(event["end"]) - datetime.fromisoformat(event["start"])
                new_end = (datetime.fromisoformat(new_start) + span).isoformat(timespec="minutes")
        except ValueError:
            new_end = None
    values = state.events.normalize(
        title=event["title"],
        start=new_start,
        end=new_end,
        all_day=all_day,
        timezone_name=event.get("timezone") or state.timezone,
        notes=event.get("notes"),
        note_path=event.get("note_path"),
        recurrence=event.get("recurrence"),
        reminders=event.get("reminders"),
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
    favorites = inp.get("favorites")
    if favorites is not None:
        if len(favorites) > MAX_FAVORITES:
            raise NoteError(400, f"Favorites are limited to {MAX_FAVORITES} items")
        cleaned: list[str] = []
        for value in favorites:
            path = str(value)
            if path in cleaned:
                continue
            if await _item(state.db, path) is None:
                raise NoteError(400, f"Not a tracked media item: {path}")
            cleaned.append(path)
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


async def bulk_log_media(state, thread_id, inp) -> dict:
    """Track several media items at once (bare — no per-item cover lookup; add
    art later with set_media_art)."""
    items = inp.get("items") or []
    if not items:
        raise NoteError(400, "Give the media items to log")
    taken = {note.path for note in state.store.list_notes()}
    lowered = {p.lower() for p in taken}
    created: list[dict] = []
    paths: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        media_type = item.get("media_type")
        if not title or not media_type:
            continue
        slug_path = f"{media_items.MEDIA_DIR}/{media_items.media_slug(title)}.md"
        if slug_path.lower() in lowered:
            continue  # already tracked; skip rather than fail the whole batch
        try:
            content = media_items.build_item_content(
                title=title,
                media_type=media_type,
                status=item.get("status") or "planned",
                creator=item.get("creator"),
                year=item.get("year"),
                total=item.get("total"),
                rating=item.get("rating"),
                today=_media_today(state),
            )
        except ValueError as exc:
            raise NoteError(400, f"{title}: {exc}")
        path = media_items.next_media_path(title, taken)
        written = state.store.create(path, content)
        await state.indexer.index_note(path, written)
        media_theme.enrich_soon(state, path)
        taken.add(path)
        lowered.add(path.lower())
        created.append({"path": path, "title": title})
        paths.append(path)
    if not created:
        return {"logged": 0}
    return await _record(
        state,
        thread_id,
        "bulk_log_media",
        f"Logged {len(created)} media item{'' if len(created) == 1 else 's'}",
        paths,
        ["notes", "search", "media"],
        {"items": created},
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
    for source, other in ((a, b), (b, a)):
        link = f"[[{Path(other).stem}]]"
        content = state.store.read(source)
        if link in content:
            continue
        updated = content + ("" if content.endswith("\n") else "\n") + f"\n{link}\n"
        normalized = state.store.write(source, updated)
        await state.indexer.index_note(source, normalized)
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


async def _resolve_task_by_text(state, text: str, path: str | None = None) -> dict:
    """Find the task a phrase refers to. Best match then act: exact text beats
    partial, open beats done, most-recent breaks ties."""
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
            r["status"] != "done",
            str(r.get("created_at") or ""),
        ),
        reverse=True,
    )
    return rows[0]


async def complete_task(state, thread_id, inp) -> dict:
    """Check off (or reopen) a task addressed by its text."""
    row = await _resolve_task_by_text(state, inp.get("text"), inp.get("path"))
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
    path = inp.get("path") or await _default_task_path(state, thread_id)
    created = []
    for text, due in cleaned:
        created.append(await create_task_record(state, text=text, path=path, due=due))
    paths = sorted({task["path"] for task in created})
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
    return await _record(
        state,
        thread_id,
        "archive_note",
        f"Archived {path}",
        [path, dest],
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
    return {"queued_alerts": alerts, "pending_confirmations": confirmations}


async def list_templates(state, thread_id, inp) -> dict:
    return {"templates": tpl.list_templates(state.settings.vault_dir)}


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
    # Which files does any note still point at? A removed image otherwise
    # lingers invisibly forever — this is what makes orphans findable.
    referenced: set[str] = set()
    for info in state.store.list_notes():
        for rel in attachments_in(state.store.read(info.path)):
            referenced.add(rel.rsplit("/", 1)[-1])
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
    for path in paths:
        if is_document(path):
            doc = load_document(state.store, path)
            loaded.append({**doc, "kind": "document"})
        else:
            loaded.append({**load_image(state.store, path), "kind": "image"})
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
    return {"decks": state.flashcards.summaries()}


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
    _tool("set_task_reminder", "Set or clear a reminder on a task, so its due date actually alerts the user. Give remind_at as an explicit YYYY-MM-DDTHH:MM, or omit it to remind relative to the task's own due date (days_before, default 0, at at_time, default 09:00). Pass clear: true to remove the reminder.", {"task_id": {"type": "string"}, "remind_at": {"type": "string", "description": "Explicit local reminder time, YYYY-MM-DDTHH:MM."}, "days_before": {"type": "integer", "description": "Days before the task's due date to remind. Needs the task to have a due date."}, "at_time": {"type": "string", "description": "Time of day HH:MM for a due-relative reminder. Default 09:00."}, "clear": {"type": "boolean"}}, ["task_id"], set_task_reminder, mode="direct"),
    _tool("bulk_edit_tasks", "Apply one change to every task matching a filter, in a single undoable step — e.g. push all of this week's open tasks to Monday, or clear a column. Filter by status/text/path/due range; then exactly one of set_status, set_due (empty clears), move_to (target note), or delete. Requires confirmation.", {"status": STATUS, "text": {"type": "string"}, "path": TASK_SCOPE, "due_start": DATE, "due_end": DATE, "include_completed": {"type": "boolean"}, "set_status": STATUS, "set_due": {"type": "string", "description": "New due date, or empty string to clear it."}, "move_to": {"type": "string", "description": "Note or board to move the matching tasks into."}, "delete": {"type": "boolean"}}, [], bulk_edit_tasks, mode="confirmed"),
    _tool("archive_completed_tasks", "Sweep a board's completed tasks into a dated archive note, keeping their completion record. Keeps a long-running board readable.", {"path": {"type": "string", "description": "Board (its note path or folder name) or note to archive completed tasks from."}, "archive_path": {"type": "string", "description": "Where to file them. Defaults to archive/<name> Done.md."}}, ["path"], archive_completed_tasks, mode="direct"),
    _tool("roll_over_tasks", "Carry overdue open tasks forward: set the due date of every unfinished, past-due task to today (or a given date). Scope to one board with path.", {"to": DATE, "path": TASK_SCOPE}, [], roll_over_tasks, mode="direct"),
    _tool("scaffold_project", "Stand up a whole project workspace in one step: a folder, its own task board, and seeded notes (Brief, Log, Resources by default). Use for 'start a project for X'.", {"name": {"type": "string"}, "parent": {"type": "string", "description": "Optional parent folder to nest the project under."}, "seed_notes": {"type": "array", "items": {"type": "string"}, "description": "Seed note titles. Defaults to Brief, Log, Resources."}}, ["name"], scaffold_project, mode="direct"),
    _tool("create_recurring_task", "Create a recurring task (a chore that reappears). It carries a recurrence and a first due date; when the user completes it, the next occurrence is created automatically with the next due date. Use for 'water the plants every 3 days', 'pay rent monthly'.", {"text": {"type": "string"}, "due": {"type": "string", "description": "First due date, YYYY-MM-DD."}, "recurrence": RECURRENCE, "path": {"type": "string"}, "due_visible": {"type": "boolean"}, "description": {"type": "string"}}, ["text", "due", "recurrence"], create_recurring_task, mode="direct"),
    _tool("board_digest", "A read-only rollup of one task board: counts by status, what's overdue, due today, and next up. Omit path to use the board of the folder the user has open. Answers 'where is this project at?'.", {"path": {"type": "string", "description": "Board (its note path or folder name) or note to summarize. Defaults to the current folder's board."}}, [], board_digest),
    _tool("generate_daily_briefing", "Compile the day's briefing — overdue and due-today tasks, today's events, event reminders, and open counts per board — as structured data to turn into a short brief. Read-only.", {"days": {"type": "integer", "description": "How many days of horizon to include. Default 1 (today); use 7 for a week-ahead brief."}}, [], generate_daily_briefing),
    _tool("notify_user", "Send the user an in-app notification (title + optional body). This is how a scheduled or proactive run reaches them; it shows when the app is open, there is no external push. Use it to deliver a briefing summary or flag something that needs attention.", {"title": {"type": "string"}, "body": {"type": "string"}}, ["title"], notify_user),
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
    _tool("create_event", "Create a standalone or recurring calendar event.", {"title": {"type": "string"}, "start": {"type": "string"}, "end": {"type": "string"}, "all_day": {"type": "boolean"}, "timezone": {"type": "string"}, "notes": {"type": "string"}, "note_path": {"type": "string"}, "recurrence": RECURRENCE, "reminders": {"type": "array", "items": {"type": "integer"}}}, ["title", "start"], create_event, mode="direct"),
    _tool("update_event", "Update an event using its revision. Empty notes/note_path and an empty recurrence object clear those fields.", {"event_id": {"type": "string"}, "expected_revision": REVISION, "title": {"type": "string"}, "start": {"type": "string"}, "end": {"type": "string"}, "all_day": {"type": "boolean"}, "timezone": {"type": "string"}, "notes": {"type": "string"}, "note_path": {"type": "string"}, "recurrence": {"type": "object"}, "reminders": {"type": "array", "items": {"type": "integer"}}}, ["event_id", "expected_revision"], update_event, mode="direct"),
    _tool("delete_event", "Delete a standalone event after separate confirmation.", {"event_id": {"type": "string"}, "expected_revision": REVISION}, ["event_id", "expected_revision"], delete_event, mode="confirmed"),
    _tool("create_reminder", "Set a standalone reminder that alerts the user at a chosen time — a plain nudge not tied to an existing task or event ('remind me to call the dentist at 3pm'). It is stored as a calendar event carrying a reminder, so it also shows on the calendar and can be updated or deleted with the event tools.", {"text": {"type": "string"}, "when": {"type": "string", "description": "YYYY-MM-DD for an all-day reminder, or YYYY-MM-DDTHH:MM for a specific time."}, "reminders": {"type": "array", "items": {"type": "integer"}, "description": "Optional minutes-before offsets. Defaults to [0]: alert at the reminder's own time."}, "timezone": {"type": "string"}, "note_path": {"type": "string", "description": "Optional note to link the reminder to."}, "recurrence": RECURRENCE}, ["text", "when"], create_reminder, mode="direct"),
    _tool("capture", "FILE something the user just told you, in one step. Use this whenever they state a plan, a commitment, an appointment, or something to do — 'tomorrow I'm going to the library', 'I need to email the professor', 'exam on the 12th'. kind='event' for something happening at a time or on a day, kind='task' for something to do, kind='note' only when it is neither. It creates the task or calendar event AND writes a line into that day's daily note, so the written record and the structured record agree. Prefer this over ensure_daily_note + patch_note.", {"text": {"type": "string"}, "kind": {"type": "string", "enum": ["task", "event", "note"]}, "when": {"type": "string"}, "reminders": {"type": "array", "items": {"type": "integer"}}}, ["text", "kind"], capture, mode="direct"),
    _tool("search_conversations", "Search what was said in past conversations with the user. Use this when they refer to something you discussed before — 'what did we decide', 'like we talked about', 'you suggested'. Your own memory of earlier threads is not otherwise available.", {"query": {"type": "string"}, "limit": {"type": "integer"}}, ["query"], search_conversations),
    _tool("read_conversation", "Read a past conversation's transcript, by thread_id from search_conversations or list_conversations.", {"thread_id": {"type": "string"}, "limit": {"type": "integer"}}, ["thread_id"], read_conversation),
    _tool("list_conversations", "List recent conversation threads with titles and dates.", {"limit": {"type": "integer"}}, [], list_conversations),
    _tool("remember", "Record ONE durable fact about the user in your memory note — who they are, what they are working on, a preference, a convention they follow. Call this the moment something durable comes up; it appends for you, so there is no need to rewrite the note. Not for trivia, not for anything the vault already says.", {"text": {"type": "string"}}, ["text"], remember, mode="direct"),
    _tool("forget", "Remove memory entries matching a phrase, when something you recorded turns out wrong or stale.", {"text": {"type": "string"}}, ["text"], forget, mode="direct"),
    _tool("rename_tag", "Rename an inline #tag everywhere it appears, in one operation. This is how you fix duplicate tags found by analyze_tags — never rewrite notes one by one for a tag merge. Set dry_run to preview which notes would change. Requires confirmation.", {"from_tag": {"type": "string"}, "to_tag": {"type": "string"}, "dry_run": {"type": "boolean"}}, ["from_tag", "to_tag"], rename_tag, mode="confirmed"),
    _tool("propose_edits", "Stage spelling, grammar and clarity fixes for a note so the user can accept or dismiss each one in the app's Review panel. THIS is how you answer \"check my spelling/grammar\" — never rewrite the note with edit_note or patch_note for proofreading. Quote `before` exactly as it appears, keeping it short but unique; if the same text occurs more than once, set `occurrence` (0-based). `after` is the replacement, or empty to delete.", {"path": {"type": "string"}, "suggestions": {"type": "array", "items": {"type": "object", "properties": {"category": {"type": "string", "enum": list(CATEGORIES)}, "before": {"type": "string", "description": "Exact text from the note to replace"}, "after": {"type": "string", "description": "Replacement text; empty string deletes"}, "reason": {"type": "string", "description": "One short phrase, e.g. 'subject-verb agreement'"}, "occurrence": {"type": "integer", "description": "0-based, only when `before` is not unique"}}, "required": ["category", "before", "after"]}}}, ["path", "suggestions"], propose_edits, mode="direct"),
    _tool("patch_note", "Make targeted find/replace edits to a note without rewriting the whole file. PREFER THIS over edit_note for small changes and for any long note — edit_note must re-emit the entire file, which does not fit for large notes. Each old_string must appear exactly once unless replace_all is set.", {"path": {"type": "string"}, "base_revision": REVISION, "edits": {"type": "array", "items": {"type": "object", "properties": {"old_string": {"type": "string"}, "new_string": {"type": "string"}, "replace_all": {"type": "boolean"}}, "required": ["old_string", "new_string"]}}}, ["path", "base_revision", "edits"], patch_note, mode="direct"),
    _tool("find_unlinked_mentions", "Find places where one note's title is written in another note's prose without a [[wikilink]]. Use this to connect an unlinked vault; pass note_path to check a single note.", {"note_path": {"type": "string"}, "limit": {"type": "integer"}}, [], find_unlinked_mentions),
    _tool("ask_vault", "Answer a question from the notebook: retrieve the most relevant notes (semantic + keyword) so you can answer and cite their paths. Use this for 'what did I conclude about X', 'what do my notes say about Y'.", {"query": {"type": "string"}, "k": {"type": "integer", "description": "How many source notes to retrieve. Default 6."}}, ["query"], ask_vault),
    _tool("summarize_timespan", "Read what happened between two dates — notes created and touched, tasks completed, and events — for a weekly or monthly review. Read-only.", {"start": DATE, "end": DATE}, ["start", "end"], summarize_timespan),
    _tool("set_note_followup", "Resurface a note later: set a reminder linked to it ('remind me about this in two weeks'). when is a date/date-time, or use in_days.", {"path": {"type": "string"}, "when": {"type": "string", "description": "YYYY-MM-DD or YYYY-MM-DDTHH:MM."}, "in_days": {"type": "integer", "description": "Days from today, if `when` is omitted."}}, ["path"], set_note_followup, mode="direct"),
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
    _tool("get_outline", "Read a note's heading map, word count, and length — to navigate a long note or jump to a section without pulling the whole body.", {"path": {"type": "string"}}, ["path"], get_outline),
    _tool("move_section", "Move a ## section (with its subsections) out of one note and into another. Read the source first for its base_revision.", {"path": {"type": "string"}, "heading": {"type": "string"}, "target_path": {"type": "string"}, "base_revision": REVISION}, ["path", "heading", "target_path", "base_revision"], move_section, mode="direct"),
    _tool("convert_bullets_to_tasks", "Turn plain '- item' bullets into '- [ ] item' checkboxes, optionally scoped to one ## heading section. Skips lines that are already tasks.", {"path": {"type": "string"}, "heading": {"type": "string", "description": "Limit to this section; omit to convert bullets in the whole note."}, "base_revision": REVISION}, ["path", "base_revision"], convert_bullets_to_tasks, mode="direct"),
    _tool("save_note_as_template", "Save an existing note as a reusable template (under templates/), stripping managed frontmatter. Then create_note_from_template can instantiate it.", {"path": {"type": "string"}, "name": {"type": "string", "description": "Template name. Defaults to the note's name."}}, ["path"], save_note_as_template, mode="direct"),
    _tool("complete_task", "Check off (or reopen) a task by describing it — 'check off the milk task' — instead of needing its id. Matches the best task by text (exact beats partial, open beats done); pass path to narrow to one board.", {"text": {"type": "string"}, "path": {"type": "string"}}, ["text"], complete_task, mode="direct"),
    _tool("reschedule_task", "Move a task's due date, addressing the task by its text — 'push the milk task to tomorrow'. Resolve the date to YYYY-MM-DD yourself.", {"text": {"type": "string"}, "due": DATE, "path": {"type": "string"}}, ["text", "due"], reschedule_task, mode="direct"),
    _tool("add_tasks", "Create several tasks at once (one step, one commit) — capturing a list of todos. Omit path to use the current folder's board or the inbox.", {"tasks": {"type": "array", "items": {"type": "object", "properties": {"text": {"type": "string"}, "due": DATE}, "required": ["text"]}}, "path": {"type": "string"}}, ["tasks"], add_tasks, mode="direct"),
    _tool("reorder_sidebar", "Arrange notes or folders in the sidebar. Pass the items you want at the top, in order; they splice to the front and everything else keeps its place. kind is 'notes' (note paths) or 'folders' (folder paths).", {"kind": {"type": "string", "enum": ["notes", "folders"]}, "order": {"type": "array", "items": {"type": "string"}}}, ["kind", "order"], reorder_sidebar, mode="direct"),
    _tool("archive_note", "Move a note into archive/ in one step, out of the way but recoverable.", {"path": {"type": "string"}}, ["path"], archive_note, mode="direct"),
    _tool("move_notes", "Move several notes into a folder in one step. Use an empty folder for the vault root.", {"paths": {"type": "array", "items": {"type": "string"}}, "folder": {"type": "string"}}, ["paths", "folder"], move_notes, mode="direct"),
    _tool("reschedule_event", "Move a calendar event or reminder to a new time by naming it — 'push my dentist appointment to Friday'. Give the new start (YYYY-MM-DD or YYYY-MM-DDTHH:MM); duration is kept unless you give an end. Matches the best/nearest event by title.", {"title": {"type": "string"}, "start": {"type": "string"}, "end": {"type": "string"}}, ["title", "start"], reschedule_event, mode="direct"),
    _tool("edit_media_profile", "Set the media hub's name, location, and/or favorites (up to 4 tracked media items, by note path). Avatar/banner images are changed in the app, not here.", {"name": {"type": "string"}, "location": {"type": "string"}, "favorites": {"type": "array", "items": {"type": "string"}, "description": "Note paths of tracked media items to feature."}}, [], edit_media_profile, mode="direct"),
    _tool("bulk_log_media", "Track several media items at once — 'log these five books'. Each item needs a title and media_type (book/movie/tv/game); optional status (planned/active/finished/dropped), creator, year, total, rating. Logged bare; add covers later with set_media_art. Already-tracked titles are skipped.", {"items": {"type": "array", "items": {"type": "object", "properties": {"title": {"type": "string"}, "media_type": {"type": "string", "enum": ["book", "movie", "tv", "game"]}, "status": {"type": "string"}, "creator": {"type": "string"}, "year": {"type": "integer"}, "total": {"type": "integer"}, "rating": {"type": "integer"}}, "required": ["title", "media_type"]}}}, ["items"], bulk_log_media, mode="direct"),
    _tool("link_notes", "Create a two-way [[wikilink]] between two notes, so each references the other. Use for 'connect these two notes'.", {"path_a": {"type": "string"}, "path_b": {"type": "string"}}, ["path_a", "path_b"], link_notes, mode="direct"),
    _tool("set_omnia_housekeeping", "Turn OMNIA's nightly conversation-list housekeeping on or off. (Full-auto mode is Settings-only and cannot be changed here.)", {"enabled": {"type": "boolean"}}, ["enabled"], set_omnia_housekeeping, mode="direct"),
    _tool("edit_selection", "Replace the text the user has selected in the editor with new_string — this is how you act on 'rewrite this' / 'fix this'. With no selection it inserts at the cursor. Acts on the exact live selection; if the selection is stale or came from outside the editor, pass old_string (the exact text to replace) as a fallback.", {"new_string": {"type": "string"}, "old_string": {"type": "string", "description": "Fallback: the exact text to replace when there's no live selection."}, "path": {"type": "string", "description": "Fallback note path for old_string."}}, ["new_string"], edit_selection, mode="direct"),
    _tool("format_selection", "Format the user's current selection: style = heading | quote | callout | bold | italic | code | bullet | task (for 'make this a heading', 'quote this', 'turn these lines into a checklist'). Give level 1-6 for heading. Falls back to old_string when the selection is stale.", {"style": {"type": "string", "enum": ["heading", "quote", "callout", "bold", "italic", "code", "bullet", "task"]}, "level": {"type": "integer", "description": "Heading level 1-6 (default 2)."}, "old_string": {"type": "string"}, "path": {"type": "string"}}, ["style"], format_selection, mode="direct"),
    _tool("extract_to_note", "Cut the user's selection out into a new note and leave a [[wikilink]] in its place — 'pull this into its own note'. Title defaults to the selection's first line. For whole ## sections prefer move_section.", {"title": {"type": "string"}, "old_string": {"type": "string"}, "path": {"type": "string"}}, [], extract_to_note, mode="direct"),
    _tool("open", "Open a note or a view in the app for the user — 'pull up my groceries note', 'go to my calendar'. Give `note` (a path or name) OR `view` (calendar, tasks, media, search, graph, trash, pending, settings). Navigation only; changes nothing.", {"note": {"type": "string", "description": "Note path or name to open."}, "view": {"type": "string", "enum": ["calendar", "tasks", "media", "search", "graph", "trash", "pending", "settings", "omnia"]}}, [], open_in_app, mode="direct"),
    _tool("close_tabs", "Close open editor tabs — pass `match` to close tabs whose name contains it, or `others: true` to close every tab except the current one.", {"match": {"type": "string"}, "others": {"type": "boolean"}}, [], close_tabs, mode="direct"),
    _tool("related_notes", "Find the notes most similar to a given note — 'what else did I write about this?'. Semantic neighbours, not explicit links.", {"path": {"type": "string"}, "k": {"type": "integer", "description": "How many to return. Default 8."}}, ["path"], related_notes),
    _tool("get_forward_links", "List the notes a given note links OUT to (the complement of get_backlinks), flagging any whose target doesn't exist.", {"path": {"type": "string"}}, ["path"], get_forward_links),
    _tool("find_notes", "Vault hygiene sweep. filter = orphan (nothing links to or from it) | stale (modified before a date — pass `before`) | empty | large. For 'find my orphan notes', 'notes I haven't touched since June'.", {"filter": {"type": "string", "enum": ["orphan", "stale", "empty", "large"]}, "before": DATE, "min_chars": {"type": "integer"}, "limit": {"type": "integer"}}, ["filter"], find_notes),
    _tool("find_broken_links", "Find [[wikilinks]] that point to notes which don't exist — dead links to fix.", {"limit": {"type": "integer"}}, [], find_broken_links),
    _tool("vault_overview", "A quick snapshot of the whole vault: how many notes, open tasks, tags, task boards, media by status, and notes per folder. The 'what've I got?' read.", {}, [], vault_overview),
    _tool("summarize_notes", "Gather the contents of a folder or a set of notes (capped) so you can summarize them and cite the paths. For a single note, use read_notes.", {"folder": {"type": "string"}, "paths": {"type": "array", "items": {"type": "string"}}}, [], summarize_notes),
    _tool("note_changes", "Show what changed in a note since a date — a diff against how it looked then. For 'what did I edit in this note this week?'.", {"path": {"type": "string"}, "since": DATE}, ["path"], note_changes),
    _tool("restore_note_to", "Restore a note to how it was at or before a date — 'put this back to how it was Monday'. Undoable.", {"path": {"type": "string"}, "date": DATE}, ["path", "date"], restore_note_to, mode="direct"),
    _tool("find_free_time", "Find open time slots between calendar events in a date range — 'am I free Thursday afternoon', 'find a 1h slot this week'. Honours working hours (day_start_hour/day_end_hour, default 9-18) and a minimum slot length (min_minutes, default 30).", {"start": DATE, "end": DATE, "day_start_hour": {"type": "integer"}, "day_end_hour": {"type": "integer"}, "min_minutes": {"type": "integer"}}, ["start", "end"], find_free_time),
    _tool("week_agenda", "Lay out events and due tasks day by day across a date range — 'what's my week look like'.", {"start": DATE, "end": DATE}, ["start", "end"], week_agenda),
    _tool("list_media", "List tracked media by status — 'what am I currently watching' (status=active), 'my watchlist' (status=watchlist), 'what have I finished'. Optional media_type book/movie/tv/game.", {"status": {"type": "string", "description": "active | planned/watchlist | finished | dropped"}, "media_type": {"type": "string", "enum": ["book", "movie", "tv", "game"]}}, [], list_media),
    _tool("media_diary", "Recent media activity — what the user has been watching/reading/playing lately, newest first.", {"limit": {"type": "integer"}}, [], media_diary),
    _tool("update_media_by_title", "Update a tracked media item by its TITLE (no path needed) — set status (active/planned/watchlist/finished/dropped), log progress ('S01E05' or a page number), or set a rating (1-10). For 'I'm on episode 5 of X', 'add X to my watchlist', 'mark X finished'.", {"title": {"type": "string"}, "status": {"type": "string"}, "progress": {"type": "string", "description": "A page number or SxxEyy to log."}, "rating": {"type": "integer"}}, ["title"], update_media_by_title, mode="direct"),
    _tool("format_table", "Align, sort, or add a row to a markdown table in a note (scoped to a heading if given). operation = align (tidy columns) | sort (by column index) | add_row (give `row`, an array of cell values).", {"path": {"type": "string"}, "base_revision": REVISION, "operation": {"type": "string", "enum": ["align", "sort", "add_row"]}, "column": {"type": "integer"}, "row": {"type": "array", "items": {"type": "string"}}, "heading": {"type": "string"}}, ["path", "base_revision"], format_table, mode="direct"),
    _tool("sort_list", "Sort a bullet list in a note alphabetically (scoped to a heading if given). Set dedupe to drop duplicate lines, descending to reverse.", {"path": {"type": "string"}, "base_revision": REVISION, "heading": {"type": "string"}, "dedupe": {"type": "boolean"}, "descending": {"type": "boolean"}}, ["path", "base_revision"], sort_list, mode="direct"),
    _tool("insert_toc", "Insert a table of contents (built from the note's ## headings) near the top of a note.", {"path": {"type": "string"}, "base_revision": REVISION}, ["path", "base_revision"], insert_toc, mode="direct"),
    _tool("embed_attachment", "Insert an image/attachment reference into a note — resolve the attachment by name, then embed it (at the end, or under a heading).", {"path": {"type": "string"}, "attachment": {"type": "string", "description": "Attachment file name (or part of it)."}, "alt": {"type": "string"}, "heading": {"type": "string"}}, ["path", "attachment"], embed_attachment, mode="direct"),
    _tool("extract_action_items", "Pull the bullet/checkbox action items out of a note (or one ## section) onto a task board — 'turn the todos in this note into tasks on my board'. By default the lines are removed from the note (remove=false to keep them).", {"path": {"type": "string"}, "board": {"type": "string", "description": "Destination board note path."}, "base_revision": REVISION, "heading": {"type": "string"}, "remove": {"type": "boolean"}}, ["path", "board", "base_revision"], extract_action_items, mode="direct"),
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
]

OVERRIDDEN_TOOL_NAMES = {
    "create_note",
    "edit_note",
    "move_note",
    "delete_note",
    "edit_frontmatter",
}
