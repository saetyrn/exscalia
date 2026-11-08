"""Git-backed time log used by daily notes."""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .vault.internal import atomic_json, iso_utc, read_json, read_versioned
from .vault.notes import NoteError, NoteStore


class ActivityStore:
    """Small append-only-style activity ledger stored inside the vault backup."""

    VERSION = 1
    # Every write rewrites the whole file, so it must not grow forever. At a
    # heavy ten entries a day this is well over a year of history.
    MAX_ENTRIES = 5000

    def __init__(self, vault_dir: Path):
        self.path = vault_dir / NoteStore.INTERNAL_DIR / "activity-log.json"

    @staticmethod
    def _dicts(value: Any) -> list[dict[str, Any]]:
        raw_entries = value.get("entries", []) if isinstance(value, dict) else []
        if not isinstance(raw_entries, list):
            return []
        return [entry for entry in raw_entries if isinstance(entry, dict)]

    @staticmethod
    def _valid(entry: dict[str, Any]) -> bool:
        return (
            isinstance(entry.get("id"), str)
            and isinstance(entry.get("date"), str)
            and isinstance(entry.get("activity"), str)
            and isinstance(entry.get("hours"), (int, float))
        )

    def _raw(self) -> list[dict[str, Any]]:
        # Writers rewrite the whole file, so they carry forward entries this
        # version cannot read (a hand edit, a newer image's shape) verbatim.
        value = read_versioned(
            self.path, {}, version=self.VERSION, name="Activity log"
        )
        return self._dicts(value)

    def _entries(self) -> list[dict[str, Any]]:
        return [
            entry
            for entry in self._dicts(read_json(self.path, {}))
            if self._valid(entry)
        ]

    def _save(self, entries: list[dict[str, Any]]) -> None:
        if len(entries) > self.MAX_ENTRIES:
            # Oldest days first; the tail is the recent history worth keeping.
            entries = sorted(entries, key=lambda e: str(e.get("date") or ""))[
                -self.MAX_ENTRIES :
            ]
        atomic_json(self.path, {"version": self.VERSION, "entries": entries})

    def add(self, day: date, activity: str, hours: float) -> dict[str, Any]:
        entry = {
            "id": uuid.uuid4().hex,
            "date": day.isoformat(),
            "activity": activity,
            "hours": round(hours, 2),
            "created_at": iso_utc(),
            "updated_at": iso_utc(),
        }
        entries = self._raw()
        entries.append(entry)
        self._save(entries)
        return entry

    def update(
        self,
        entry_id: str,
        *,
        activity: str | None = None,
        hours: float | None = None,
        expected_updated_at: str | None = None,
    ) -> dict[str, Any]:
        entries = self._raw()
        for entry in entries:
            if not self._valid(entry) or entry["id"] != entry_id:
                continue
            if (
                expected_updated_at
                and entry.get("updated_at") != expected_updated_at
            ):
                raise NoteError(
                    409, "Activity changed on another device; refresh and retry"
                )
            if activity is not None:
                entry["activity"] = activity
            if hours is not None:
                entry["hours"] = round(hours, 2)
            entry["updated_at"] = iso_utc()
            self._save(entries)
            return entry
        raise NoteError(404, "Activity entry not found")

    def delete(
        self, entry_id: str, expected_updated_at: str | None = None
    ) -> dict[str, Any]:
        entries = self._raw()
        for index, entry in enumerate(entries):
            if self._valid(entry) and entry["id"] == entry_id:
                if (
                    expected_updated_at
                    and entry.get("updated_at") != expected_updated_at
                ):
                    raise NoteError(
                        409,
                        "Activity changed on another device; refresh and retry",
                    )
                removed = entries.pop(index)
                self._save(entries)
                return removed
        raise NoteError(404, "Activity entry not found")

    @staticmethod
    def _period(
        entries: list[dict[str, Any]], start: date, end: date
    ) -> dict[str, Any]:
        totals: dict[str, dict[str, Any]] = {}
        for entry in entries:
            try:
                entry_day = date.fromisoformat(entry["date"])
            except (TypeError, ValueError):
                continue
            if not start <= entry_day <= end:
                continue
            name = entry["activity"].strip()
            key = name.casefold()
            if key not in totals:
                totals[key] = {"activity": name, "hours": 0.0}
            totals[key]["hours"] += float(entry["hours"])

        activities = sorted(
            (
                {"activity": item["activity"], "hours": round(item["hours"], 2)}
                for item in totals.values()
            ),
            key=lambda item: (-item["hours"], item["activity"].casefold()),
        )
        return {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "total_hours": round(sum(item["hours"] for item in activities), 2),
            "activities": activities,
        }

    def summary(self, day: date) -> dict[str, Any]:
        entries = self._entries()
        week_start = day - timedelta(days=day.weekday())
        week_end = week_start + timedelta(days=6)
        month_start = day.replace(day=1)
        if day.month == 12:
            next_month = day.replace(year=day.year + 1, month=1, day=1)
        else:
            next_month = day.replace(month=day.month + 1, day=1)
        month_end = next_month - timedelta(days=1)

        day_entries = sorted(
            (entry for entry in entries if entry["date"] == day.isoformat()),
            key=lambda entry: entry.get("created_at", ""),
            reverse=True,
        )
        suggestion_names: dict[str, str] = {}
        for entry in entries:
            name = entry["activity"].strip()
            if name:
                suggestion_names.setdefault(name.casefold(), name)
        suggestions = sorted(suggestion_names.values(), key=str.casefold)
        return {
            "date": day.isoformat(),
            "entries": day_entries,
            "suggestions": suggestions,
            "day": self._period(entries, day, day),
            "week": self._period(entries, week_start, week_end),
            "month": self._period(entries, month_start, month_end),
        }

    def range_totals(self, start: date, end: date) -> dict[str, float]:
        totals: dict[str, float] = {}
        for entry in self._entries():
            try:
                day = date.fromisoformat(entry["date"])
            except (TypeError, ValueError):
                continue
            if start <= day <= end:
                key = day.isoformat()
                totals[key] = round(
                    totals.get(key, 0.0) + float(entry["hours"]), 2
                )
        return totals
