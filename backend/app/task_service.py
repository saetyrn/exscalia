"""Task lifecycle helpers.

Markdown remains authoritative. SQLite is only a query cache and task IDs
inside adjacent comments let UI operations survive line movement.
"""

from __future__ import annotations

import json
from calendar import monthrange
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import alerts
from .index import parse
from .index.db import content_hash
from .vault import fm
from .vault.internal import atomic_json, iso_utc, read_json
from .vault.notes import NoteError, NoteStore

# 3: OMNIA note tools wrote bare `- [ ]` lines until they normalized too.
MIGRATION_VERSION = 3


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
            store.write_raw(info.path, normalized)
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


def parse_user_date(value: object, what: str = "Date") -> str:
    """A calendar date from the API or OMNIA, in its one stored form.

    Dues are compared as text everywhere (overdue checks, `due < ?`, ORDER BY,
    the visible 📅 marker's pattern), so only YYYY-MM-DD may ever be stored or
    bound. An unpadded '2026-9-5' and fromisoformat's compact and week forms
    ('20260905', '2026-W36-1') are read, then always written back canonical."""
    text = str(value).strip()
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d").date()
        except ValueError:
            raise NoteError(400, f"{what} must be YYYY-MM-DD")
    return parsed.isoformat()


def _validate_due(due: str | None) -> str | None:
    if due in (None, ""):
        return None
    return parse_user_date(due, "Task due date")


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
        cleaned["until"] = parse_user_date(until, "Recurrence until")
    return cleaned


def _anchor_recurrence(recurrence: dict, due: str | None) -> dict:
    """Pin a monthly or yearly recurrence to the day (and, yearly, the month)
    of the due date it runs from, unless it already carries one. Occurrences
    are computed from the anchor, not from the previous due, so a Jan 31 chore
    clamped to Feb 28 returns to the 31st in March instead of staying on the
    28th for good."""
    frequency = recurrence.get("frequency")
    if frequency not in ("monthly", "yearly") or not due:
        return recurrence
    try:
        base = date.fromisoformat(due)
    except ValueError:
        return recurrence
    anchored = dict(recurrence)
    anchored.setdefault("anchor_day", base.day)
    if frequency == "yearly":
        anchored.setdefault("anchor_month", base.month)
    return anchored


def _anchor_value(value: object, fallback: int, top: int) -> int:
    """A hand-edited anchor that is not a day/month number falls back to the
    due date's own."""
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    return number if 1 <= number <= top else fallback


