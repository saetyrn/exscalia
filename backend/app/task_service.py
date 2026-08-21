"""Task lifecycle helpers.

Markdown remains authoritative. SQLite is only a query cache and task IDs
inside adjacent comments let UI operations survive line movement.
"""

from __future__ import annotations

import json
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .index import parse
from .index.db import content_hash
from .vault import fm
from .vault.internal import atomic_json, iso_utc, read_json
from .vault.notes import NoteError, NoteStore

MIGRATION_VERSION = 2


def _publish(state, path: str, content: str) -> None:
    """Tell open editors the note under a task changed.

    Task edits rewrite note files, so an editor holding the note needs the
    same broadcast a plain save gets — it merges the new base in and its next
    autosave carries a fresh revision instead of forking a conflict copy."""
    live = getattr(state, "live", None)
    if live is not None:
        live.note_changed(path, content_hash(content))


def migrate_task_metadata(store: NoteStore, vault_dir: Path) -> list[str]:
    marker = vault_dir / NoteStore.INTERNAL_DIR / "migrations" / "tasks-v2.json"
    current = read_json(marker, {})
    if isinstance(current, dict) and current.get("version") == MIGRATION_VERSION:
        return []

    migrated_at = iso_utc()
    changed: list[str] = []
    for info in store.list_notes():
        content = store.read(info.path)
        normalized, added = parse.normalize_task_metadata(content, now=migrated_at)
        if added:
            store.resolve(info.path).write_text(normalized, encoding="utf-8")
            changed.append(info.path)

    atomic_json(
        marker,
        {
            "version": MIGRATION_VERSION,
            "migrated_at": migrated_at,
            "notes_changed": len(changed),
        },
    )
    return changed


def _clean_description(value: str | None) -> str | None:
    """Descriptions live in the task's metadata comment, which is one line —
    collapse newlines so the marker can never be split."""
    if value is None:
        return None
    cleaned = " ".join(value.split())
    return cleaned[:2000] or None


def _clean_text(text: str) -> str:
    value = " ".join(text.replace("\r", "\n").splitlines()).strip()
    value = parse.DUE_RE.sub("", value).strip()
    if not value:
        raise NoteError(400, "Task text cannot be empty")
    return value


def _status_char(status: str) -> str:
    try:
        return {"todo": " ", "in_progress": "/", "done": "x"}[status]
    except KeyError:
        raise NoteError(400, f"Unknown task status: {status}")


def _validate_due(due: str | None) -> str | None:
    if due in (None, ""):
        return None
    try:
        datetime.strptime(due, "%Y-%m-%d")
    except ValueError:
        raise NoteError(400, "Task due date must be YYYY-MM-DD")
    return due


_TASK_FREQUENCIES = {"daily", "weekly", "monthly", "yearly"}


def clean_task_recurrence(value: dict | None) -> dict:
    """Validate a task recurrence down to the subset tasks support:
    frequency + interval, with an optional end date. Kept simpler than event
    recurrence on purpose — a recurring chore doesn't need weekday masks."""
    if not value or not isinstance(value, dict):
        raise NoteError(400, "Give a recurrence with a frequency")
    frequency = str(value.get("frequency") or "").lower()
    if frequency not in _TASK_FREQUENCIES:
        raise NoteError(400, "Recurrence frequency must be daily, weekly, monthly, or yearly")
    interval = int(value.get("interval") or 1)
    if not 1 <= interval <= 365:
        raise NoteError(400, "Recurrence interval must be 1-365")
    cleaned = {"frequency": frequency, "interval": interval}
    until = value.get("until")
    if until:
        try:
            date.fromisoformat(str(until))
        except ValueError:
            raise NoteError(400, "Recurrence until must be YYYY-MM-DD")
        cleaned["until"] = str(until)
    return cleaned


def _next_occurrence(due: str, recurrence: dict) -> str | None:
    """The date strictly after ``due`` on this recurrence, or None once it has
    passed the ``until`` bound."""
    try:
        base = date.fromisoformat(due)
    except ValueError:
        return None
    frequency = recurrence.get("frequency")
    interval = int(recurrence.get("interval") or 1)
    if frequency == "daily":
        nxt = base + timedelta(days=interval)
    elif frequency == "weekly":
        nxt = base + timedelta(weeks=interval)
    elif frequency == "monthly":
        index = base.year * 12 + base.month - 1 + interval
        year, month_zero = divmod(index, 12)
        month = month_zero + 1
        nxt = date(year, month, min(base.day, monthrange(year, month)[1]))
    elif frequency == "yearly":
        year = base.year + interval
        try:
            nxt = date(year, base.month, base.day)
        except ValueError:  # Feb 29 in a non-leap year
            nxt = date(year, base.month, 28)
    else:
        return None
    until = recurrence.get("until")
    if until and nxt > date.fromisoformat(str(until)):
        return None
    return nxt.isoformat()


