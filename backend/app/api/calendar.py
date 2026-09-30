"""Calendar and task lifecycle activity."""

from __future__ import annotations

from datetime import date as date_cls, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Request
from pydantic import BaseModel

from ..task_service import public_task, update_task
from ..vault.notes import NoteError

router = APIRouter(prefix="/api", tags=["calendar"])


def _timezone(request: Request) -> ZoneInfo:
    try:
        return ZoneInfo(request.app.state.timezone)
    except ZoneInfoNotFoundError:
        return ZoneInfo("America/New_York")


def _local_day(value: str | None, tz: ZoneInfo) -> str | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(tz).date().isoformat()
    except ValueError:
        return None


def _note_day(value: str | None, tz: ZoneInfo) -> str | None:
    """The local day of a note's created/modified stamp.

    The app writes those in UTC, so their leading date put anything written in
    the evening on the next day. A hand-written date-only or zone-less value
    is already local and keeps its date, as does anything unparseable.
    """
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value[:10]
    if stamp.tzinfo is None:
        return stamp.date().isoformat()
    return stamp.astimezone(tz).date().isoformat()


def _padded(first: date_cls, last: date_cls) -> tuple[str, str]:
    """UTC days that can hold a stamp from [first, last] in any zone."""
    return (first - timedelta(days=1)).isoformat(), (last + timedelta(days=1)).isoformat()


async def _task_rows(request: Request) -> list[dict]:
    rows = [
        dict(row)
        for row in await request.app.state.db.execute_fetchall(
            "SELECT * FROM tasks WHERE path NOT LIKE 'templates/%'"
        )
    ]
    active_ids = {row["id"] for row in rows}
    for task_id, row in request.app.state.task_tombstones.all().items():
        if task_id not in active_ids:
            rows.append({**row, "tombstone": True, "due_visible": False})
    return rows


def _events(rows: list[dict], tz: ZoneInfo, day: str | None = None) -> list[dict]:
    events: list[dict] = []
    for row in rows:
        task = public_task(row)
        # A folder board's task is invisible here until it is finished, and
        # then it contributes exactly one event: the completion. Backfilling
        # created/started would make dots appear in the past, on days the task
        # was deliberately off the calendar. `.get` — tombstones written
        # before boards existed carry no flag.
        milestones = (
            (("completed", "completed_at"),)
            if row.get("board")
            else (
                ("created", "created_at"),
                ("started", "started_at"),
                ("completed", "completed_at"),
            )
        )
        for event_type, field in milestones:
            # A reopened task keeps its completed_at as history; it is not
            # completed on that day any more.
            if event_type == "completed" and row.get("status") != "done":
                continue
            event_day = _local_day(row.get(field), tz)
            if event_day and (day is None or day == event_day):
                events.append(
                    {
                        "type": event_type,
                        "date": event_day,
                        "timestamp": row.get(field),
                        "task": task,
                    }
                )
        due = row.get("due")
        if (
            due
            and not row.get("tombstone")
            and not row.get("board")
            and (day is None or day == due)
        ):
            events.append(
                {
                    "type": "due",
                    "date": due,
                    "timestamp": None,
                    "task": task,
                }
            )
    order = {"created": 0, "started": 1, "completed": 2, "due": 3}
    return sorted(
        events,
        key=lambda event: (
            event["date"],
            order[event["type"]],
            event["task"].get("text", ""),
        ),
    )


@router.get("/calendar/day/{day}")
async def day_view(request: Request, day: str):
    try:
        target = date_cls.fromisoformat(day)
    except ValueError:
        raise NoteError(400, f"Invalid date: {day}")
    tz = _timezone(request)
    db = request.app.state.db
    lo, hi = _padded(target, target)
    candidates = await db.execute_fetchall(
        """SELECT path, title, created, modified FROM notes
           WHERE (substr(created,1,10) BETWEEN ? AND ?
                  OR substr(modified,1,10) BETWEEN ? AND ?)
             AND path NOT LIKE 'templates/%'
           ORDER BY modified DESC""",
        (lo, hi, lo, hi),
    )
    notes = []
    for row in candidates:
        created_today = _note_day(row["created"], tz) == day
        if created_today or _note_day(row["modified"], tz) == day:
            notes.append({**dict(row), "created_today": int(created_today)})
    rows = await _task_rows(request)
    due_tasks = [
        public_task(row)
        for row in rows
        if row.get("due") == day and not row.get("tombstone") and not row.get("board")
    ]
    return {
        "date": day,
        "daily_note": f"daily/{day}.md"
        if request.app.state.store.exists(f"daily/{day}.md")
        else None,
        "notes": notes,
        "tasks": due_tasks,
        "task_events": _events(rows, tz, day),
        # Includes events still running from an earlier day; `continuation`
        # marks those.
        "events": (
            [
                {
                    **event,
                    "continuation": event["occurrence_date"] != target.isoformat(),
                }
                for event in request.app.state.events.occurrences(target, target)
            ]
            if hasattr(request.app.state, "events")
            else []
        ),
        "activity_hours": (
            request.app.state.activities.range_totals(
                date_cls.fromisoformat(day), date_cls.fromisoformat(day)
            ).get(day, 0)
            if hasattr(request.app.state, "activities")
            else 0
        ),
    }


@router.get("/calendar/range")
async def range_view(request: Request, start: str, end: str):
    return await calendar_range(request, start, end)


