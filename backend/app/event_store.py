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
    ) -> dict[str, Any]:
        title = " ".join(title.split()).strip()
        if not title:
            raise NoteError(400, "Event title is required")
        if len(title) > 160:
            raise NoteError(400, "Event title is too long")
        tz = self._timezone(timezone_name)
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
            timing = {
                "start": start_local.isoformat(timespec="minutes"),
                "end": end_local.isoformat(timespec="minutes"),
                "start_utc": start_local.replace(tzinfo=tz)
                .astimezone(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
                "end_utc": end_local.replace(tzinfo=tz)
                .astimezone(timezone.utc)
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
            "timezone": tz.key,
            "notes": (notes or "").strip()[:5000] or None,
            "note_path": note_path or None,
            "recurrence": _clean_recurrence(recurrence),
            "reminders": clean_reminders,
        }

    def add(self, values: dict[str, Any]) -> dict[str, Any]:
        now = iso_utc()
        event = {
            "id": uuid.uuid4().hex,
            **values,
            "created_at": now,
            "updated_at": now,
            "surfaced_reminders": [],
        }
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
                "surfaced_reminders": [],
            }
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
    def _base_day(event: dict[str, Any]) -> date:
        value = str(event["start"])
        return date.fromisoformat(value[:10])

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
        cursor = base
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
        if event["all_day"]:
            duration = date.fromisoformat(event["end"]) - base_day
            return {
                **event,
                "event_id": event["id"],
                "occurrence_date": day.isoformat(),
                "occurrence_start": day.isoformat(),
                "occurrence_end": (day + duration).isoformat(),
            }

        start_local = datetime.fromisoformat(event["start"])
        end_local = datetime.fromisoformat(event["end"])
        duration = end_local - start_local
        occurrence_local = datetime.combine(day, start_local.time())
        occurrence_end = occurrence_local + duration
        tz = ZoneInfo(event["timezone"])
        return {
            **event,
            "event_id": event["id"],
            "occurrence_date": day.isoformat(),
            "occurrence_start": occurrence_local.isoformat(timespec="minutes"),
            "occurrence_end": occurrence_end.isoformat(timespec="minutes"),
            "occurrence_start_utc": occurrence_local.replace(tzinfo=tz)
            .astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "occurrence_end_utc": occurrence_end.replace(tzinfo=tz)
            .astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
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
            for day in self._candidate_days(event):
                if day > end:
                    break
                if day < start or day.isoformat() in exceptions:
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
        # One snapshot for the whole pass. This used to read the file three
        # times independently and then save the *first* copy, so an event added
        # while the 60-second reminder tick was running was silently erased —
        # and a deleted one raised KeyError below.
        events = self._events()
        by_id = {event["id"]: event for event in events}
        today_events = self.occurrences(day, day, events)
        window_start = day - timedelta(days=30)
        window_end = day + timedelta(days=30)
        reminders: list[dict[str, Any]] = []
        # (event_id, key) pairs to consume, applied to a fresh read before save.
        to_surface: list[tuple[str, str]] = []
        day_end = datetime.combine(day, time.max)

        for occurrence in self.occurrences(window_start, window_end, events):
            event = by_id.get(occurrence["event_id"])
            if event is None:
                continue
            if occurrence["all_day"]:
                start_local = datetime.combine(
                    date.fromisoformat(occurrence["occurrence_date"]), time.min
                )
            else:
                start_local = datetime.fromisoformat(
                    occurrence["occurrence_start"]
                )
            for minutes in occurrence.get("reminders") or []:
                trigger = start_local - timedelta(minutes=minutes)
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
                            "overdue": trigger.date() < day,
                        }
                    )
                    # Only consume a reminder once its trigger has actually
                    # passed. The morning check runs at whatever hour the app
                    # is first opened, and marking everything surfaced there
                    # burned a "15 minutes before a 4pm lecture" reminder at
                    # 8am — delivered inside the greeting, then never again.
                    # Occurrence times are stored as naive local datetimes, so
                    # compare in that space — an aware `now` would raise.
                    moment = now or datetime.now()
                    if moment.tzinfo is not None:
                        moment = moment.replace(tzinfo=None)
                    if mark_surfaced and trigger <= moment:
                        to_surface.append((occurrence["event_id"], key))
        if mark_surfaced:
            # Re-read and apply, so anything written while this pass ran
            # survives instead of being overwritten by the opening snapshot.
            current = self._events()
            current_by_id = {event["id"]: event for event in current}
            changed = False
            for event_id, key in to_surface:
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
            # fall inside the ±30-day reminder window. Without this, a daily
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
        return {"events_today": today_events, "reminders": reminders}
