"""The Done column is weekly: it holds everything finished during the week and
clears once Sunday night passes."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import tasks as tasks_module
from app.vault.internal import iso_utc

TZ = "America/New_York"


def make_client(state) -> TestClient:
    app = FastAPI()
    app.state = state
    app.state.timezone = TZ
    app.include_router(tasks_module.router)
    return TestClient(app)


async def seed(state, task_id: str, status: str, completed_at: str | None):
    await state.db.execute(
        "INSERT INTO tasks (id, path, line, text, status, completed_at)"
        " VALUES (?, 'Tasks Inbox.md', 1, ?, ?, ?)",
        (task_id, f"task {task_id}", status, completed_at),
    )
    await state.db.commit()


def week_start_local() -> datetime:
    now = datetime.now(ZoneInfo(TZ))
    return (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


@pytest.mark.asyncio
async def test_done_column_holds_this_week_and_drops_earlier_weeks(state):
    start = week_start_local()
    await seed(state, "this-week", "done", iso_utc((start + timedelta(minutes=5)).astimezone(timezone.utc)))
    await seed(state, "last-week", "done", iso_utc((start - timedelta(days=2)).astimezone(timezone.utc)))
    await seed(state, "open", "todo", None)

    client = make_client(state)
    visible = {t["id"] for t in client.get("/api/tasks").json()}
    # Completed earlier in the week stays as a record of the week's work…
    assert "this-week" in visible
    # …and anything from before Monday has cleared.
    assert "last-week" not in visible
    # Unfinished work is never filtered by date.
    assert "open" in visible

    everything = {t["id"] for t in client.get("/api/tasks?include_completed=true").json()}
    assert {"this-week", "last-week", "open"} <= everything


@pytest.mark.asyncio
async def test_week_start_is_monday_midnight_in_the_users_timezone(state):
    client = make_client(state)
    # A task completed one minute before this week began must not show; one
    # minute after must. This pins the boundary to local Sunday midnight.
    start = week_start_local()
    await seed(state, "just-before", "done", iso_utc((start - timedelta(minutes=1)).astimezone(timezone.utc)))
    await seed(state, "just-after", "done", iso_utc((start + timedelta(minutes=1)).astimezone(timezone.utc)))

    visible = {t["id"] for t in client.get("/api/tasks").json()}
    assert "just-after" in visible
    assert "just-before" not in visible
