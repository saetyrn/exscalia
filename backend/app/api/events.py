"""Standalone calendar events."""

from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from ..vault.notes import NoteError

router = APIRouter(prefix="/api/events", tags=["events"])


class EventBody(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    start: str
    end: str | None = None
    all_day: bool = True
    timezone: str | None = None
    notes: str | None = Field(default=None, max_length=5000)
    note_path: str | None = None
    recurrence: dict[str, Any] | None = None
    reminders: list[int] = Field(default_factory=list)


class EventUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=160)
    start: str | None = None
    end: str | None = None
    all_day: bool | None = None
    timezone: str | None = None
    notes: str | None = Field(default=None, max_length=5000)
    note_path: str | None = None
    recurrence: dict[str, Any] | None = None
    reminders: list[int] | None = None
    expected_revision: str | None = None


def _range(start: str, end: str) -> tuple[date, date]:
    try:
        return date.fromisoformat(start), date.fromisoformat(end)
    except ValueError as exc:
        raise NoteError(400, "Event ranges must use YYYY-MM-DD") from exc


def _values(request: Request, body: EventBody) -> dict[str, Any]:
    state = request.app.state
    if body.note_path and not state.store.exists(body.note_path):
        raise NoteError(404, f"Linked note not found: {body.note_path}")
    return state.events.normalize(
        title=body.title,
        start=body.start,
        end=body.end,
        all_day=body.all_day,
        timezone_name=body.timezone or state.timezone,
        notes=body.notes,
        note_path=body.note_path,
        recurrence=body.recurrence,
        reminders=body.reminders,
        app_timezone=state.timezone,
    )


@router.get("")
async def list_events(request: Request, start: str, end: str):
    first, last = _range(start, end)
    return {"events": request.app.state.events.occurrences(first, last)}


@router.post("")
async def create_event(request: Request, body: EventBody):
    event = request.app.state.events.add(_values(request, body))
    request.app.state.autocommit.touch()
    return event


@router.patch("/{event_id}")
async def update_event(request: Request, event_id: str, body: EventUpdate):
    state = request.app.state
    existing = state.events.get(event_id)
    supplied = body.model_dump(exclude_unset=True)
    supplied.pop("expected_revision", None)
    merged = {
        key: supplied[key] if key in supplied else existing.get(key)
        for key in (
            "title",
            "start",
            "end",
            "all_day",
            "notes",
            "note_path",
            "recurrence",
            "reminders",
        )
    }
    if (
        "start" in supplied
        and "end" not in supplied
        and not existing.get("all_day")
        and not merged["all_day"]
    ):
        # A start-only move keeps the event's length; the stored end used to
        # stay put, stretching the event or landing before its new start.
        try:
            start = datetime.fromisoformat(merged["start"])
            span = datetime.fromisoformat(existing["end"]) - datetime.fromisoformat(
                existing["start"]
            )
        except (TypeError, ValueError) as exc:
            raise NoteError(400, "Timed event values must be ISO local date-times") from exc
        merged["end"] = (start + span).isoformat(timespec="minutes")
    values = state.events.normalize(
        title=merged["title"],
        start=merged["start"],
        end=merged["end"],
        all_day=merged["all_day"],
        # Not the stored zone: events float in the app zone, and re-reading the
        # stored one after a settings change shifted a title-only edit's time.
        timezone_name=supplied.get("timezone") or state.timezone,
        notes=merged["notes"],
        note_path=merged["note_path"],
        recurrence=merged["recurrence"],
        reminders=merged["reminders"],
        app_timezone=state.timezone,
    )
    event = request.app.state.events.update(
        event_id, values, body.expected_revision
    )
    request.app.state.autocommit.touch()
    return event


@router.delete("/{event_id}")
async def delete_event(
    request: Request, event_id: str, expected_revision: str | None = None
):
    request.app.state.events.delete(event_id, expected_revision)
    request.app.state.autocommit.touch()
    return {"ok": True}
