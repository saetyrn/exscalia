"""OMNIA's board, reorder, move-task, and reminder tools.

These close the gaps a user kept hitting — OMNIA could create and update tasks
but could not lay out a board, pin a column's order, relocate a task, or set a
plain reminder. Each tool takes the git-scoped, undoable path via `_record`.
"""
import asyncio
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

    after = alerts.due_reminders(omnia_state, datetime(2020, 1, 1, 10, 0, tzinfo=tz))
    assert any(a["title"] == "call the dentist" for a in after)


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
