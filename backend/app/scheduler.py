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

import contextlib
import logging
import uuid
from contextvars import ContextVar
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


# The unattended run a model turn belongs to, for notify_user's per-run cap.
# Every schedule of the day, the morning check-in and the user's own chat share
# the daily thread, so a cap keyed by thread held back the third schedule due
# at 08:00 although the user set each one up to notify them.
notify_run: ContextVar[str | None] = ContextVar("notify_run", default=None)


@contextlib.asynccontextmanager
async def run_scope(label: str):
    """Count the notify_user calls made inside as one run's."""
    token = notify_run.set(f"{label}:{uuid.uuid4().hex[:8]}")
    try:
        yield
    finally:
        notify_run.reset(token)


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
            "next_run_tz": tz.key,
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


NOTHING_SENTINEL = "NOTHING"


def is_nothing_reply(text: str) -> bool:
    """True when a scheduled run said it had nothing to surface.

    The prompt used to ask for "a single word" and the guard only dropped
    replies of one character, so an obedient "Nothing." landed in the daily
    thread as a visible assistant message. Asking for one literal word makes
    the null reply recognisable; the punctuation strip covers the models that
    can't resist a full stop.
    """
    return text.strip().rstrip(".!…").strip().lower() == NOTHING_SENTINEL.lower()


async def _run_one(state, overseer, schedule: dict) -> str:
    """Run one schedule; "ok" when it posted to the daily thread, "nothing"
    when it answered with the null sentinel (or not at all)."""
    tz = schedule_tz(state)
    now = datetime.now(tz)
    thread, _ = state.threads.ensure_daily(now.date().isoformat(), now.strftime("%A, %B %-d"))
    thread_id = thread["id"]
    prompt = (
        "(Scheduled run — the user set this up to run automatically. Instruction: "
        f"{schedule['instruction']}) Carry it out now with your read and compose "
        "tools. If there is something worth telling the user, call notify_user with a "
        "short title and one-line body. If nothing is worth surfacing, do nothing and "
        f"reply with exactly one word: {NOTHING_SENTINEL}. That reply is discarded, "
        "so never use it to say anything you want the user to read."
    )
    # The daily thread is the one the user is most likely typing in. Without
    # the lock a run beside a user turn swapped in its own copy of the history,
    # and whichever persisted last dropped the other exchange. chat() appends
    # to `history` itself, so that list is what gets saved, not whatever the
    # shared sessions dict holds by then.
    outcome = "nothing"
    run = run_scope(f"schedule:{schedule.get('id')}")
    async with state.threads.generation_lock(thread_id), run:
        history = state.threads.provider_history(thread_id)
        overseer.sessions[thread_id] = history
        result = await overseer.chat(prompt, session_id=thread_id, proactive=True)
        state.threads.set_provider_history(thread_id, list(history))
        result = result or {}
        # The closing round alone: the joined reply keeps any narration from
        # before a tool call ("Let me look.\n\nNOTHING"), which the sentinel
        # check can't match, so a null run landed in the daily thread.
        message = result.get("final_text", result.get("reply"))
        if message and message.strip() and not is_nothing_reply(message):
            state.threads.append_public(thread_id, {"role": "assistant", "text": message})
            outcome = "ok"
    if hasattr(state, "autocommit"):
        state.autocommit.touch()
    return outcome


def _bookkeep(store: ScheduleStore, schedule_id: str, **changes) -> dict | None:
    """Write bookkeeping onto a schedule. None when it was cancelled meanwhile:
    a run takes seconds to minutes, and OMNIA can cancel a schedule from
    another turn (or from inside its own run)."""
    try:
        return store.update(schedule_id, **changes)
    except NoteError:
        log.info("schedule %s was cancelled mid-tick", schedule_id)
        return None


async def run_due_schedules(state, now: datetime | None = None) -> list[str]:
    """Fire every schedule whose time has come. Advances each schedule's
    next_run exactly once even after downtime, so a gap never causes a burst,
    and persists the advance before the run starts, so a restart or a
    cancellation mid-run can't fire the same slot twice.

    Each record carries the zone its next_run was computed in; after a
    timezone change the stored instant is recomputed in the new zone (and not
    fired this tick) instead of firing once at the old zone's wall-clock time.
    A paused schedule's next_run keeps moving too, so resuming it waits for
    the next real slot. Outcomes land on the record: last_status (ok, nothing,
    failed, skipped_budget), last_error, a consecutive failure count, and
    last_run, which is stamped only when the run actually completed."""
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
        schedule_id = schedule.get("id")
        if not schedule_id:
            continue
        zone = schedule.get("next_run_tz")
        if zone != tz.key:
            if zone:  # the timezone changed since next_run was computed
                _bookkeep(
                    store,
                    schedule_id,
                    next_run=next_run(schedule["cadence"], moment, tz),
                    next_run_tz=tz.key,
                )
                continue
            _bookkeep(store, schedule_id, next_run_tz=tz.key)  # legacy record
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
        upcoming = next_run(schedule["cadence"], moment, tz)
        if schedule.get("paused"):
            _bookkeep(store, schedule_id, next_run=upcoming, next_run_tz=tz.key)
            continue
        if not budget_ok:
            _bookkeep(
                store,
                schedule_id,
                next_run=upcoming,
                next_run_tz=tz.key,
                last_status="skipped_budget",
            )
            continue
        # Advance first, so a failure, a restart or a cancellation mid-run
        # can't re-fire this slot. The re-read also catches a pause or cancel
        # made by an earlier run in this same tick.
        fresh = _bookkeep(store, schedule_id, next_run=upcoming, next_run_tz=tz.key)
        if fresh is None or fresh.get("paused"):
            continue
        try:
            outcome = await _run_one(state, overseer, fresh)
        except Exception as exc:
            log.warning("scheduled run failed", exc_info=True)
            _bookkeep(
                store,
                schedule_id,
                last_status="failed",
                last_error=f"{type(exc).__name__}: {exc}"[:300],
                failures=int(fresh.get("failures") or 0) + 1,
            )
            continue
        ran.append(schedule_id)
        _bookkeep(
            store,
            schedule_id,
            last_run=iso_utc(),
            last_status=outcome,
            last_error=None,
            failures=0,
        )
    return ran
