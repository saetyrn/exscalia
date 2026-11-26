"""OMNIA's board, reorder, move-task, and reminder tools.

These close the gaps a user kept hitting — OMNIA could create and update tasks
but could not lay out a board, pin a column's order, relocate a task, or set a
plain reminder. Each tool takes the git-scoped, undoable path via `_record`.
"""
import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app import alerts
from app.event_store import EventStore
from app.overseer.tool_expansion import (
    create_reminder,
    create_task,
    create_task_board,
    move_task,
    reorder_tasks,
    set_task_reminder,
)
from app.task_service import move_tasks, update_task as update_task_record
from app.vault.notes import NoteError

INBOX = "Tasks Inbox.md"


@pytest.fixture
def omnia_state(acting_state):
    acting_state.timezone = "UTC"
    acting_state.events = EventStore(acting_state.settings.vault_dir)
    return acting_state


def add_task(state, text: str, path: str = INBOX) -> dict:
    return asyncio.run(create_task(state, "t", {"text": text, "path": path}))["task"]


async def task_row(state, task_id: str) -> dict | None:
    cur = await state.db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
    row = await cur.fetchone()
    return dict(row) if row else None


# --- create_task_board -----------------------------------------------------


def test_create_task_board_gives_a_folder_its_own_board(omnia_state):
    omnia_state.store.create_folder("projects")

    result = asyncio.run(create_task_board(omnia_state, "t", {"folder": "projects"}))

    assert result["path"] == "projects/Tasks.md"
    content = omnia_state.store.read("projects/Tasks.md")
    assert "task_board: true" in content


def test_create_task_board_rejects_a_missing_folder(omnia_state):
    with pytest.raises(NoteError):
        asyncio.run(create_task_board(omnia_state, "t", {"folder": "nope"}))


def test_create_task_board_refuses_a_second_board_for_the_same_folder(omnia_state):
    omnia_state.store.create_folder("projects")
    asyncio.run(create_task_board(omnia_state, "t", {"folder": "projects"}))

    with pytest.raises(NoteError):
        asyncio.run(create_task_board(omnia_state, "t", {"folder": "projects"}))


def test_create_task_board_canonicalizes_folder_casing(omnia_state):
    omnia_state.store.create_folder("Projects")

    result = asyncio.run(create_task_board(omnia_state, "t", {"folder": "projects"}))

    assert result["path"] == "Projects/Tasks.md"


# --- reorder_tasks ---------------------------------------------------------


def test_reorder_tasks_pins_the_column_order(omnia_state):
    first = add_task(omnia_state, "first")
    second = add_task(omnia_state, "second")
    third = add_task(omnia_state, "third")

    asyncio.run(
        reorder_tasks(
            omnia_state, "t", {"task_ids": [third["id"], first["id"], second["id"]]}
        )
    )

    ranks = {
        task_id: asyncio.run(task_row(omnia_state, task_id))["sort"]
        for task_id in (first["id"], second["id"], third["id"])
    }
    assert ranks[third["id"]] < ranks[first["id"]] < ranks[second["id"]]


# --- move_task -------------------------------------------------------------


def test_move_task_relocates_keeping_its_id(omnia_state):
    omnia_state.store.create_folder("projects")
    board = asyncio.run(create_task_board(omnia_state, "t", {"folder": "projects"}))[
        "path"
    ]
    task = add_task(omnia_state, "ship it")

    result = asyncio.run(
        move_task(omnia_state, "t", {"task_id": task["id"], "target_path": board})
    )

    assert result["task"]["id"] == task["id"]
    assert result["task"]["path"] == board
    # The task left its source note and now lives on the board.
    assert task["id"] not in omnia_state.store.read(INBOX)
    row = asyncio.run(task_row(omnia_state, task["id"]))
    assert row["path"] == board


def test_move_task_to_a_missing_note_is_rejected(omnia_state):
    task = add_task(omnia_state, "stay put")
    with pytest.raises(NoteError):
        asyncio.run(
            move_task(
                omnia_state,
                "t",
                {"task_id": task["id"], "target_path": "ghost/Tasks.md"},
            )
        )


def make_board(state) -> str:
    state.store.create_folder("projects")
    return asyncio.run(create_task_board(state, "t", {"folder": "projects"}))["path"]


