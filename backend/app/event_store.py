"""Git-backed standalone calendar events and recurrence expansion."""

from __future__ import annotations

import hashlib
import json
import uuid
from calendar import monthrange
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .alerts import EVENT_REMINDER_GRACE
from .vault.internal import atomic_json, iso_utc, read_json
from .vault.notes import NoteError, NoteStore


FREQUENCIES = {"daily", "weekly", "monthly", "yearly"}


def _revision(event: dict[str, Any]) -> str:
    value = {
        key: item
        for key, item in event.items()
        if key not in {"revision", "surfaced_reminders"}
    }
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _reminder_trigger(start_local: datetime, minutes: int, tz: ZoneInfo) -> datetime:
    """When a reminder `minutes` before a naive local start fires, as naive local
    time in `tz`.

    Offsets under a day count elapsed minutes, so "2 hours before" 03:30 on the
    spring-forward day is 00:30, not 01:30. Day-length offsets keep wall-clock
    arithmetic: "1 day before" a 9am event stays at 9am across a DST change."""
    if minutes >= 1440:
        return start_local - timedelta(minutes=minutes)
    # fold=0: an ambiguous fall-back time means its first (daylight) instance.
    instant = start_local.replace(tzinfo=tz, fold=0).astimezone(timezone.utc)
    return (instant - timedelta(minutes=minutes)).astimezone(tz).replace(tzinfo=None)


def _key_start(key: str) -> datetime | None:
    """The occurrence start of a surfaced-reminder key ("start|minutes"), naive
    local; a bare all-day date parses as its midnight."""
    try:
        return datetime.fromisoformat(key.split("|", 1)[0])
    except ValueError:
        return None


