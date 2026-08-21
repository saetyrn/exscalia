"""Reminders must arrive at their trigger time.

Before this, the only proactive surface was the morning greeting, so a reminder
set for 15 minutes before a 4pm lecture was emitted at 8am and never again.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.alerts import MAX_QUEUE, AlertQueue, due_reminders
from app.event_store import EventStore


@pytest.fixture
def alert_state(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    return SimpleNamespace(events=EventStore(vault), timezone="UTC")


def _at(hours_from_now: float) -> str:
    return (datetime.now(ZoneInfo("UTC")) + timedelta(hours=hours_from_now)).strftime(
        "%Y-%m-%dT%H:%M"
    )


def test_a_reminder_fires_only_once_its_trigger_time_arrives(alert_state):
    start = _at(2)  # two hours out, remind 30 minutes before
    alert_state.events.add(
        alert_state.events.normalize(
            title="Lecture",
            start=start,
            end=_at(3),
            all_day=False,
            timezone_name="UTC",
            notes=None,
            note_path=None,
            recurrence=None,
            reminders=[30],
        )
    )

    # Now: too early. The reminder must not be delivered or consumed.
    assert due_reminders(alert_state) == []

    # Now: inside the 30-minute window.
    later = datetime.now(ZoneInfo("UTC")) + timedelta(hours=1, minutes=45)
    fired = due_reminders(alert_state, now=later)

    assert [a["title"] for a in fired] == ["Lecture"]
    assert fired[0]["minutes_before"] == 30

    # Shown once — a second tick must not repeat it.
    assert due_reminders(alert_state, now=later) == []


def test_queue_drains_once_and_dedupes(tmp_path):
    queue = AlertQueue(tmp_path)
    alert = {"key": "e1|start|30", "title": "Lecture"}

    queue.add([alert])
    queue.add([alert])  # same key — must not double up

    drained = queue.drain()
    assert len(drained) == 1
    assert drained[0]["title"] == "Lecture"
    assert queue.drain() == []  # alerts are shown once


def test_queue_does_not_grow_without_bound(tmp_path):
    queue = AlertQueue(tmp_path)
    queue.add([{"key": f"k{i}", "title": f"E{i}"} for i in range(MAX_QUEUE + 10)])

    drained = queue.drain()

    assert len(drained) == MAX_QUEUE
    # The newest survive; stale notices are what get dropped.
    assert drained[-1]["title"] == f"E{MAX_QUEUE + 9}"


def test_events_without_reminders_produce_nothing(alert_state):
    alert_state.events.add(
        alert_state.events.normalize(
            title="Quiet event",
            start=_at(0.1),
            end=_at(1),
            all_day=False,
            timezone_name="UTC",
            notes=None,
            note_path=None,
            recurrence=None,
            reminders=[],
        )
    )
    assert due_reminders(alert_state) == []