def _find_task(content: str, task_id: str) -> parse.Task:
    meta, body = fm.parse(content)
    del meta
    offset = parse.frontmatter_line_count(content, body)
    for task in parse.extract_tasks(body, offset):
        if task.id == task_id:
            return task
    raise NoteError(409, "Task moved or changed; refresh and try again")


def public_task(row: dict) -> dict:
    value = dict(row)
    value["done"] = value.get("status") == "done"
    value["due_visible"] = bool(value.get("due_visible"))
    return value


async def _fetch_task(db, task_id: str):
    cur = await db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
    return await cur.fetchone()


async def create_task(
    state,
    *,
    text: str,
    path: str = "Tasks Inbox.md",
    status: str = "todo",
    due: str | None = None,
    due_visible: bool = False,
    description: str | None = None,
    recurrence: dict | None = None,
) -> dict:
    text = _clean_text(text)
    char = _status_char(status)
    due = _validate_due(due)
    now = iso_utc()
    meta = parse._metadata(char, text, now=now)
    meta["due"] = due
    meta["due_visible"] = bool(due_visible and due)
    meta["description"] = _clean_description(description)
    if recurrence:
        meta["recurrence"] = recurrence
    visible_due = f" 📅 {due}" if due and due_visible else ""
    block = f"{parse.task_meta_line(meta)}\n- [{char}] {text}{visible_due}\n"

    if state.store.exists(path):
        content = state.store.read(path)
        if content and not content.endswith("\n"):
            content += "\n"
        content += "\n" + block
        normalized = state.store.write(path, content)
    else:
        title = Path(path).stem
        normalized = state.store.create(
            path,
            f"---\ntitle: \"{title}\"\n---\n\n# Tasks\n\n{block}",
        )
    await state.indexer.index_note(path, normalized)
    state.autocommit.touch()
    _publish(state, path, normalized)
    row = await _fetch_task(state.db, meta["id"])
    return public_task(dict(row))


async def update_task(
    state,
    task_id: str,
    *,
    text: str | None = None,
    status: str | None = None,
    due: str | None | object = ...,
    due_visible: bool | None = None,
    description: str | None | object = ...,
) -> dict:
    row = await _fetch_task(state.db, task_id)
    if row is None:
        raise NoteError(404, "Task not found")
    path = row["path"]
    content = state.store.read(path)
    task = _find_task(content, task_id)
    if task.meta_line is None:
        raise NoteError(409, "Task metadata is missing; refresh the note to repair it")

    lines = content.splitlines()
    trailing = "\n" if content.endswith("\n") else ""
    meta_match = parse.TASK_META_RE.match(lines[task.meta_line - 1])
    line_match = parse.TASK_RE.match(lines[task.line - 1])
    if not meta_match or not line_match:
        raise NoteError(409, "Task changed; refresh and try again")
    try:
        metadata = json.loads(meta_match.group(1))
    except json.JSONDecodeError:
        raise NoteError(409, "Task metadata is invalid")

    next_status = status or task.status
    char = _status_char(next_status)
    next_text = _clean_text(text) if text is not None else task.text
    next_due = task.due if due is ... else _validate_due(due)
    next_due_visible = task.due_visible if due_visible is None else bool(due_visible)
    next_due_visible = bool(next_due and next_due_visible)
    now = iso_utc()

    if next_status == "in_progress" and not metadata.get("started_at"):
        metadata["started_at"] = now
    if next_status == "done" and not metadata.get("completed_at"):
        metadata["completed_at"] = now
    if task.status == "done" and next_status != "done" and not metadata.get(
        "reopened_at"
    ):
        metadata["reopened_at"] = now
    metadata.update(
        {
            "id": task_id,
            "created_at": metadata.get("created_at") or now,
            "due": next_due,
            "due_visible": next_due_visible,
            "description": (
                metadata.get("description")
                if description is ...
                else _clean_description(description)  # type: ignore[arg-type]
            ),
        }
    )

    visible_due = f" 📅 {next_due}" if next_due_visible else ""
    lines[task.meta_line - 1] = parse.task_meta_line(metadata)
    lines[task.line - 1] = (
        line_match.group(1)
        + char
        + line_match.group(3)
        + next_text
        + visible_due
    )
    normalized = state.store.write(path, "\n".join(lines) + trailing)
    await state.indexer.index_note(path, normalized)
    state.autocommit.touch()
    _publish(state, path, normalized)
    updated = await _fetch_task(state.db, task_id)
    result = public_task(dict(updated))

    # A recurring task spawns its next occurrence the moment it is completed,
    # so the chore reappears with a fresh due date instead of piling up. Only
    # on a genuine todo/in-progress -> done transition, never on a re-save of an
    # already-done task.
    recurrence = metadata.get("recurrence")
    if (
        recurrence
        and task.status != "done"
        and next_status == "done"
        and metadata.get("due")
    ):
        upcoming = _next_occurrence(str(metadata["due"]), recurrence)
        if upcoming:
            await create_task(
                state,
                text=next_text,
                path=path,
                due=upcoming,
                due_visible=next_due_visible,
                description=metadata.get("description"),
                recurrence=recurrence,
            )
    return result