def test_move_tasks_writes_each_note_once(omnia_state, monkeypatch):
    """Moving N tasks between two notes is two writes, not 2N — target first,
    so an interruption can only duplicate a task, never drop it."""
    board = make_board(omnia_state)
    tasks = [add_task(omnia_state, f"batch {n}") for n in range(3)]
    written: list[str] = []
    real = omnia_state.store.write

    def counting(path, content, **kwargs):
        written.append(path)
        return real(path, content, **kwargs)

    monkeypatch.setattr(omnia_state.store, "write", counting)
    moved = asyncio.run(move_tasks(omnia_state, [t["id"] for t in tasks], board))

    assert written == [board, INBOX]
    assert [t["id"] for t in moved] == [t["id"] for t in tasks]
    assert {t["path"] for t in moved} == {board}
    inbox = omnia_state.store.read(INBOX)
    content = omnia_state.store.read(board)
    assert not any(t["id"] in inbox for t in tasks)
    assert content.index("batch 0") < content.index("batch 1") < content.index("batch 2")
    rows = [asyncio.run(task_row(omnia_state, t["id"])) for t in tasks]
    assert {row["path"] for row in rows} == {board}
    assert all(row["sort"] is None for row in rows)


def test_a_failed_source_write_leaves_a_duplicate_not_a_loss(omnia_state, monkeypatch):
    board = make_board(omnia_state)
    task = add_task(omnia_state, "precious")
    real = omnia_state.store.write
    calls: list[str] = []

    def flaky(path, content, **kwargs):
        calls.append(path)
        if len(calls) == 2:
            raise OSError("simulated write failure")
        return real(path, content, **kwargs)

    monkeypatch.setattr(omnia_state.store, "write", flaky)
    with pytest.raises(OSError):
        asyncio.run(move_tasks(omnia_state, [task["id"]], board))
    monkeypatch.undo()

    assert "precious" in omnia_state.store.read(INBOX)
    assert "precious" in omnia_state.store.read(board)
    asyncio.run(omnia_state.indexer.sync_all(force=True))
    rows = asyncio.run(
        omnia_state.db.execute_fetchall("SELECT id FROM tasks WHERE id = ?", (task["id"],))
    )
    assert len(rows) == 1


def test_move_tasks_validates_every_task_before_writing(omnia_state, monkeypatch):
    board = make_board(omnia_state)
    good = add_task(omnia_state, "good one")
    other = add_task(omnia_state, "moves alone", path="Other.md")
    # Remove the second task's line behind the index's back.
    content = omnia_state.store.read("Other.md")
    omnia_state.store.write(
        "Other.md",
        "\n".join(line for line in content.splitlines() if "moves alone" not in line) + "\n",
    )
    before = {path: omnia_state.store.read(path) for path in (INBOX, "Other.md", board)}
    with pytest.raises(NoteError) as caught:
        asyncio.run(move_tasks(omnia_state, [good["id"], other["id"]], board))
    assert caught.value.status == 409
    assert {path: omnia_state.store.read(path) for path in before} == before


# --- create_reminder -------------------------------------------------------


def test_create_reminder_stores_a_reminding_event(omnia_state):
    result = asyncio.run(
        create_reminder(
            omnia_state,
            "t",
            {"text": "call the dentist", "when": "2020-01-01T09:00"},
        )
    )

    event = result["event"]
    assert event["title"] == "call the dentist"
    assert event["reminders"] == [0]
    assert omnia_state.events.get(event["id"])["title"] == "call the dentist"


def test_create_reminder_fires_only_after_its_time(omnia_state):
    asyncio.run(
        create_reminder(
            omnia_state,
            "t",
            {"text": "call the dentist", "when": "2020-01-01T09:00"},
        )
    )
    tz = ZoneInfo("UTC")

    before = alerts.due_reminders(omnia_state, datetime(2020, 1, 1, 8, 0, tzinfo=tz))
    assert not any(a["title"] == "call the dentist" for a in before)

    # Pushed while it is still timely; an hour late it would be left for the
    # morning greeting instead (EVENT_REMINDER_GRACE).
    after = alerts.due_reminders(omnia_state, datetime(2020, 1, 1, 9, 10, tzinfo=tz))
    assert any(a["title"] == "call the dentist" for a in after)


