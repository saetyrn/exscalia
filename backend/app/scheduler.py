"""OMNIA scheduled runs — recurring instructions the user asked OMNIA to run.

Specs live in the vault (``.exscalia/omnia/schedules.json``) so they are
git-recoverable like every other thing OMNIA does. A lifespan ticker fires the
due ones through ``overseer.chat`` in **proactive** mode, which restricts them
to suggestion-tier tools: a scheduled run can read, compose, propose, and
``notify_user``, but never silently perform a destructive write. It is dormant
until the user creates a schedule, and every run is gated by the same soft token
budget that pauses the nightly housekeeping.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .vault.internal import atomic_json, iso_utc, read_json
from .vault.notes import NoteError, NoteStore

log = logging.getLogger(__name__)

FREQUENCIES = {"daily", "weekly"}
MAX_SCHEDULES = 50
SCHEDULE_TICK = 60  # seconds; schedules fire at HH:MM granularity
SCHEDULE_PATH = f"{NoteStore.INTERNAL_DIR}/omnia/schedules.json"
_UTC = ZoneInfo("UTC")


def schedule_tz(state) -> ZoneInfo:
    try:
        return ZoneInfo(getattr(state, "timezone", "UTC"))
    except (ZoneInfoNotFoundError, ValueError):
        return _UTC


def _parse_at(at: str) -> tuple[int, int]:
    try:
        hours, minutes = str(at).split(":")
        hour, minute = int(hours), int(minutes)
    except (ValueError, AttributeError):
        raise NoteError(400, "Schedule time must be HH:MM (24-hour)")
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise NoteError(400, "Schedule time must be HH:MM (24-hour)")
    return hour, minute


def clean_cadence(frequency, interval, at, weekday) -> dict:
    """Validate a schedule cadence: daily/weekly, an interval, a time of day,
    and (weekly) a weekday. Deliberately simpler than event recurrence."""
    freq = str(frequency or "").lower()
    if freq not in FREQUENCIES:
        raise NoteError(400, "Schedule frequency must be daily or weekly")
    hour, minute = _parse_at(at or "09:00")
    step = int(interval or 1)
    if not 1 <= step <= 30:
        raise NoteError(400, "Schedule interval must be 1-30")
    cadence = {"frequency": freq, "interval": step, "at": f"{hour:02d}:{minute:02d}"}
    if freq == "weekly":
        day = int(weekday if weekday is not None else 0)
        if not 0 <= day <= 6:
            raise NoteError(400, "weekday must be 0 (Monday) - 6 (Sunday)")
        cadence["weekday"] = day
    return cadence


def next_run(cadence: dict, after: datetime, tz: ZoneInfo) -> str:
    """The first fire strictly after ``after``, as a UTC ISO string."""
    after_local = after.astimezone(tz)
    hour, minute = _parse_at(cadence.get("at") or "09:00")
    interval = int(cadence.get("interval") or 1)
    candidate = after_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if cadence.get("frequency") == "weekly":
        weekday = int(cadence.get("weekday") or 0)
        candidate += timedelta(days=(weekday - candidate.weekday()) % 7)
        while candidate <= after_local:
            candidate += timedelta(weeks=interval)
    else:  # daily
        while candidate <= after_local:
            candidate += timedelta(days=interval)
    return candidate.astimezone(_UTC).isoformat()


class ScheduleStore:
    """Schedule specs, versioned in the vault."""

    VERSION = 1

    def __init__(self, vault_dir: Path):
        self.path = vault_dir / SCHEDULE_PATH

    def _read(self) -> list[dict]:
        value = read_json(self.path, {})
        items = value.get("schedules", []) if isinstance(value, dict) else []
        return [item for item in items if isinstance(item, dict)]

    def _save(self, schedules: list[dict]) -> None:
        atomic_json(self.path, {"version": self.VERSION, "schedules": schedules})

    def list(self) -> list[dict]:
        return self._read()

    def get(self, schedule_id: str) -> dict:
        for schedule in self._read():
            if schedule.get("id") == schedule_id:
                return schedule
        raise NoteError(404, "Schedule not found")

    def add(self, instruction: str, cadence: dict, tz: ZoneInfo, now: datetime) -> dict:
        schedules = self._read()
        if len(schedules) >= MAX_SCHEDULES:
            raise NoteError(400, f"Too many schedules (max {MAX_SCHEDULES})")
        schedule = {
            "id": uuid.uuid4().hex[:12],
            "instruction": instruction,
            "cadence": cadence,
            "paused": False,
            "created_at": iso_utc(),
            "last_run": None,
            "next_run": next_run(cadence, now, tz),
        }
        schedules.append(schedule)
        self._save(schedules)
        return schedule

    def update(self, schedule_id: str, **changes) -> dict:
        schedules = self._read()
        for schedule in schedules:
            if schedule.get("id") == schedule_id:
                schedule.update(changes)
                self._save(schedules)
                return schedule
        raise NoteError(404, "Schedule not found")

    def remove(self, schedule_id: str) -> dict:
        schedules = self._read()
        for index, schedule in enumerate(schedules):
            if schedule.get("id") == schedule_id:
                removed = schedules.pop(index)
                self._save(schedules)
                return removed
        raise NoteError(404, "Schedule not found")


async def _run_one(state, overseer, schedule: dict) -> None:
    tz = schedule_tz(state)
    now = datetime.now(tz)
    thread, _ = state.threads.ensure_daily(now.date().isoformat(), now.strftime("%A, %B %-d"))
    thread_id = thread["id"]
    overseer.sessions[thread_id] = state.threads.provider_history(thread_id)
    prompt = (
        "(Scheduled run — the user set this up to run automatically. Instruction: "
        f"{schedule['instruction']}) Carry it out now with your read and compose "
        "tools. If there is something worth telling the user, call notify_user with a "
        "short title and one-line body. If nothing is worth surfacing, do nothing and "
        "reply with a single word."
    )
    result = await overseer.chat(prompt, session_id=thread_id, proactive=True)
    state.threads.set_provider_history(
        thread_id, list(overseer.sessions.get(thread_id) or [])
    )
    message = (result or {}).get("reply")
    if message and len(message.strip()) > 1:
        state.threads.append_public(thread_id, {"role": "assistant", "text": message})
    if hasattr(state, "autocommit"):
        state.autocommit.touch()


async def run_due_schedules(state, now: datetime | None = None) -> list[str]:
    """Fire every schedule whose time has come. Advances each schedule's
    next_run exactly once even after downtime, so a gap never causes a burst."""
    overseer = getattr(state, "overseer", None)
    store = ScheduleStore(state.settings.vault_dir)
    schedules = store.list()
    if overseer is None or not schedules:
        return []
    tz = schedule_tz(state)
    moment = now or datetime.now(tz)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=tz)

    budget_ok = True
    ledger = getattr(state, "usage", None)
    if ledger is not None:
        try:
            from .overseer.runtime_config import load_ai_config, monthly_token_budget

            budget_ok = ledger.allow_background(
                monthly_token_budget(load_ai_config(state.settings.data_dir))
            )
        except Exception:  # never let a config hiccup wedge the ticker
            budget_ok = True

    ran: list[str] = []
    for schedule in schedules:
        if schedule.get("paused"):
            continue
        raw_next = schedule.get("next_run")
        if not raw_next:
            continue
        try:
            due = datetime.fromisoformat(raw_next)
        except ValueError:
            continue
        if due.tzinfo is None:
            due = due.replace(tzinfo=_UTC)
        if due > moment:
            continue
        # Advance first, so a failure or a paused budget can't re-fire in a loop.
        upcoming = next_run(schedule["cadence"], moment, tz)
        if not budget_ok:
            store.update(schedule["id"], next_run=upcoming)
            continue
        try:
            await _run_one(state, overseer, schedule)
            ran.append(schedule["id"])
        except Exception:
            log.warning("scheduled run failed", exc_info=True)
        store.update(schedule["id"], last_run=iso_utc(), next_run=upcoming)
    return ran