def _next_occurrence(due: str, recurrence: dict, *, today: date) -> str | None:
    """The first date on this recurrence after both ``due`` and ``today``, or
    None once it has passed the ``until`` bound.

    Rolling past today means completing an overdue chore schedules the next
    one ahead instead of another already-overdue copy. Monthly and yearly
    steps land on the anchor day, clamped to the month's length per month (so
    a Feb 29 anchor falls back to the 28th in a common year and returns in the
    next leap year); the clamped day is never written back as the anchor."""
    try:
        base = date.fromisoformat(due)
        interval = int(recurrence.get("interval") or 1)
    except (TypeError, ValueError):
        return None
    if interval < 1:
        return None
    frequency = recurrence.get("frequency")
    anchor_day = _anchor_value(recurrence.get("anchor_day"), base.day, 31)
    anchor_month = _anchor_value(recurrence.get("anchor_month"), base.month, 12)

    def step(current: date) -> date:
        if frequency == "monthly":
            index = current.year * 12 + current.month - 1 + interval
            year, month_zero = divmod(index, 12)
            month = month_zero + 1
        else:
            year, month = current.year + interval, anchor_month
        return date(year, month, min(anchor_day, monthrange(year, month)[1]))

    try:
        if frequency in ("daily", "weekly"):
            gap = timedelta(days=interval * (7 if frequency == "weekly" else 1))
            nxt = base + gap
            if nxt <= today:
                nxt += gap * ((today - nxt).days // gap.days + 1)
        elif frequency in ("monthly", "yearly"):
            nxt = step(base)
            while nxt <= today:
                nxt = step(nxt)
        else:
            return None
    except (OverflowError, ValueError):  # stepped past year 9999
        return None
    until = recurrence.get("until")
    if until:
        try:
            if nxt > date.fromisoformat(str(until)):
                return None
        except ValueError:
            pass  # a hand-mangled bound does not end the series
    return nxt.isoformat()


def _local_today(state) -> date:
    """Today in the user's timezone, which is where "overdue" is decided."""
    try:
        tz = ZoneInfo(getattr(state, "timezone", "UTC"))
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo("UTC")
    return datetime.now(tz).date()


def _validate_reminder(remind_at: str | None) -> str | None:
    if not remind_at:
        return None
    try:
        datetime.fromisoformat(remind_at)
    except ValueError:
        raise NoteError(400, "Reminder time must be ISO YYYY-MM-DD or YYYY-MM-DDTHH:MM")
    return remind_at


def _shift_reminder(reminder_at: object, days: int) -> str | None:
    """``reminder_at`` moved by whole days at the same time of day, or None
    when there is no readable reminder to move."""
    if not reminder_at:
        return None
    try:
        moment = datetime.fromisoformat(str(reminder_at)) + timedelta(days=days)
    except (OverflowError, ValueError):
        return None
    return moment.isoformat(timespec="minutes")


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


def _task_block(
    char: str,
    text: str,
    *,
    now: str,
    due: str | None,
    due_visible: bool,
    description: str | None,
    recurrence: dict | None,
    reminder_at: str | None,
) -> tuple[dict, str]:
    """A new task's metadata and its markdown block (the metadata comment,
    then the task line), ready to append to a note."""
    meta = parse._metadata(char, text, now=now)
    meta["due"] = due
    meta["due_visible"] = bool(due_visible and due)
    meta["description"] = _clean_description(description)
    if recurrence:
        meta["recurrence"] = _anchor_recurrence(recurrence, due)
    if reminder_at:
        meta["reminder_at"] = reminder_at
    visible_due = f" 📅 {due}" if due and due_visible else ""
    return meta, f"{parse.task_meta_line(meta)}\n- [{char}] {text}{visible_due}\n"


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
    reminder_at: str | None = None,
) -> dict:
    text = _clean_text(text)
    char = _status_char(status)
    due = _validate_due(due)
    reminder_at = _validate_reminder(reminder_at)
    now = iso_utc()
    meta, block = _task_block(
        char,
        text,
        now=now,
        due=due,
        due_visible=due_visible,
        description=description,
        recurrence=recurrence,
        reminder_at=reminder_at,
    )

    if state.store.exists(path):
        content = state.store.read(path)
        if content and not content.endswith("\n"):
            content += "\n"
        content += "\n" + block
        normalized = state.store.write(path, content)
    else:
        # serialize, not an f-string: a quote or backslash in the filename
        # would make hand-built YAML invalid, and saving refuses that.
        normalized = state.store.create(
            path,
            fm.serialize({"title": Path(path).stem}, f"# Tasks\n\n{block}"),
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

    lines = parse.split_lines(content)
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
    # completed_at and reopened_at record the LATEST transition each way: a
    # task finished last week, reopened and finished again today counts as
    # done today. The original completion survives in first_completed_at.
    if next_status == "done" and (
        task.status != "done" or not metadata.get("completed_at")
    ):
        if metadata.get("completed_at") and not metadata.get("first_completed_at"):
            metadata["first_completed_at"] = metadata["completed_at"]
        metadata["completed_at"] = now
    if task.status == "done" and next_status != "done":
        metadata["reopened_at"] = now
    # A reminder belongs to the due date: moving the due by N days moves the
    # reminder by N days at the same time of day. Clearing the due leaves it.
    # One that lands in the past is settled below, never pushed late.
    moved_reminders: dict[str, str] = {}
    if task.due and next_due and next_due != task.due:
        try:
            moved = (date.fromisoformat(next_due) - date.fromisoformat(task.due)).days
        except ValueError:
            moved = 0
        shifted = _shift_reminder(metadata.get("reminder_at"), moved) if moved else None
        if shifted:
            metadata["reminder_at"] = shifted
            moved_reminders[task_id] = shifted
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

    # A recurring task spawns its next occurrence the moment it is completed,
    # so the chore reappears with a fresh due date instead of piling up. Only
    # on a genuine todo/in-progress -> done transition, never on a re-save of an
    # already-done task — and only once: completing it again after an Undo
    # finds its spawn still there (spawned_next) and leaves it alone. The spawn
    # is appended in the same write as the completion, so the two cannot come
    # apart.
    spawn_block = None
    recurrence = metadata.get("recurrence")
    if (
        isinstance(recurrence, dict)
        and recurrence
        and task.status != "done"
        and next_status == "done"
        and metadata.get("due")
    ):
        spawned = metadata.get("spawned_next")
        if not spawned or await _fetch_task(state.db, str(spawned)) is None:
            base_due = str(metadata["due"])
            recurrence = _anchor_recurrence(recurrence, base_due)
            upcoming = _next_occurrence(base_due, recurrence, today=_local_today(state))
            if upcoming:
                gap = (date.fromisoformat(upcoming) - date.fromisoformat(base_due)).days
                spawn_meta, spawn_block = _task_block(
                    " ",
                    next_text,
                    now=now,
                    due=upcoming,
                    due_visible=next_due_visible,
                    description=metadata.get("description"),
                    recurrence=recurrence,
                    reminder_at=_shift_reminder(metadata.get("reminder_at"), gap),
                )
                metadata["spawned_next"] = spawn_meta["id"]
                if spawn_meta.get("reminder_at"):
                    moved_reminders[spawn_meta["id"]] = spawn_meta["reminder_at"]

    visible_due = f" 📅 {next_due}" if next_due_visible else ""
    lines[task.meta_line - 1] = parse.task_meta_line(metadata)
    lines[task.line - 1] = (
        line_match.group(1)
        + char
        + line_match.group(3)
        + next_text
        + visible_due
    )
    content = "\n".join(lines) + trailing
    if spawn_block:
        if not content.endswith("\n"):
            content += "\n"
        content += "\n" + spawn_block
    # Before the write: a tick that finds the new reminder_at indexed must
    # also find it settled.
    if moved_reminders:
        alerts.settle_passed_task_reminders(state, moved_reminders)
    normalized = state.store.write(path, content)
    await state.indexer.index_note(path, normalized)
    state.autocommit.touch()
    _publish(state, path, normalized)
    updated = await _fetch_task(state.db, task_id)
    return public_task(dict(updated))


MAX_REORDER = 500


async def reorder_tasks(state, task_ids: list[str]) -> list[dict]:
    """Pin a column's order by writing each task's rank into its metadata.

    ``task_ids`` is the column's complete order after the drag, so ranks are
    assigned densely from zero and the whole column becomes hand-ordered at
    once. A column nobody has dragged keeps every rank at None and falls back
    to the board's own sort, which is why this ranks all of them and not just
    the card that moved.

    A column on the universal board can span notes, so edits are grouped per
    note. Every note is parsed and validated before any of them is written —
    a stale id must not leave half a column renumbered. Only notes whose ranks
    actually change are rewritten, and a rank is bookkeeping, not an edit, so
    the rewrite keeps the note's `modified` stamp.
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
        lines = parse.split_lines(content)
        trailing = "\n" if content.endswith("\n") else ""
        changed = False
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
            current = metadata.get("sort")
            if (
                isinstance(current, (int, float))
                and not isinstance(current, bool)
                and current == rank[task_id]
            ):
                continue
            metadata["sort"] = rank[task_id]
            lines[task.meta_line - 1] = parse.task_meta_line(metadata)
            changed = True
        if changed:
            pending.append((path, "\n".join(lines) + trailing))

    for path, content in pending:
        normalized = state.store.write(path, content, preserve_modified=True)
        await state.indexer.index_note(path, normalized)
        _publish(state, path, normalized)
    if pending:
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
    lines = parse.split_lines(content)
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
    remind_at = _validate_reminder(remind_at)
    row = await _fetch_task(state.db, task_id)
    if row is None:
        raise NoteError(404, "Task not found")
    path = row["path"]
    content = state.store.read(path)
    task = _find_task(content, task_id)
    if task.meta_line is None:
        raise NoteError(409, "Task metadata is missing; refresh the note to repair it")
    lines = parse.split_lines(content)
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


async def move_tasks(state, task_ids: list[str], target_path: str) -> list[dict]:
    """Relocate tasks to another note/board, keeping their ids and history.

    Markdown stays authoritative, so this is an append-to-target plus
    remove-from-source rewrite, and each touched note is written once however
    many tasks move. The metadata comment carries the stable id and lifecycle
    timestamps across intact; only the column rank is dropped, because a rank
    is meaningful only inside the column it was set in — the task rejoins its
    new home at the natural (due/append) order.

    Every source is validated before anything is written, and the target is
    written first: a failure between the writes leaves a task in both notes
    (the index keeps one row per id and the next sync heals it), never in
    neither.
    """
    target = (target_path or "").strip()
    if not target:
        raise NoteError(400, "Pick a note to move the task into")
    rows = []
    for task_id in dict.fromkeys(task_ids):
        row = await _fetch_task(state.db, task_id)
        if row is None:
            raise NoteError(404, "Task not found")
        rows.append(row)
    by_source: dict[str, list[str]] = {}
    for row in rows:
        if row["path"] != target:
            by_source.setdefault(row["path"], []).append(row["id"])
    if not by_source:
        return [public_task(dict(row)) for row in rows]
    if not state.store.exists(target):
        raise NoteError(404, "Target note not found")

    blocks: dict[str, str] = {}
    sources: list[tuple[str, str]] = []
    for source, ids in by_source.items():
        content = state.store.read(source)
        _meta, body = fm.parse(content)
        offset = parse.frontmatter_line_count(content, body)
        found = {task.id: task for task in parse.extract_tasks(body, offset)}
        lines = parse.split_lines(content)
        drop: set[int] = set()
        for task_id in ids:
            task = found.get(task_id)
            if task is None:
                raise NoteError(409, "Task moved or changed; refresh and try again")
            if task.meta_line is None:
                raise NoteError(409, "Task metadata is missing; refresh the note to repair it")
            meta_match = parse.TASK_META_RE.match(lines[task.meta_line - 1])
            line_match = parse.TASK_RE.match(lines[task.line - 1])
            if not meta_match or not line_match:
                raise NoteError(409, "Task changed; refresh and try again")
            try:
                metadata = json.loads(meta_match.group(1))
            except json.JSONDecodeError:
                raise NoteError(409, "Task metadata is invalid")
            metadata.pop("sort", None)
            # Matching create_task's block shape.
            blocks[task_id] = f"{parse.task_meta_line(metadata)}\n{lines[task.line - 1]}\n"
            drop.update({task.line - 1, task.meta_line - 1})
        trailing = "\n" if content.endswith("\n") else ""
        sources.append(
            (source, "\n".join(line for i, line in enumerate(lines) if i not in drop) + trailing)
        )

    target_content = state.store.read(target)
    if target_content and not target_content.endswith("\n"):
        target_content += "\n"
    for row in rows:
        if row["id"] in blocks:
            target_content += "\n" + blocks[row["id"]]
    target_normalized = state.store.write(target, target_content)
    written = [(source, state.store.write(source, content)) for source, content in sources]

    # Sources first: dropping their rows frees each id before the target's
    # insert claims it.
    for source, normalized in written:
        await state.indexer.index_note(source, normalized)
    await state.indexer.index_note(target, target_normalized)
    state.autocommit.touch()
    for source, normalized in written:
        _publish(state, source, normalized)
    _publish(state, target, target_normalized)
    moved = []
    for row in rows:
        fresh = await _fetch_task(state.db, row["id"])
        if fresh is not None:
            moved.append(public_task(dict(fresh)))
    return moved


async def move_task(state, task_id: str, target_path: str) -> dict:
    """Relocate one task to another note/board; see move_tasks."""
    return (await move_tasks(state, [task_id], target_path))[0]