MAX_REORDER = 500


async def reorder_tasks(state, task_ids: list[str]) -> list[dict]:
    """Pin a column's order by writing each task's rank into its metadata.

    ``task_ids`` is the column's complete order after the drag, so ranks are
    assigned densely from zero and the whole column becomes hand-ordered at
    once. A column nobody has dragged keeps every rank at None and falls back
    to the board's own sort, which is why this writes all of them and not just
    the card that moved.

    A column on the universal board can span notes, so edits are grouped per
    note. Every note is parsed and validated before any of them is written —
    a stale id must not leave half a column renumbered.
    """
    if not task_ids:
        raise NoteError(400, "No tasks to reorder")
    if len(set(task_ids)) != len(task_ids):
        raise NoteError(400, "The same task appears twice in the new order")
    if len(task_ids) > MAX_REORDER:
        raise NoteError(400, f"Cannot reorder more than {MAX_REORDER} tasks at once")

    rank = {task_id: float(index) for index, task_id in enumerate(task_ids)}
    by_path: dict[str, list[str]] = {}
    for task_id in task_ids:
        row = await _fetch_task(state.db, task_id)
        if row is None:
            raise NoteError(404, "Task not found")
        by_path.setdefault(row["path"], []).append(task_id)

    pending: list[tuple[str, str]] = []
    for path, ids in by_path.items():
        content = state.store.read(path)
        _meta, body = fm.parse(content)
        offset = parse.frontmatter_line_count(content, body)
        found = {task.id: task for task in parse.extract_tasks(body, offset)}
        lines = content.splitlines()
        trailing = "\n" if content.endswith("\n") else ""
        for task_id in ids:
            task = found.get(task_id)
            if task is None or task.meta_line is None:
                raise NoteError(409, "Task moved or changed; refresh and try again")
            meta_match = parse.TASK_META_RE.match(lines[task.meta_line - 1])
            if not meta_match:
                raise NoteError(409, "Task changed; refresh and try again")
            try:
                metadata = json.loads(meta_match.group(1))
            except json.JSONDecodeError:
                raise NoteError(409, "Task metadata is invalid")
            metadata["sort"] = rank[task_id]
            lines[task.meta_line - 1] = parse.task_meta_line(metadata)
        pending.append((path, "\n".join(lines) + trailing))

    for path, content in pending:
        normalized = state.store.write(path, content)
        await state.indexer.index_note(path, normalized)
        _publish(state, path, normalized)
    state.autocommit.touch()

    ordered = []
    for task_id in task_ids:
        row = await _fetch_task(state.db, task_id)
        if row is not None:
            ordered.append(public_task(dict(row)))
    return ordered


async def delete_task(state, task_id: str) -> dict:
    row = await _fetch_task(state.db, task_id)
    if row is None:
        raise NoteError(404, "Task not found")
    path = row["path"]
    content = state.store.read(path)
    task = _find_task(content, task_id)
    lines = content.splitlines()
    trailing = "\n" if content.endswith("\n") else ""
    indices = {task.line - 1}
    if task.meta_line is not None:
        indices.add(task.meta_line - 1)
    normalized = state.store.write(
        path,
        "\n".join(line for i, line in enumerate(lines) if i not in indices) + trailing,
    )
    await state.indexer.index_note(path, normalized)
    state.autocommit.touch()
    _publish(state, path, normalized)
    return {"ok": True, "id": task_id, "path": path}