async def calendar_range(request: Request, start: str, end: str) -> dict:
    """Assemble the notes/tasks/events/daily-notes bundle for a date window.

    Shared by the ``/calendar/range`` route and the morning briefing so both
    speak the same shape without the briefing re-deriving any of it.
    """
    try:
        first = date_cls.fromisoformat(start)
        last = date_cls.fromisoformat(end)
    except ValueError:
        raise NoteError(400, "Calendar ranges must use YYYY-MM-DD")
    if last < first:
        raise NoteError(400, "Calendar range end cannot be before start")
    if (last - first).days > 366:
        raise NoteError(400, "Calendar ranges are limited to one year")

    tz = _timezone(request)
    db = request.app.state.db
    lo, hi = _padded(first, last)
    candidates = await db.execute_fetchall(
        """SELECT path, title, created, modified FROM notes
           WHERE (substr(created,1,10) BETWEEN ? AND ?)
              OR (substr(modified,1,10) BETWEEN ? AND ?)
           ORDER BY modified DESC""",
        (lo, hi, lo, hi),
    )
    notes = [
        dict(row)
        for row in candidates
        if start <= (_note_day(row["created"], tz) or "") <= end
        or start <= (_note_day(row["modified"], tz) or "") <= end
    ]
    task_rows = await _task_rows(request)
    task_events = [
        event
        for event in _events(task_rows, tz)
        if start <= event["date"] <= end
    ]
    due_tasks = [
        public_task(row)
        for row in task_rows
        if row.get("due")
        and start <= row["due"] <= end
        and not row.get("tombstone")
        and not row.get("board")
    ]
    daily_notes = []
    cursor = first
    while cursor <= last:
        day = cursor.isoformat()
        if request.app.state.store.exists(f"daily/{day}.md"):
            daily_notes.append(day)
        cursor += timedelta(days=1)
    return {
        "start": start,
        "end": end,
        "daily_notes": daily_notes,
        "notes": notes,
        "task_events": task_events,
        "due_tasks": due_tasks,
        "activity_hours": request.app.state.activities.range_totals(
            first, last
        ),
        "events": request.app.state.events.occurrences(first, last),
    }


@router.get("/calendar/{year}/{month}")
async def month_view(request: Request, year: int, month: int):
    if not (1 <= month <= 12):
        raise NoteError(400, "month must be 1-12")
    db = request.app.state.db
    store = request.app.state.store
    prefix = f"{year:04d}-{month:02d}"
    daily = [
        f"{prefix}-{day:02d}"
        for day in range(1, 32)
        if store.exists(f"daily/{prefix}-{day:02d}.md")
    ]

    tz = _timezone(request)
    month_start = date_cls(year, month, 1)
    lo, hi = _padded(
        month_start, (month_start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    )
    rows = await db.execute_fetchall(
        """SELECT created, modified FROM notes
           WHERE (substr(created,1,10) BETWEEN ? AND ?
                  OR substr(modified,1,10) BETWEEN ? AND ?)
             AND path NOT LIKE 'templates/%'""",
        (lo, hi, lo, hi),
    )
    activity: dict[str, int] = {}
    for field in ("created", "modified"):
        counts: dict[str, int] = {}
        for row in rows:
            local = _note_day(row[field], tz)
            if local and local.startswith(prefix):
                counts[local] = counts.get(local, 0) + 1
        for local, count in counts.items():
            activity[local] = max(activity.get(local, 0), count)

    task_rows = await _task_rows(request)
    events = [
        event
        for event in _events(task_rows, tz)
        if event["date"].startswith(prefix)
    ]
    task_activity: dict[str, int] = {}
    for event in events:
        task_activity[event["date"]] = task_activity.get(event["date"], 0) + 1
    tasks = [
        public_task(row)
        for row in task_rows
        if row.get("due")
        and row["due"].startswith(prefix)
        and not row.get("tombstone")
        and not row.get("board")
    ]
    event_occurrences = request.app.state.events.occurrences(
        date_cls(year, month, 1),
        date_cls(
            year,
            month,
            28,
        )
        + timedelta(days=4),
    )
    # occurrences() also returns an occurrence that started before the 1st and
    # runs into the month, so the query need not start earlier. The range runs
    # past the month's end; keep occurrences that cover one of its days, and
    # count each on every day of the month it covers.
    event_occurrences = [
        event
        for event in event_occurrences
        if any(day.startswith(prefix) for day in event["covers"])
    ]
    event_activity: dict[str, int] = {}
    for event in event_occurrences:
        for day in event["covers"]:
            if day.startswith(prefix):
                event_activity[day] = event_activity.get(day, 0) + 1
    return {
        "daily_notes": daily,
        "activity": activity,
        "task_activity": task_activity,
        "tasks": tasks,
        "event_activity": event_activity,
        "events": event_occurrences,
        "activity_hours": request.app.state.activities.range_totals(
            date_cls(year, month, 1),
            date_cls(year + (1 if month == 12 else 0), 1 if month == 12 else month + 1, 1)
            - timedelta(days=1),
        ),
    }


class ToggleTask(BaseModel):
    path: str
    line: int
    done: bool


# No UI caller (tasks update via PATCH /tasks/{id}); pinned by tests/contracts/desktop-*.json.
@router.post("/tasks/toggle")
async def toggle_task(request: Request, body: ToggleTask):
    row = await request.app.state.db.execute(
        "SELECT id FROM tasks WHERE path = ? AND line = ?", (body.path, body.line)
    )
    task = await row.fetchone()
    if task is None:
        raise NoteError(409, "Task moved or changed; refresh and try again")
    updated = await update_task(
        request.app.state,
        task["id"],
        status="done" if body.done else "todo",
    )
    return {"ok": True, "task": updated}
