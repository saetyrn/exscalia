"""The Done column is weekly: it holds everything finished during the week and
clears once Sunday night passes."""
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import calendar as calendar_api
from app.api import tasks as tasks_module
from app.index import parse
from app.task_service import create_task, update_task
from app.vault.internal import iso_utc, parse_utc

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


def rewrite_meta(state, task: dict, **fields) -> dict:
    """Edit a task's metadata comment in the markdown itself, as a hand edit
    or an older build would have left it."""
    content = state.store.read(task["path"])
    for line in content.splitlines():
        match = parse.TASK_META_RE.match(line)
        if match and json.loads(match.group(1)).get("id") == task["id"]:
            meta = {**json.loads(match.group(1)), **fields}
            return content.replace(line, parse.task_meta_line(meta)), meta
    raise AssertionError(f"no metadata for {task['id']}")


@pytest.mark.asyncio
async def test_completing_again_after_a_reopen_counts_as_done_now(state):
    """be-tasks-calendar-misc#6: completed_at was only ever written once, so a
    task finished last week, reopened and finished again today filed under the
    old date and dropped straight off this week's Done column."""
    task = await create_task(state, text="file the report")
    await update_task(state, task["id"], status="done")
    ten_days_ago = iso_utc(datetime.now(timezone.utc) - timedelta(days=10))
    content, _ = rewrite_meta(state, task, completed_at=ten_days_ago)
    await state.indexer.index_note(task["path"], state.store.write(task["path"], content))

    client = make_client(state)
    assert task["id"] not in {t["id"] for t in client.get("/api/tasks").json()}

    reopened = await update_task(state, task["id"], status="todo")
    assert reopened["reopened_at"]
    redone = await update_task(state, task["id"], status="done")

    assert datetime.now(timezone.utc) - parse_utc(redone["completed_at"]) < timedelta(minutes=1)
    assert redone["reopened_at"] == reopened["reopened_at"]
    _, meta = rewrite_meta(state, task)
    assert meta["first_completed_at"] == ten_days_ago
    assert task["id"] in {t["id"] for t in client.get("/api/tasks").json()}

    # Every reopen is stamped, not just the first.
    again = await update_task(state, task["id"], status="todo")
    assert parse_utc(again["reopened_at"]) >= parse_utc(redone["completed_at"])


def completed_events(row: dict) -> list[dict]:
    events = calendar_api._events([row], ZoneInfo("UTC"))
    return [e for e in events if e["type"] == "completed"]


def test_calendar_shows_no_completion_for_a_reopened_task():
    row = {
        "id": "t1",
        "text": "file the report",
        "status": "todo",
        "created_at": "2030-01-01T12:00:00Z",
        "completed_at": "2030-01-02T12:00:00Z",
        "reopened_at": "2030-01-03T12:00:00Z",
        "due": None,
        "due_visible": 0,
        "board": 0,
    }
    assert completed_events(row) == []
    assert [e["date"] for e in completed_events({**row, "status": "done"})] == ["2030-01-02"]
    # A deleted task's tombstone carries its last status too.
    assert completed_events({**row, "tombstone": True}) == []


NOW = "2030-01-09T00:00:00Z"


def note(meta: dict, status_char: str) -> str:
    return parse.task_meta_line({"id": "t1", **meta}) + f"\n- [{status_char}] file the report\n"


def saved(meta: dict, before: str | None, after: str) -> dict:
    """The task's metadata once a save turns its checkbox from ``before`` into
    ``after`` (None: the note is new, as create_note and the migration see it)."""
    previous = None if before is None else note(meta, before)
    content, _ = parse.normalize_task_metadata(
        note(meta, after), now=NOW, previous_content=previous
    )
    [task] = parse.extract_tasks(content, 0)
    match = parse.TASK_META_RE.match(content.splitlines()[task.meta_line - 1])
    return json.loads(match.group(1))


def test_editor_checking_a_reopened_task_restamps_completion():
    meta = {"completed_at": "2030-01-01T00:00:00Z", "reopened_at": "2030-01-05T00:00:00Z"}

    done = saved(meta, " ", "x")

    assert done["completed_at"] == NOW
    assert done["first_completed_at"] == "2030-01-01T00:00:00Z"
    assert done["reopened_at"] == "2030-01-05T00:00:00Z"
    # Re-saving the checked box is a no-op.
    assert saved(done, "x", "x") == done


def test_editor_unchecking_a_completed_task_stamps_the_reopen():
    never_reopened = saved({"completed_at": "2030-01-01T00:00:00Z"}, "x", " ")
    assert never_reopened["reopened_at"] == NOW
    assert never_reopened["completed_at"] == "2030-01-01T00:00:00Z"

    reopened_before = saved(
        {"completed_at": "2030-01-05T00:00:00Z", "reopened_at": "2030-01-01T00:00:00Z"}, "x", "/"
    )
    assert reopened_before["reopened_at"] == NOW
    assert saved(reopened_before, "/", "/") == reopened_before


def test_resaving_a_legacy_recompleted_task_keeps_its_completion_date():
    """Before update_task restamped every transition, it stamped only the
    first completion and the first reopen, so a task completed, reopened and
    completed again is stored done with reopened_at after completed_at. A save
    that leaves its box checked must not read that as a fresh completion."""
    legacy = {"completed_at": "2030-01-01T00:00:00Z", "reopened_at": "2030-01-05T00:00:00Z"}

    for before in ("x", None):
        kept = saved(legacy, before, "x")
        assert kept["completed_at"] == "2030-01-01T00:00:00Z"
        assert "first_completed_at" not in kept