def test_a_date_only_reminder_fires_that_morning_not_at_midnight(omnia_state):
    """'Remind me Friday to call mom' was an all-day event with a [0] alert, so
    the push arrived at 00:00."""
    event = asyncio.run(
        create_reminder(omnia_state, "t", {"text": "call mom", "when": "2030-05-01"})
    )["event"]

    assert event["all_day"] is False
    assert (event["start"], event["end"]) == ("2030-05-01T09:00", "2030-05-01T09:15")
    tz = ZoneInfo("UTC")
    night = alerts.due_reminders(omnia_state, datetime(2030, 5, 1, 0, 30, tzinfo=tz))
    assert not any(a["title"] == "call mom" for a in night)
    morning = alerts.due_reminders(omnia_state, datetime(2030, 5, 1, 9, 5, tzinfo=tz))
    assert any(a["title"] == "call mom" for a in morning)


def test_a_reminder_with_an_offset_is_stored_in_local_time(omnia_state):
    omnia_state.timezone = "America/New_York"

    event = asyncio.run(
        create_reminder(omnia_state, "t", {"text": "standup", "when": "2030-05-01T14:00Z"})
    )["event"]

    # 14:00Z is 10:00 EDT. Dropping the Z made it fire at 14:00 local.
    assert event["start"] == "2030-05-01T10:00"
    assert event["timezone"] == "America/New_York"


# --- set_task_reminder -----------------------------------------------------


def test_set_task_reminder_stores_the_trigger(omnia_state):
    task = add_task(omnia_state, "pay rent")

    result = asyncio.run(
        set_task_reminder(
            omnia_state, "t", {"task_id": task["id"], "remind_at": "2020-01-01T09:00"}
        )
    )

    assert result["task"]["reminder_at"] == "2020-01-01T09:00"
    assert asyncio.run(task_row(omnia_state, task["id"]))["reminder_at"] == "2020-01-01T09:00"


def test_set_task_reminder_can_be_relative_to_the_due_date(omnia_state):
    task = asyncio.run(
        create_task(omnia_state, "t", {"text": "submit", "path": INBOX, "due": "2020-06-15"})
    )["task"]

    result = asyncio.run(
        set_task_reminder(
            omnia_state, "t", {"task_id": task["id"], "days_before": 1, "at_time": "08:00"}
        )
    )

    assert result["task"]["reminder_at"] == "2020-06-14T08:00"


@pytest.mark.parametrize(
    "remind_at, stored",
    [
        ("2030-05-01T14:00Z", "2030-05-01T10:00"),  # the ticker dropped the offset
        ("2030-05-01T14:00:00-04:00", "2030-05-01T14:00"),
        ("2030-05-01", "2030-05-01T09:00"),  # fired at 00:00
    ],
)
def test_task_reminder_is_stored_as_local_minutes(omnia_state, remind_at, stored):
    omnia_state.timezone = "America/New_York"
    task = add_task(omnia_state, "pay rent")

    result = asyncio.run(
        set_task_reminder(omnia_state, "t", {"task_id": task["id"], "remind_at": remind_at})
    )

    assert result["task"]["reminder_at"] == stored
    assert asyncio.run(task_row(omnia_state, task["id"]))["reminder_at"] == stored


def test_a_relative_reminder_needs_a_due_date(omnia_state):
    task = add_task(omnia_state, "someday")
    with pytest.raises(NoteError):
        asyncio.run(set_task_reminder(omnia_state, "t", {"task_id": task["id"]}))


def test_task_reminder_fires_once_after_its_time(omnia_state):
    task = add_task(omnia_state, "pay rent")
    asyncio.run(
        set_task_reminder(
            omnia_state, "t", {"task_id": task["id"], "remind_at": "2020-01-01T09:00"}
        )
    )
    tz = ZoneInfo("UTC")

    before = asyncio.run(
        alerts.task_due_reminders(omnia_state, datetime(2020, 1, 1, 8, 0, tzinfo=tz))
    )
    assert not before

    after = asyncio.run(
        alerts.task_due_reminders(omnia_state, datetime(2020, 1, 1, 10, 0, tzinfo=tz))
    )
    assert [a["title"] for a in after] == ["pay rent"]

    # Delivered once: a later tick must not repeat it.
    again = asyncio.run(
        alerts.task_due_reminders(omnia_state, datetime(2020, 1, 1, 10, 1, tzinfo=tz))
    )
    assert not again

    # The ledger records when it was delivered, not the reminder's own time, so
    # its retention measures time since delivery.
    assert task_ledger(omnia_state) == {
        f"task:{task['id']}:2020-01-01T09:00": "2020-01-01T10:00:00"
    }