def _clean_recurrence(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if not value:
        return None
    frequency = str(value.get("frequency") or "").lower()
    if frequency not in FREQUENCIES:
        raise NoteError(400, "Event recurrence frequency is invalid")
    interval = int(value.get("interval") or 1)
    if not 1 <= interval <= 365:
        raise NoteError(400, "Event recurrence interval must be 1-365")
    weekdays = sorted({int(day) for day in value.get("weekdays") or []})
    if any(day < 0 or day > 6 for day in weekdays):
        raise NoteError(400, "Event weekdays must be 0-6 (Monday-Sunday)")
    until = value.get("until") or None
    if until:
        try:
            date.fromisoformat(str(until))
        except ValueError as exc:
            raise NoteError(400, "Recurrence until must be YYYY-MM-DD") from exc
    count = value.get("count")
    if count is not None:
        count = int(count)
        if not 1 <= count <= 5000:
            raise NoteError(400, "Recurrence count must be 1-5000")
    exceptions: list[str] = []
    for raw in value.get("exceptions") or []:
        try:
            exceptions.append(date.fromisoformat(str(raw)).isoformat())
        except ValueError as exc:
            raise NoteError(400, "Recurrence exceptions must be YYYY-MM-DD") from exc
    return {
        "frequency": frequency,
        "interval": interval,
        "weekdays": weekdays,
        "until": str(until) if until else None,
        "count": count,
        "exceptions": sorted(set(exceptions)),
    }


class EventStore:
    """Calendar events float in the app timezone: `start`/`end` are naive wall
    times that reminders, the calendar and OMNIA read against the app's clock.
    `timezone` records the app zone when the event was saved (a time given in
    another zone is converted on save), and `start_utc`/`end_utc` and
    `occurrence_*_utc` are derived from it. Those go stale if the app zone later
    changes, and nothing reads them; they stay for clients that deserialize
    them."""

    VERSION = 1

    def __init__(self, vault_dir: Path):
        self.path = (
            vault_dir / NoteStore.INTERNAL_DIR / "calendar" / "events.json"
        )

    def _events(self) -> list[dict[str, Any]]:
        value = read_json(self.path, {})
        events = value.get("events", []) if isinstance(value, dict) else []
        return [event for event in events if isinstance(event, dict)]

    def _save(self, events: list[dict[str, Any]]) -> None:
        atomic_json(self.path, {"version": self.VERSION, "events": events})

    @staticmethod
    def _timezone(name: str) -> ZoneInfo:
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError as exc:
            raise NoteError(400, "Unknown event timezone") from exc

    def normalize(
        self,
        *,
        title: str,
        start: str,
        end: str | None,
        all_day: bool,
        timezone_name: str,
        notes: str | None,
        note_path: str | None,
        recurrence: dict[str, Any] | None,
        reminders: list[int] | None,
        app_timezone: str | None = None,
    ) -> dict[str, Any]:
        title = " ".join(title.split()).strip()
        if not title:
            raise NoteError(400, "Event title is required")
        if len(title) > 160:
            raise NoteError(400, "Event title is too long")
        tz = self._timezone(timezone_name)
        app_tz = self._timezone(app_timezone) if app_timezone else tz
        if all_day:
            try:
                start_day = date.fromisoformat(start)
                end_day = date.fromisoformat(end or start)
            except ValueError as exc:
                raise NoteError(400, "All-day event dates must be YYYY-MM-DD") from exc
            if end_day < start_day:
                raise NoteError(400, "Event end cannot be before its start")
            timing = {
                "start": start_day.isoformat(),
                "end": end_day.isoformat(),
                "start_utc": None,
                "end_utc": None,
            }
        else:
            try:
                start_local = datetime.fromisoformat(start).replace(tzinfo=None)
                end_local = datetime.fromisoformat(end or start).replace(tzinfo=None)
            except ValueError as exc:
                raise NoteError(
                    400, "Timed event values must be ISO local date-times"
                ) from exc
            if end_local <= start_local:
                raise NoteError(400, "Timed event end must be after its start")
            for value in (start_local, end_local):
                # A time in the spring-forward gap was stored with its UTC
                # instant an hour later, leaving end_utc before start_utc.
                exists = (
                    value.replace(tzinfo=tz, fold=0)
                    .astimezone(timezone.utc)
                    .astimezone(tz)
                    .replace(tzinfo=None)
                )
                if exists != value:
                    raise NoteError(
                        400,
                        f"{value:%H:%M} does not exist on {value:%Y-%m-%d} in "
                        f"{tz.key} (clocks move forward)",
                    )
            # fold=0: an ambiguous fall-back time means its first (daylight)
            # instance. Events float in the app zone, so a time given in another
            # zone is stored as the app zone's wall time for the same instant.
            start_at = start_local.replace(tzinfo=tz, fold=0).astimezone(app_tz)
            end_at = end_local.replace(tzinfo=tz, fold=0).astimezone(app_tz)
            timing = {
                "start": start_at.replace(tzinfo=None).isoformat(timespec="minutes"),
                "end": end_at.replace(tzinfo=None).isoformat(timespec="minutes"),
                "start_utc": start_at.astimezone(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
                "end_utc": end_at.astimezone(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
            }
        clean_reminders = sorted(
            {int(minutes) for minutes in reminders or []}, reverse=True
        )
        if any(minutes < 0 or minutes > 525600 for minutes in clean_reminders):
            raise NoteError(400, "Event reminders must be 0-525600 minutes")
        return {
            "title": title,
            **timing,
            "all_day": bool(all_day),
            "timezone": app_tz.key,
            "notes": (notes or "").strip()[:5000] or None,
            "note_path": note_path or None,
            "recurrence": _clean_recurrence(recurrence),
            "reminders": clean_reminders,
        }

    def add(
        self, values: dict[str, Any], now: datetime | None = None
    ) -> dict[str, Any]:
        stamp = iso_utc()
        event = {
            "id": uuid.uuid4().hex,
            **values,
            "created_at": stamp,
            "updated_at": stamp,
            "surfaced_reminders": [],
        }
        # A reminder already past delivery when the event was made is not
        # news: a daily series backdated three weeks used to push one reminder
        # per past occurrence on the next tick. One still inside its window (a
        # 3pm call with a 15-minute reminder, added at 2:50) is delivered.
        passed = self._passed_reminders(event, self._local_now(event, now))
        event["surfaced_reminders"] = [key for key, missed in passed.items() if missed]
        event["revision"] = _revision(event)
        events = self._events()
        events.append(event)
        self._save(events)
        return event

    def all(self) -> list[dict[str, Any]]:
        """The raw stored events (unexpanded), for looking one up by title."""
        return self._events()

    def get(self, event_id: str) -> dict[str, Any]:
        for event in self._events():
            if event.get("id") == event_id:
                return event
        raise NoteError(404, "Event not found")

    def update(
        self,
        event_id: str,
        values: dict[str, Any],
        expected_revision: str | None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        events = self._events()
        for event in events:
            if event.get("id") != event_id:
                continue
            if expected_revision and event.get("revision") != expected_revision:
                raise NoteError(409, "Event changed on another device; refresh and retry")
            replacement = {
                **event,
                **values,
                "updated_at": iso_utc(),
            }
            # Clearing every consumed key on an edit re-pushed each past
            # occurrence of a series after a title change. Past occurrences stay
            # consumed, and so does a delivered reminder the edit left in place;
            # other keys for ones still ahead are dropped, so a rescheduled
            # reminder fires at its new time.
            local_now = self._local_now(replacement, now)
            passed = self._passed_reminders(replacement, local_now)
            kept: set[str] = set()
            for key in event.get("surfaced_reminders") or []:
                start = _key_start(key)
                if key in passed or (start is not None and start < local_now):
                    kept.add(key)
            replacement["surfaced_reminders"] = sorted(
                kept | {key for key, missed in passed.items() if missed}
            )
            replacement["revision"] = _revision(replacement)
            event.clear()
            event.update(replacement)
            self._save(events)
            return event
        raise NoteError(404, "Event not found")

    def delete(
        self, event_id: str, expected_revision: str | None = None
    ) -> dict[str, Any]:
        events = self._events()
        for index, event in enumerate(events):
            if event.get("id") != event_id:
                continue
            if expected_revision and event.get("revision") != expected_revision:
                raise NoteError(409, "Event changed on another device; refresh and retry")
            removed = events.pop(index)
            self._save(events)
            return removed
        raise NoteError(404, "Event not found")

    @staticmethod
    def _local_now(event: dict[str, Any], now: datetime | None) -> datetime:
        """`now` as naive wall-clock time in the event's zone, the space its
        occurrence times are stored in."""
        tz = ZoneInfo(event["timezone"])
        if now is None:
            return datetime.now(tz).replace(tzinfo=None)
        if now.tzinfo is not None:
            return now.astimezone(tz).replace(tzinfo=None)
        return now

    def _passed_reminders(self, event: dict[str, Any], now: datetime) -> dict[str, bool]:
        """Surfaced-reminder keys for every reminder of `event` whose trigger is
        at or before `now` (naive local), across the window digest() emits from,
        each mapped to whether it is also past delivery.

        alerts.due_reminders pushes what digest() offers for today until
        EVENT_REMINDER_GRACE after its trigger, or until its occurrence starts
        if that is later. digest() offers a trigger from an earlier day only
        once its occurrence's day comes, where it would arrive at midnight, so
        a six-week reminder on an event added 41 days out is past too."""
        if not event.get("reminders"):
            return {}
        passed: dict[str, bool] = {}
        for occurrence in self._reminder_occurrences(now.date(), [event]):
            start_local = self._occurrence_start_local(occurrence)
            tz = ZoneInfo(occurrence["timezone"])
            for minutes in occurrence.get("reminders") or []:
                trigger = _reminder_trigger(start_local, minutes, tz)
                if trigger > now:
                    continue
                key = f"{occurrence['occurrence_start']}|{minutes}"
                offered = trigger.date() == now.date() or start_local.date() <= now.date()
                passed[key] = not offered or now > max(
                    trigger + EVENT_REMINDER_GRACE, start_local
                )
        return passed

    def _reminder_occurrences(
        self, day: date, events: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Occurrences whose reminders can matter on `day`: 30 days back for
        missed ones, and forward past the longest reminder offset. A fixed
        30-day horizon left a six-weeks-before reminder unseen until the event's
        own day, where it arrived at midnight as overdue."""
        events = [event for event in events if event.get("reminders")]
        longest = max((max(event["reminders"]) for event in events), default=0)
        ahead = min(30 + longest // 1440 + 1, 366)

        # Reminders hang off an occurrence's start. occurrences() also returns
        # one that began before the range and runs into it, which would list a
        # multi-day occurrence in both windows.
        def starting(first: date, last: date) -> list[dict[str, Any]]:
            return [
                item
                for item in self.occurrences(first, last, events)
                if item["occurrence_date"] >= first.isoformat()
            ]

        # occurrences() takes at most a year per call, and a year-long offset
        # plus the look-back is longer than that.
        return starting(
            day - timedelta(days=30), day - timedelta(days=1)
        ) + starting(day, day + timedelta(days=ahead))

    @staticmethod
    def _occurrence_start_local(occurrence: dict[str, Any]) -> datetime:
        """An occurrence's naive local start; an all-day one starts at midnight."""
        if occurrence["all_day"]:
            return datetime.combine(
                date.fromisoformat(occurrence["occurrence_date"]), time.min
            )
        return datetime.fromisoformat(occurrence["occurrence_start"])

    @staticmethod
    def _base_day(event: dict[str, Any]) -> date:
        value = str(event["start"])
        return date.fromisoformat(value[:10])

    @staticmethod
    def _span_days(event: dict[str, Any]) -> int:
        """How many days past its start day an occurrence runs: 0 for a one-day
        event. An all-day end is an inclusive date; a timed event that ends at
        midnight does not reach into the next day."""
        if event["all_day"]:
            end_day = date.fromisoformat(event["end"])
            return (end_day - EventStore._base_day(event)).days
        start_local = datetime.fromisoformat(event["start"])
        last = datetime.fromisoformat(event["end"]) - timedelta(microseconds=1)
        return max((last.date() - start_local.date()).days, 0)

    @staticmethod
    def _candidate_days(event: dict[str, Any]) -> Iterator[date]:
        base = EventStore._base_day(event)
        recurrence = event.get("recurrence")
        if not recurrence:
            yield base
            return
        frequency = recurrence["frequency"]
        interval = int(recurrence.get("interval") or 1)
        emitted = 0
        count = recurrence.get("count")
        until = (
            date.fromisoformat(recurrence["until"])
            if recurrence.get("until")
            else None
        )
        max_iterations = 20000

        if frequency == "weekly":
            weekdays = recurrence.get("weekdays") or [base.weekday()]
            week = base - timedelta(days=base.weekday())
            for iteration in range(max_iterations):
                current_week = week + timedelta(weeks=iteration * interval)
                for weekday in weekdays:
                    candidate = current_week + timedelta(days=weekday)
                    if candidate < base:
                        continue
                    if until and candidate > until:
                        return
                    yield candidate
                    emitted += 1
                    if count and emitted >= count:
                        return
            return

        for iteration in range(max_iterations):
            if frequency == "daily":
                candidate = base + timedelta(days=iteration * interval)
            elif frequency == "monthly":
                month_index = base.year * 12 + base.month - 1 + iteration * interval
                year, month_zero = divmod(month_index, 12)
                month = month_zero + 1
                if base.day > monthrange(year, month)[1]:
                    continue
                candidate = date(year, month, base.day)
            else:
                year = base.year + iteration * interval
                try:
                    candidate = date(year, base.month, base.day)
                except ValueError:
                    continue
            if until and candidate > until:
                return
            yield candidate
            emitted += 1
            if count and emitted >= count:
                return

    @staticmethod
    def _occurrence(event: dict[str, Any], day: date) -> dict[str, Any]:
        base_day = EventStore._base_day(event)
        # Every day the occurrence runs on, from its start day.
        covers = [
            (day + timedelta(days=offset)).isoformat()
            for offset in range(EventStore._span_days(event) + 1)
        ]
        if event["all_day"]:
            duration = date.fromisoformat(event["end"]) - base_day
            return {
                **event,
                "event_id": event["id"],
                "occurrence_date": day.isoformat(),
                "occurrence_start": day.isoformat(),
                "occurrence_end": (day + duration).isoformat(),
                "covers": covers,
            }

        start_local = datetime.fromisoformat(event["start"])
        end_local = datetime.fromisoformat(event["end"])
        duration = end_local - start_local
        occurrence_local = datetime.combine(day, start_local.time())
        occurrence_end = occurrence_local + duration
        tz = ZoneInfo(event["timezone"])
        # fold=0: an ambiguous fall-back time means its first (daylight) instance.
        return {
            **event,
            "event_id": event["id"],
            "occurrence_date": day.isoformat(),
            "occurrence_start": occurrence_local.isoformat(timespec="minutes"),
            "occurrence_end": occurrence_end.isoformat(timespec="minutes"),
            "occurrence_start_utc": occurrence_local.replace(tzinfo=tz, fold=0)
            .astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "occurrence_end_utc": occurrence_end.replace(tzinfo=tz, fold=0)
            .astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "covers": covers,
        }

    def occurrences(
        self, start: date, end: date, events: list[dict[str, Any]] | None = None
    ) -> list[dict[str, Any]]:
        if end < start:
            raise NoteError(400, "Calendar range end cannot be before start")
        if (end - start).days > 366:
            raise NoteError(400, "Calendar ranges are limited to one year")
        occurrences: list[dict[str, Any]] = []
        for event in events if events is not None else self._events():
            exceptions = set(
                (event.get("recurrence") or {}).get("exceptions") or []
            )
            # An occurrence is in range on any day it covers, not only its
            # start day: testing the start alone left a trip that began before
            # the range off every later day of it. One entry per occurrence,
            # dated by its start; `covers` lists its days.
            span = timedelta(days=self._span_days(event))
            for day in self._candidate_days(event):
                if day > end:
                    break
                if day + span < start or day.isoformat() in exceptions:
                    continue
                occurrences.append(self._occurrence(event, day))
        return sorted(
            occurrences,
            key=lambda item: (
                item["occurrence_date"],
                item.get("occurrence_start") or "",
                item["title"].casefold(),
            ),
        )

    def digest(
        self, day: date, mark_surfaced: bool = False, now: datetime | None = None
    ) -> dict[str, Any]:
        if mark_surfaced and now is None:
            # The fallback was a naive datetime.now(): UTC in the container,
            # hours ahead of the local times below, so the morning check
            # consumed reminders that were not due yet.
            raise TypeError("digest(mark_surfaced=True) requires now")
        # Occurrence times are stored as naive local datetimes, so compare in
        # that space — an aware `now` would raise.
        moment = now.replace(tzinfo=None) if now is not None else None
        # One snapshot for the whole pass. This used to read the file three
        # times independently and then save the *first* copy, so an event added
        # while the 60-second reminder tick was running was silently erased —
        # and a deleted one raised KeyError below.
        events = self._events()
        by_id = {event["id"]: event for event in events}
        today_events = self.occurrences(day, day, events)
        reminders: list[dict[str, Any]] = []
        # (event_id, key) pairs to consume, applied to a fresh read before save.
        to_surface: list[tuple[str, str]] = []
        day_end = datetime.combine(day, time.max)

        for occurrence in self._reminder_occurrences(day, events):
            event = by_id.get(occurrence["event_id"])
            if event is None:
                continue
            start_local = self._occurrence_start_local(occurrence)
            tz = ZoneInfo(occurrence["timezone"])
            for minutes in occurrence.get("reminders") or []:
                trigger = _reminder_trigger(start_local, minutes, tz)
                key = f"{occurrence['occurrence_start']}|{minutes}"
                surfaced = set(event.get("surfaced_reminders") or [])
                if key in surfaced:
                    continue
                if trigger.date() == day or (
                    trigger < datetime.combine(day, time.min)
                    and start_local <= day_end
                ):
                    reminders.append(
                        {
                            "event_id": occurrence["event_id"],
                            "title": occurrence["title"],
                            "occurrence_start": occurrence["occurrence_start"],
                            "minutes_before": minutes,
                            "trigger": trigger.isoformat(timespec="minutes"),
                            "overdue": trigger.date() < day,
                        }
                    )
                    # Only consume a reminder once its trigger has actually
                    # passed. The morning check runs at whatever hour the app
                    # is first opened, and marking everything surfaced there
                    # burned a "15 minutes before a 4pm lecture" reminder at
                    # 8am — delivered inside the greeting, then never again.
                    if mark_surfaced and trigger <= moment:
                        to_surface.append((occurrence["event_id"], key))
        if mark_surfaced:
            self.mark_surfaced(to_surface, day)
        return {"events_today": today_events, "reminders": reminders}

    def mark_surfaced(self, pairs: list[tuple[str, str]], day: date) -> None:
        """Consume (event_id, "occurrence_start|minutes") reminder keys and prune
        keys too old to matter.

        Split out of digest() so the reminder tick can queue an alert first and
        only then consume it; consuming first lost the reminder whenever the
        queueing step failed."""
        # Re-read and apply, so anything written while the caller's pass ran
        # survives instead of being overwritten by its opening snapshot.
        current = self._events()
        current_by_id = {event["id"]: event for event in current}
        changed = False
        for event_id, key in pairs:
            event = current_by_id.get(event_id)
            if event is None:
                continue
            surfaced = event.setdefault("surfaced_reminders", [])
            if key in surfaced:
                continue
            surfaced.append(key)
            event["revision"] = _revision(event)
            changed = True
        # A consumed key is only meaningful while its occurrence can still
        # fall inside the 30-day reminder look-back. Without this, a daily
        # recurring event accrued one key per day forever, and every save
        # re-hashed the ever-growing list.
        cutoff = (datetime.combine(day, time.min) - timedelta(days=45)).isoformat()
        for event in current:
            surfaced = event.get("surfaced_reminders")
            if not surfaced:
                continue
            kept = [k for k in surfaced if k.split("|", 1)[0] >= cutoff]
            if len(kept) != len(surfaced):
                event["surfaced_reminders"] = kept
                event["revision"] = _revision(event)
                changed = True
        if changed:
            self._save(current)
