"""Time-based delivery for reminders the user asked for.

Reminders were previously only ever surfaced by the morning greeting, which
fires whenever the app is first opened that day. A "15 minutes before a 4pm
lecture" reminder was therefore emitted at 8am and never again — the whole
`reminders` field was decorative.

This is deliberately narrow: it delivers only reminders attached to events the
user created, never unsolicited commentary.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .vault.internal import atomic_json, iso_utc, read_json

log = logging.getLogger(__name__)

# How often the scheduler wakes. A minute is granular enough for "15 minutes
# before" to land usefully and costs nothing.
TICK_SECONDS = 60
# Alerts nobody collected within this window are dropped rather than piling up
# to greet the user with a wall of stale notices.
MAX_QUEUE = 20


class AlertQueue:
    """Reminders waiting to be shown. Lives in data_dir, not the vault: this is
    transient delivery state, not notebook content."""

    def __init__(self, data_dir: Path):
        self.path = data_dir / "omnia-alerts.json"

    def _read(self) -> list[dict[str, Any]]:
        value = read_json(self.path, [])
        return value if isinstance(value, list) else []

    def add(self, alerts: list[dict[str, Any]]) -> None:
        if not alerts:
            return
        queued = self._read()
        known = {a.get("key") for a in queued}
        for alert in alerts:
            if alert.get("key") not in known:
                queued.append({**alert, "queued_at": iso_utc()})
        atomic_json(self.path, queued[-MAX_QUEUE:])

    def drain(self) -> list[dict[str, Any]]:
        """Return everything waiting and clear it — alerts are shown once."""
        queued = self._read()
        if queued:
            atomic_json(self.path, [])
        return queued

    def peek(self) -> list[dict[str, Any]]:
        """Read what's queued WITHOUT clearing it, so OMNIA can report on
        pending reminders without consuming them out from under the client."""
        return self._read()


class TaskReminderLog:
    """Task-reminder keys already delivered, so a reminder fires exactly once.

    Events track this inside events.json; a task's reminder lives in its
    markdown, and rewriting the note from a background tick to mark it delivered
    would race real edits. This transient ledger in data_dir keeps delivery
    bookkeeping out of the notebook, mirroring AlertQueue."""

    RETAIN_DAYS = 90

    def __init__(self, data_dir: Path):
        self.path = data_dir / "omnia-task-reminders.json"

    def _read(self) -> dict[str, str]:
        value = read_json(self.path, {})
        return value if isinstance(value, dict) else {}

    def seen(self) -> dict[str, str]:
        return self._read()

    def mark(self, fired: dict[str, str], now: datetime) -> None:
        """Record freshly delivered keys and drop ones long past their time."""
        if not fired:
            return
        cutoff = (now.replace(tzinfo=None) - timedelta(days=self.RETAIN_DAYS)).isoformat()
        merged = {**self._read(), **fired}
        kept = {key: at for key, at in merged.items() if at >= cutoff}
        atomic_json(self.path, kept)


async def task_due_reminders(state, now: datetime | None = None) -> list[dict[str, Any]]:
    """Reminders attached to open tasks whose trigger time has arrived.

    A task keeps its reminder as an ISO local date-time in its metadata; this
    scans the indexed `reminder_at` column, emits the ones now due that were not
    already delivered, and records them so they never repeat."""
    try:
        tz = ZoneInfo(getattr(state, "timezone", "UTC"))
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo("UTC")
    moment = now or datetime.now(tz)
    naive_now = moment.replace(tzinfo=None)
    today = moment.date().isoformat()

    rows = await state.db.execute_fetchall(
        "SELECT id, text, reminder_at, due FROM tasks "
        "WHERE reminder_at IS NOT NULL AND reminder_at != '' AND status != 'done'"
    )
    ledger = TaskReminderLog(state.settings.data_dir)
    seen = ledger.seen()
    alerts: list[dict[str, Any]] = []
    fired: dict[str, str] = {}
    for row in rows:
        remind_at = str(row["reminder_at"])
        try:
            trigger = datetime.fromisoformat(remind_at).replace(tzinfo=None)
        except ValueError:
            continue
        if trigger > naive_now:
            continue  # still in the future; a later tick will pick it up
        key = f"task:{row['id']}:{remind_at}"
        if key in seen:
            continue
        due = str(row["due"] or "")
        alerts.append(
            {
                "key": key,
                "title": row["text"],
                "starts_at": remind_at,
                "minutes_before": 0,
                "overdue": bool(due) and due < today,
                "task_id": row["id"],
            }
        )
        fired[key] = remind_at
    ledger.mark(fired, moment)
    return alerts


def due_reminders(state, now: datetime | None = None) -> list[dict[str, Any]]:
    """Reminders whose trigger time has arrived and that haven't been shown."""
    try:
        tz = ZoneInfo(getattr(state, "timezone", "UTC"))
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo("UTC")
    moment = now or datetime.now(tz)
    # mark_surfaced consumes only reminders whose trigger has actually passed,
    # so a future one stays queued for its real time.
    digest = state.events.digest(moment.date(), mark_surfaced=True, now=moment)

    # digest() reports every reminder falling on the day — right for the morning
    # greeting, wrong here: delivery must wait for the trigger to actually pass.
    naive_now = moment.replace(tzinfo=None)
    alerts = []
    for reminder in digest.get("reminders") or []:
        start = str(reminder.get("occurrence_start") or "")
        minutes = int(reminder.get("minutes_before") or 0)
        try:
            trigger = datetime.fromisoformat(start) - timedelta(minutes=minutes)
        except ValueError:
            continue
        if trigger > naive_now:
            continue  # still in the future; a later tick will pick it up
        alerts.append(
            {
                "key": f"{reminder.get('event_id')}|{start}|{minutes}",
                "title": reminder.get("title"),
                "starts_at": start,
                "minutes_before": minutes,
                "overdue": bool(reminder.get("overdue")),
            }
        )
    return alerts