def task_ledger(state) -> dict:
    path = alerts.TaskReminderLog(state.settings.data_dir).path
    return json.loads(path.read_text()) if path.exists() else {}


def remind(state, text: str, remind_at: str) -> dict:
    task = add_task(state, text)
    asyncio.run(
        set_task_reminder(state, "t", {"task_id": task["id"], "remind_at": remind_at})
    )
    return task


def tick(state, now: datetime) -> list[str]:
    return [a["title"] for a in asyncio.run(alerts.task_due_reminders(state, now))]


def test_a_task_reminder_past_its_retention_does_not_refire_every_tick(omnia_state):
    """be-omnia-background#2: the ledger stored the reminder's own time and
    pruned by it, so a reminder 100 days old was written and dropped in the same
    call and re-fired on every 60-second tick while the task stayed open."""
    task = remind(omnia_state, "pay rent", "2020-01-01T09:00")
    now = datetime(2020, 4, 10, 9, 0, tzinfo=ZoneInfo("UTC"))

    assert tick(omnia_state, now) == []
    assert tick(omnia_state, now) == []
    assert not any(task["id"] in key for key in task_ledger(omnia_state))


def test_a_pruned_ledger_entry_cannot_bring_an_old_reminder_back(omnia_state):
    """A prod ledger written before the fix holds remind_at values. Once another
    reminder's delivery prunes the stale key, nothing may re-fire it."""
    stale = remind(omnia_state, "pay rent", "2020-01-01T09:00")
    stale_key = f"task:{stale['id']}:2020-01-01T09:00"
    path = alerts.TaskReminderLog(omnia_state.settings.data_dir).path
    path.write_text(json.dumps({stale_key: "2020-01-01T09:00"}))
    remind(omnia_state, "call the bank", "2020-04-10T08:30")
    now = datetime(2020, 4, 10, 9, 0, tzinfo=ZoneInfo("UTC"))

    assert tick(omnia_state, now) == ["call the bank"]
    assert stale_key not in task_ledger(omnia_state)
    assert tick(omnia_state, now) == []
    assert tick(omnia_state, datetime(2020, 4, 10, 9, 1, tzinfo=ZoneInfo("UTC"))) == []


def test_a_task_reminder_days_late_is_not_delivered(omnia_state):
    """A missing or quarantined ledger replays at most the last day, not every
    reminder ever set on an open task (gap-migrations-restore#7)."""
    remind(omnia_state, "pay rent", "2020-01-08T09:00")

    assert tick(omnia_state, datetime(2020, 1, 10, 9, 0, tzinfo=ZoneInfo("UTC"))) == []


def test_clearing_a_task_reminder_stops_it(omnia_state):
    task = add_task(omnia_state, "pay rent")
    asyncio.run(
        set_task_reminder(
            omnia_state, "t", {"task_id": task["id"], "remind_at": "2020-01-01T09:00"}
        )
    )
    asyncio.run(set_task_reminder(omnia_state, "t", {"task_id": task["id"], "clear": True}))

    assert not asyncio.run(task_row(omnia_state, task["id"]))["reminder_at"]
    after = asyncio.run(
        alerts.task_due_reminders(
            omnia_state, datetime(2020, 1, 1, 10, 0, tzinfo=ZoneInfo("UTC"))
        )
    )
    assert not after


def test_moving_the_due_date_moves_the_reminder(omnia_state):
    """gap-time-dst#6: 'push the essay to next week' left the reminder on the
    old date, so it fired a week early and then never again."""
    task = asyncio.run(
        create_task(omnia_state, "t", {"text": "essay", "path": INBOX, "due": "2030-09-24"})
    )["task"]
    asyncio.run(
        set_task_reminder(
            omnia_state, "t", {"task_id": task["id"], "days_before": 1, "at_time": "18:00"}
        )
    )

    moved = asyncio.run(update_task_record(omnia_state, task["id"], due="2030-10-01"))
    assert moved["reminder_at"] == "2030-09-30T18:00"

    # Clearing the due date keeps the reminder where it is.
    cleared = asyncio.run(update_task_record(omnia_state, task["id"], due=None))
    assert cleared["due"] is None
    assert cleared["reminder_at"] == "2030-09-30T18:00"
