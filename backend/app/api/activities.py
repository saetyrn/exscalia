"""Daily-note activity logging and hour summaries."""

from datetime import date

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, model_validator

from ..vault.notes import NoteError

router = APIRouter(prefix="/api/activities", tags=["activities"])


def _day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise NoteError(400, "Date must use YYYY-MM-DD")


def _activity(value: str) -> str:
    normalized = " ".join(value.split())
    if not normalized:
        raise NoteError(400, "Activity name is required")
    return normalized


class CreateActivity(BaseModel):
    date: str
    activity: str = Field(min_length=1, max_length=80)
    hours: float = Field(gt=0, le=24)


class UpdateActivity(BaseModel):
    activity: str | None = Field(default=None, min_length=1, max_length=80)
    hours: float | None = Field(default=None, gt=0, le=24)
    expected_updated_at: str | None = None

    @model_validator(mode="after")
    def has_change(self):
        if self.activity is None and self.hours is None:
            raise ValueError("Provide an activity or hours")
        return self


@router.get("")
async def activity_summary(request: Request, date: str):
    return request.app.state.activities.summary(_day(date))


@router.post("")
async def create_activity(request: Request, body: CreateActivity):
    day = _day(body.date)
    entry = request.app.state.activities.add(
        day, _activity(body.activity), body.hours
    )
    request.app.state.autocommit.touch()
    return entry


@router.patch("/{entry_id}")
async def update_activity(
    request: Request, entry_id: str, body: UpdateActivity
):
    entry = request.app.state.activities.update(
        entry_id,
        activity=_activity(body.activity) if body.activity is not None else None,
        hours=body.hours,
        expected_updated_at=body.expected_updated_at,
    )
    request.app.state.autocommit.touch()
    return entry


@router.delete("/{entry_id}")
async def delete_activity(
    request: Request, entry_id: str, expected_updated_at: str | None = None
):
    request.app.state.activities.delete(entry_id, expected_updated_at)
    request.app.state.autocommit.touch()
    return {"ok": True}
