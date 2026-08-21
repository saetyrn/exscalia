"""Capture: a stated commitment must become something actionable.

The failure this prevents was observed live — "Tommorow im going to the library
with mark" produced prose in a daily note and no task, no event, no reminder.
"""

import asyncio
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.overseer.tool_expansion import capture
from app.vault.notes import NoteError


def _today(state) -> date:
    """Capture resolves "today" in the app's timezone, not the machine's."""
    return datetime.now(ZoneInfo(state.timezone)).date()


@pytest.fixture
def cap_state(acting_state, tmp_path):
    """acting_state plus the calendar/timezone wiring capture needs."""
    from app.event_store import EventStore

    acting_state.timezone = "UTC"
    acting_state.events = EventStore(acting_state.settings.vault_dir)
    return acting_state


def test_event_capture_creates_a_calendar_entry_and_a_daily_line(cap_state):
    when = (_today(cap_state) + timedelta(days=1)).isoformat()

    result = asyncio.run(
        capture(
            cap_state,
            "t",
            {"text": "Library with Mark", "kind": "event", "when": f"{when}T15:00"},
        )
    )

    assert result["captured"] == "event"
    events = cap_state.events.all() if hasattr(cap_state.events, "all") else None
    # The event exists and carries the title.
    assert result["event"]["title"] == "Library with Mark"
    # And the day's note records it, so both views agree.
    body = cap_state.store.read(f"daily/{when}.md")
    assert "Library with Mark" in body


def test_task_capture_creates_a_due_task(cap_state):
    when = (_today(cap_state) + timedelta(days=3)).isoformat()

    result = asyncio.run(
        capture(
            cap_state, "t", {"text": "Email the professor", "kind": "task", "when": when}
        )
    )

    task = result["task"]
    assert task["text"] == "Email the professor"
    assert task["due"] == when
    assert "Email the professor" in cap_state.store.read(f"daily/{when}.md")


def test_note_capture_still_lands_in_the_daily_note(cap_state):
    result = asyncio.run(capture(cap_state, "t", {"text": "Mark prefers mornings", "kind": "note"}))

    today = _today(cap_state).isoformat()
    assert result["captured"] == "note"
    assert "Mark prefers mornings" in cap_state.store.read(f"daily/{today}.md")


def test_capture_creates_the_daily_note_when_missing(cap_state):
    when = (_today(cap_state) + timedelta(days=9)).isoformat()
    assert not cap_state.store.exists(f"daily/{when}.md")

    asyncio.run(capture(cap_state, "t", {"text": "Dentist", "kind": "event", "when": when}))

    assert cap_state.store.exists(f"daily/{when}.md")


def test_capture_is_idempotent_on_the_daily_line(cap_state):
    body = {"text": "Standup", "kind": "note"}
    asyncio.run(capture(cap_state, "t", body))
    asyncio.run(capture(cap_state, "t", body))

    today = _today(cap_state).isoformat()
    assert cap_state.store.read(f"daily/{today}.md").count("Standup") == 1


def test_capture_validates_inputs(cap_state):
    for bad in (
        {"text": "", "kind": "task"},
        {"text": "x", "kind": "reminder"},
        {"text": "x", "kind": "event"},  # an event needs a date
        {"text": "x", "kind": "task", "when": "next tuesday"},
    ):
        with pytest.raises(NoteError):
            asyncio.run(capture(cap_state, "t", bad))


def test_timed_events_get_a_default_reminder(cap_state):
    when = (_today(cap_state) + timedelta(days=2)).isoformat()

    result = asyncio.run(
        capture(cap_state, "t", {"text": "Lecture", "kind": "event", "when": f"{when}T16:00"})
    )

    assert result["event"]["reminders"] == [30]


def test_all_day_events_get_no_reminder_by_default(cap_state):
    when = (_today(cap_state) + timedelta(days=2)).isoformat()

    result = asyncio.run(
        capture(cap_state, "t", {"text": "Holiday", "kind": "event", "when": when})
    )

    assert result["event"]["all_day"] is True
    assert result["event"]["reminders"] == []


def test_a_captured_task_is_one_record_not_two(cap_state):
    """It used to create a task in Tasks Inbox *and* a prose line in the daily
    note saying the same thing — two records of one commitment, free to drift
    apart, only one of them actually a task."""
    when = (_today(cap_state) + timedelta(days=2)).isoformat()

    result = asyncio.run(
        capture(cap_state, "t", {"text": "Return the library book", "kind": "task", "when": when})
    )

    assert result["task"]["path"] == f"daily/{when}.md"
    assert not cap_state.store.exists("Tasks Inbox.md")
    body = cap_state.store.read(f"daily/{when}.md")
    assert body.count("Return the library book") == 1, "no duplicate prose line"
    assert "- [ ] Return the library book" in body, "and it is a real task"


def test_a_capture_is_not_deduped_against_a_longer_line(cap_state):
    """The dedupe was a substring test, so capturing "Stand" matched an
    existing "- Standup with the team" and silently wrote nothing."""
    asyncio.run(capture(cap_state, "t", {"text": "Standup with the team", "kind": "note"}))
    asyncio.run(capture(cap_state, "t", {"text": "Stand", "kind": "note"}))

    body = cap_state.store.read(f"daily/{_today(cap_state).isoformat()}.md")
    assert "- Standup with the team" in body.splitlines()
    assert "- Stand" in body.splitlines(), "the shorter capture must still land"
