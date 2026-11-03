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

from . import push_service
from .vault.internal import atomic_json, iso_utc, read_json

log = logging.getLogger(__name__)

# How often the scheduler wakes. A minute is granular enough for "15 minutes
# before" to land usefully and costs nothing.
TICK_SECONDS = 60
# Alerts nobody collected within this window are dropped rather than piling up
# to greet the user with a wall of stale notices.
MAX_QUEUE = 20
# A task reminder more than this late is never delivered. Without a bound, a
# lost or quarantined ledger replayed every reminder ever set on an open task,
# and one older than the ledger's retention re-fired on every tick forever.
TASK_REMINDER_GRACE = timedelta(hours=24)
# An event reminder is pushed from its trigger until this long after it, or
# until its occurrence starts if that is later. Anything older (the server was
# down, a series was backdated) is left for the morning greeting to list as
# missed rather than arriving as a burst of stale pushes.
EVENT_REMINDER_GRACE = timedelta(minutes=30)


class AlertQueue:
    """Reminders waiting to be shown. Lives in data_dir, not the vault: this is
    transient delivery state, not notebook content."""

    def __init__(self, data_dir: Path):
        self.path = data_dir / "omnia-alerts.json"

    def _read(self) -> list[dict[str, Any]]:
        value = read_json(self.path, [])
        return value if isinstance(value, list) else []

    def add(self, alerts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Queue alerts, skipping keys already waiting. Returns the ones newly
        queued, so a retried tick does not push the same reminder again."""
        if not alerts:
            return []
        queued = self._read()
        known = {a.get("key") for a in queued}
        added = []
        for alert in alerts:
            if alert.get("key") not in known:
                known.add(alert.get("key"))
                queued.append({**alert, "queued_at": iso_utc()})
                added.append(alert)
        atomic_json(self.path, queued[-MAX_QUEUE:])
        return added

    def drain(self) -> list[dict[str, Any]]:
        """Return everything waiting and clear it. Only for clients that
        never ack (a desktop build from before peek): a response lost in
        flight takes the alert with it."""
        queued = self._read()
        if queued:
            atomic_json(self.path, [])
        return queued

    def peek(self) -> list[dict[str, Any]]:
        """Read what's queued WITHOUT clearing it. The client shows these and
        then acks them; OMNIA reports on them without consuming anything."""
        return self._read()

    def ack(self, keys: list[str]) -> int:
        """Remove the given keys once the client has shown them."""
        wanted = set(keys)
        if not wanted:
            return 0
        queued = self._read()
        kept = [a for a in queued if a.get("key") not in wanted]
        if len(kept) != len(queued):
            atomic_json(self.path, kept)
        return len(queued) - len(kept)


class TaskReminderLog:
    """Task-reminder keys already delivered, so a reminder fires at most once.

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
        """Record freshly delivered keys (valued by delivery time) and drop ones
        delivered long ago. Older ledgers hold the reminder's own time instead;
        both are naive ISO strings, so the cutoff compares them the same way."""
        if not fired:
            return
        cutoff = (now.replace(tzinfo=None) - timedelta(days=self.RETAIN_DAYS)).isoformat()
        merged = {**self._read(), **fired}
        kept = {key: at for key, at in merged.items() if at >= cutoff}
        atomic_json(self.path, kept)


async def task_due_reminders(state, now: datetime | None = None, mark: bool = True):
    """Reminders attached to open tasks whose trigger time has arrived.

    A task keeps its reminder as an ISO local date-time in its metadata; this
    scans the indexed `reminder_at` column, emits the ones now due that were not
    already delivered, and records them so they never repeat. A reminder more
    than TASK_REMINDER_GRACE late is skipped outright, so nothing the ledger
    forgets (pruned, lost, quarantined) can come back. With mark=False it
    returns `(alerts, fired)` and leaves the ledger alone, so the caller can
    queue the alerts before recording them as delivered."""
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
        if naive_now - trigger > TASK_REMINDER_GRACE:
            continue  # long missed; a push now would only be noise
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
        fired[key] = naive_now.isoformat()
    if not mark:
        return alerts, fired
    ledger.mark(fired, moment)
    return alerts


def settle_passed_task_reminders(state, reminders: dict[str, str]) -> None:
    """Record as delivered the task reminders (task id -> reminder_at) whose
    time has already passed.

    Moving a due date carries its reminder along (task_service.update_task),
    and a roll-over to today can land it earlier today: a key the ledger has
    never seen, inside TASK_REMINDER_GRACE, so the next tick would push it,
    one per task moved. The time it was set for is gone; the task keeps it
    only so that later moves keep the same distance from the due date."""
    try:
        tz = ZoneInfo(getattr(state, "timezone", "UTC"))
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo("UTC")
    moment = datetime.now(tz)
    naive_now = moment.replace(tzinfo=None)
    settled: dict[str, str] = {}
    for task_id, remind_at in reminders.items():
        try:
            trigger = datetime.fromisoformat(remind_at).replace(tzinfo=None)
        except ValueError:
            continue
        if trigger <= naive_now and naive_now - trigger <= TASK_REMINDER_GRACE:
            settled[f"task:{task_id}:{remind_at}"] = naive_now.isoformat()
    TaskReminderLog(state.settings.data_dir).mark(settled, moment)


def due_reminders(state, now: datetime | None = None, consume: bool = True):
    """Reminders due right now that haven't been shown.

    With consume=False nothing is written: it returns `(alerts, pairs)`, where
    `pairs` are the keys to hand to `events.mark_surfaced` once the alerts are
    safely queued."""
    try:
        tz = ZoneInfo(getattr(state, "timezone", "UTC"))
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo("UTC")
    moment = now or datetime.now(tz)
    # Nothing is consumed here up front: only what is actually delivered below
    # is marked, so a missed reminder stays for the morning greeting.
    digest = state.events.digest(moment.date(), mark_surfaced=False, now=moment)

    # digest() reports every reminder falling on the day — right for the morning
    # greeting, wrong here: delivery must wait for the trigger to actually pass.
    naive_now = moment.replace(tzinfo=None)
    alerts = []
    # Only reminders actually delivered are consumed, so a future one stays
    # pending for its real time.
    pairs: list[tuple[str, str]] = []
    for reminder in digest.get("reminders") or []:
        start = str(reminder.get("occurrence_start") or "")
        minutes = int(reminder.get("minutes_before") or 0)
        try:
            # An all-day occurrence's start is a bare date: its midnight.
            start_local = datetime.fromisoformat(start)
            # digest's own trigger, so sub-day offsets across a DST change count
            # the same elapsed minutes here as there.
            trigger = datetime.fromisoformat(str(reminder.get("trigger") or ""))
        except ValueError:
            continue
        if trigger > naive_now:
            continue  # still in the future; a later tick will pick it up
        if naive_now > max(trigger + EVENT_REMINDER_GRACE, start_local):
            continue  # missed; the morning greeting lists and consumes it
        alerts.append(
            {
                "key": f"{reminder.get('event_id')}|{start}|{minutes}",
                "title": reminder.get("title"),
                "starts_at": start,
                "minutes_before": minutes,
                "overdue": bool(reminder.get("overdue")),
            }
        )
        # The surfaced key is "start|minutes", not the alert key above.
        pairs.append((str(reminder.get("event_id")), f"{start}|{minutes}"))
    if not consume:
        return alerts, pairs
    if pairs:
        state.events.mark_surfaced(pairs, moment.date())
    return alerts


async def reminder_tick(
    state, now: datetime | None = None, push: bool = True
) -> list[dict[str, Any]]:
    """One pass of reminder delivery: queue first, then mark delivered.

    Marking first lost a reminder whenever anything after it failed (the task
    query, a corrupt ledger or queue file); the next tick treated it as sent.
    Now a failure leaves it unmarked and the next tick offers it again, which
    AlertQueue.add and the client's shown-set make harmless. Each source has
    its own try block so one failing cannot strand the other. With push=False
    the newly queued alerts are returned unpushed, for a caller that delivers
    them off the tick (main.py, so a slow push service never delays the queue).
    """
    try:
        tz = ZoneInfo(getattr(state, "timezone", "UTC"))
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo("UTC")
    moment = now or datetime.now(tz)
    added: list[dict[str, Any]] = []
    try:
        event_alerts, pairs = due_reminders(state, moment, consume=False)
        added += state.alerts.add(event_alerts)
        state.events.mark_surfaced(pairs, moment.date())
    except Exception:
        log.warning("event reminder tick failed", exc_info=True)
    try:
        task_alerts, fired = await task_due_reminders(state, moment, mark=False)
        added += state.alerts.add(task_alerts)
        TaskReminderLog(state.settings.data_dir).mark(fired, moment)
    except Exception:
        log.warning("task reminder tick failed", exc_info=True)
    if push:
        await push_service.deliver(state, added)
    return added