async def set_task_reminder(state, task_id: str, remind_at: str | None) -> dict:
    """Set (or clear, with None) the time a task's reminder should fire.

    The trigger lives in the task's metadata comment, so it stays in markdown,
    survives a reindex, and is git-undoable like every other task edit. The
    reminder ticker reads it from the indexed `reminder_at` column."""
    if remind_at:
        try:
            datetime.fromisoformat(remind_at)
        except ValueError:
            raise NoteError(400, "Reminder time must be ISO YYYY-MM-DD or YYYY-MM-DDTHH:MM")
    row = await _fetch_task(state.db, task_id)
    if row is None:
        raise NoteError(404, "Task not found")
    path = row["path"]
    content = state.store.read(path)
    task = _find_task(content, task_id)
    if task.meta_line is None:
        raise NoteError(409, "Task metadata is missing; refresh the note to repair it")
    lines = content.splitlines()
    trailing = "\n" if content.endswith("\n") else ""
    meta_match = parse.TASK_META_RE.match(lines[task.meta_line - 1])
    if not meta_match:
        raise NoteError(409, "Task changed; refresh and try again")
    try:
        metadata = json.loads(meta_match.group(1))
    except json.JSONDecodeError:
        raise NoteError(409, "Task metadata is invalid")
    if remind_at:
        metadata["reminder_at"] = remind_at
    else:
        metadata.pop("reminder_at", None)
    lines[task.meta_line - 1] = parse.task_meta_line(metadata)
    normalized = state.store.write(path, "\n".join(lines) + trailing)
    await state.indexer.index_note(path, normalized)
    state.autocommit.touch()
    _publish(state, path, normalized)
    updated = await _fetch_task(state.db, task_id)
    return public_task(dict(updated))


async def move_task(state, task_id: str, target_path: str) -> dict:
    """Relocate a task to another note/board, keeping its id and history.

    Markdown stays authoritative, so this is a remove-from-source plus
    append-to-target rewrite of two notes. The metadata comment carries the
    stable id and lifecycle timestamps across intact; only the column rank is
    dropped, because a rank is meaningful only inside the column it was set in —
    the task rejoins its new home at the natural (due/append) order.
    """
    target = (target_path or "").strip()
    if not target:
        raise NoteError(400, "Pick a note to move the task into")
    row = await _fetch_task(state.db, task_id)
    if row is None:
        raise NoteError(404, "Task not found")
    source = row["path"]
    if target == source:
        return public_task(dict(row))
    if not state.store.exists(target):
        raise NoteError(404, "Target note not found")

    content = state.store.read(source)
    task = _find_task(content, task_id)
    if task.meta_line is None:
        raise NoteError(409, "Task metadata is missing; refresh the note to repair it")
    lines = content.splitlines()
    meta_match = parse.TASK_META_RE.match(lines[task.meta_line - 1])
    line_match = parse.TASK_RE.match(lines[task.line - 1])
    if not meta_match or not line_match:
        raise NoteError(409, "Task changed; refresh and try again")
    try:
        metadata = json.loads(meta_match.group(1))
    except json.JSONDecodeError:
        raise NoteError(409, "Task metadata is invalid")
    metadata.pop("sort", None)
    task_line = lines[task.line - 1]

    # Remove the task and its metadata comment from the source note.
    trailing = "\n" if content.endswith("\n") else ""
    drop = {task.line - 1, task.meta_line - 1}
    source_content = (
        "\n".join(line for i, line in enumerate(lines) if i not in drop) + trailing
    )
    source_normalized = state.store.write(source, source_content)

    # Append it to the target note, matching create_task's block shape.
    block = f"{parse.task_meta_line(metadata)}\n{task_line}\n"
    target_content = state.store.read(target)
    if target_content and not target_content.endswith("\n"):
        target_content += "\n"
    target_content += "\n" + block
    target_normalized = state.store.write(target, target_content)

    await state.indexer.index_note(source, source_normalized)
    await state.indexer.index_note(target, target_normalized)
    state.autocommit.touch()
    _publish(state, source, source_normalized)
    _publish(state, target, target_normalized)
    moved = await _fetch_task(state.db, task_id)
    return public_task(dict(moved))
