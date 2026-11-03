"""Reminders must arrive at their trigger time.

Before this, the only proactive surface was the morning greeting, so a reminder
set for 15 minutes before a 4pm lecture was emitted at 8am and never again.
"""

import time
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import asyncio

import pytest

from app import alerts as alerts_module
from app import push_service
from app.alerts import (
    MAX_QUEUE,
    AlertQueue,
    TaskReminderLog,
    due_reminders,
    reminder_tick,
)
from app.event_store import EventStore
from app.vault.notes import NoteError


class NoTasks:
    async def execute_fetchall(self, *args):
        return []

UTC = ZoneInfo("UTC")
NEW_YORK = ZoneInfo("America/New_York")


@pytest.fixture
def alert_state(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    return SimpleNamespace(
        events=EventStore(vault),
        timezone="UTC",
        alerts=AlertQueue(tmp_path),
        db=NoTasks(),
        settings=SimpleNamespace(vault_dir=vault, data_dir=tmp_path),
    )


@pytest.fixture
def pushed(monkeypatch):
    sent: list[list[str]] = []

    async def deliver(state, alerts):
        if alerts:
            sent.append([a["key"] for a in alerts])
        return {"sent": len(alerts), "pruned": 0}

    monkeypatch.setattr(push_service, "deliver", deliver)
    return sent


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


def test_queue_keeps_alerts_until_acked_and_dedupes(tmp_path):
    queue = AlertQueue(tmp_path)
    alert = {"key": "e1|start|30", "title": "Lecture"}

    assert queue.add([alert]) == [alert]
    assert queue.add([alert]) == []  # same key — must not double up

    # Reading consumes nothing: a response lost in flight is re-sent.
    first = queue.peek()
    assert [a["title"] for a in first] == ["Lecture"]
    assert queue.peek() == first

    assert queue.ack(["e1|start|30"]) == 1
    assert queue.peek() == []


def test_ack_removes_only_the_given_keys(tmp_path):
    queue = AlertQueue(tmp_path)
    queue.add([{"key": k, "title": k} for k in ("a", "b", "c")])

    assert queue.ack(["b", "missing"]) == 1

    assert [a["key"] for a in queue.peek()] == ["a", "c"]
    assert queue.ack([]) == 0


def test_queue_does_not_grow_without_bound(tmp_path):
    queue = AlertQueue(tmp_path)
    queue.add([{"key": f"k{i}", "title": f"E{i}"} for i in range(MAX_QUEUE + 10)])

    queued = queue.peek()

    assert len(queued) == MAX_QUEUE
    # The newest survive; stale notices are what get dropped.
    assert queued[-1]["title"] == f"E{MAX_QUEUE + 9}"


def test_alert_routes_peek_then_ack(alert_state):
    from conftest import make_client
    from app.api import overseer as overseer_api

    alert_state.alerts.add(
        [{"key": "e1|start|30", "title": "Lecture"}, {"key": "task:t1:x", "title": "Rent"}]
    )
    client = make_client(alert_state, overseer_api.router)

    first = client.get("/api/overseer/alerts?peek=1").json()["alerts"]
    assert [a["key"] for a in first] == ["e1|start|30", "task:t1:x"]
    # A second poll (the first response was lost) still sees both.
    assert client.get("/api/overseer/alerts?peek=1").json()["alerts"] == first

    acked = client.post("/api/overseer/alerts/ack", json={"keys": ["e1|start|30"]})
    assert acked.json() == {"acked": 1}
    assert [a["key"] for a in client.get("/api/overseer/alerts?peek=1").json()["alerts"]] == [
        "task:t1:x"
    ]


def test_plain_alert_get_still_drains_for_clients_that_never_ack(alert_state):
    """A desktop build bundles its own frontend; one from before the ack polls
    a plain GET every minute and toasts all it gets. Peek there would re-toast
    every queued alert on each poll until some newer client acked it."""
    from conftest import make_client
    from app.api import overseer as overseer_api

    alert_state.alerts.add([{"key": "e1|start|30", "title": "Lecture"}])
    client = make_client(alert_state, overseer_api.router)

    first = client.get("/api/overseer/alerts").json()["alerts"]
    assert [a["key"] for a in first] == ["e1|start|30"]
    assert client.get("/api/overseer/alerts").json()["alerts"] == []
    assert alert_state.alerts.peek() == []


# --- reminder_tick: queue first, then mark delivered ------------------------

TICK_AT = datetime(2026, 9, 23, 15, 50, tzinfo=ZoneInfo("UTC"))
LECTURE_KEY = "2026-09-23T16:00|15"


def _lecture(state) -> str:
    """An event whose 15-minutes-before reminder is due at TICK_AT. Created an
    hour earlier: add() pre-marks reminders already past at creation, and the
    wall clock has no say in these tests."""
    return state.events.add(
        state.events.normalize(
            title="Lecture",
            start="2026-09-23T16:00",
            end="2026-09-23T17:00",
            all_day=False,
            timezone_name="UTC",
            notes=None,
            note_path=None,
            recurrence=None,
            reminders=[15],
        ),
        now=TICK_AT - timedelta(hours=1),
    )["id"]


def _surfaced(state, event_id: str) -> list[str]:
    return state.events.get(event_id).get("surfaced_reminders") or []


def test_a_failing_task_source_does_not_strand_event_reminders(
    alert_state, pushed, monkeypatch
):
    async def broken(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(alerts_module, "task_due_reminders", broken)
    event_id = _lecture(alert_state)

    asyncio.run(reminder_tick(alert_state, TICK_AT))

    # This used to mark it surfaced, then die before queueing: gone for good.
    assert [a["title"] for a in alert_state.alerts.peek()] == ["Lecture"]
    assert _surfaced(alert_state, event_id) == [LECTURE_KEY]
    assert pushed == [[f"{event_id}|2026-09-23T16:00|15"]]

    # Consumed once queued: the next tick neither re-queues nor re-pushes it.
    asyncio.run(reminder_tick(alert_state, TICK_AT + timedelta(minutes=1)))
    assert len(alert_state.alerts.peek()) == 1
    assert len(pushed) == 1


def test_a_failed_queue_write_leaves_event_reminders_unconsumed(
    alert_state, pushed, monkeypatch
):
    task_alert = {
        "key": "task:t1:2026-09-23T15:00",
        "title": "Pay rent",
        "starts_at": "2026-09-23T15:00",
        "minutes_before": 0,
        "overdue": False,
        "task_id": "t1",
    }

    async def one_task(state, now=None, mark=True):
        assert mark is False  # the tick records the ledger itself, after queueing
        return [task_alert], {task_alert["key"]: task_alert["starts_at"]}

    real_add = AlertQueue.add

    def add_failing_for_events(self, batch):
        if any(not a["key"].startswith("task:") for a in batch):
            raise NoteError(500, "omnia-alerts.json is corrupt")
        return real_add(self, batch)

    monkeypatch.setattr(alerts_module, "task_due_reminders", one_task)
    monkeypatch.setattr(AlertQueue, "add", add_failing_for_events)
    event_id = _lecture(alert_state)

    asyncio.run(reminder_tick(alert_state, TICK_AT))

    assert _surfaced(alert_state, event_id) == []
    assert [a["key"] for a in alert_state.alerts.peek()] == [task_alert["key"]]
    assert task_alert["key"] in TaskReminderLog(alert_state.settings.data_dir).seen()

    # Once the queue works again the held-back reminder is delivered.
    monkeypatch.setattr(AlertQueue, "add", real_add)
    asyncio.run(reminder_tick(alert_state, TICK_AT + timedelta(minutes=1)))

    assert [a["title"] for a in alert_state.alerts.peek()] == ["Pay rent", "Lecture"]
    assert _surfaced(alert_state, event_id) == [LECTURE_KEY]
    assert pushed == [[task_alert["key"]], [f"{event_id}|2026-09-23T16:00|15"]]


def test_a_corrupt_queue_file_delays_a_reminder_rather_than_losing_it(
    alert_state, pushed
):
    event_id = _lecture(alert_state)
    alert_state.alerts.path.write_text("{not json", encoding="utf-8")

    # read_json sets the corrupt file aside and raises, so nothing is queued
    # and nothing may be marked delivered.
    asyncio.run(reminder_tick(alert_state, TICK_AT))
    assert _surfaced(alert_state, event_id) == []
    assert not alert_state.alerts.path.exists()

    asyncio.run(reminder_tick(alert_state, TICK_AT + timedelta(minutes=1)))
    assert [a["title"] for a in alert_state.alerts.peek()] == ["Lecture"]
    assert _surfaced(alert_state, event_id) == [LECTURE_KEY]


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


def _add(state, title, start, end, reminders, *, now, recurrence=None, zone="UTC"):
    return state.events.add(
        state.events.normalize(
            title=title,
            start=start,
            end=end,
            all_day=False,
            timezone_name=zone,
            notes=None,
            note_path=None,
            recurrence=recurrence,
            reminders=reminders,
        ),
        now=now,
    )


def test_a_backdated_series_does_not_push_its_past_occurrences(alert_state):
    """gap-time-dst#1: a daily series created three weeks after it started, or
    any later edit to one, pushed one overdue reminder per past occurrence in a
    single tick."""
    events = alert_state.events
    now = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
    event = _add(
        alert_state,
        "Meds",
        "2026-09-01T08:00",
        "2026-09-01T08:15",
        [10],
        now=now,
        recurrence={"frequency": "daily", "interval": 1},
    )

    assert due_reminders(alert_state, now=now) == []
    # Consumed at creation, so the greeting does not list them as missed either.
    assert events.digest(now.date())["reminders"] == []

    # A title-only edit keeps the past occurrences consumed.
    updated = events.update(
        event["id"], {"title": "Morning meds"}, event["revision"], now=now
    )
    assert {"2026-09-01T08:00|10", "2026-09-22T08:00|10"} <= set(
        updated["surfaced_reminders"]
    )
    assert due_reminders(alert_state, now=now) == []
    assert events.digest(now.date())["reminders"] == []

    # The series still reminds at its next occurrence.
    tomorrow = datetime(2026, 9, 23, 7, 51, tzinfo=UTC)
    fired = due_reminders(alert_state, now=tomorrow)
    assert [a["starts_at"] for a in fired] == ["2026-09-23T08:00"]


def test_an_event_made_inside_its_reminder_window_still_alerts(alert_state):
    """Creating an event marked every reminder whose trigger had passed as
    delivered, so a 3pm call with a 15-minute reminder, added at 2:50, was
    never pushed, although due_reminders would still deliver it."""
    now = datetime(2026, 9, 22, 14, 50, tzinfo=UTC)
    _add(alert_state, "Call", "2026-09-22T15:00", "2026-09-22T15:30", [15], now=now)
    # A reminder set for the current minute, as create_reminder makes one.
    _add(alert_state, "Stretch", "2026-09-22T14:50", "2026-09-22T15:05", [0], now=now)
    # A day-before reminder for an event later today.
    _add(alert_state, "Exam", "2026-09-22T18:00", "2026-09-22T20:00", [1440], now=now)

    tick = now + timedelta(minutes=1)
    fired = sorted(a["title"] for a in due_reminders(alert_state, now=tick))
    assert fired == ["Call", "Exam", "Stretch"]
    assert due_reminders(alert_state, now=tick + timedelta(minutes=1)) == []


def test_rescheduling_into_the_reminder_window_alerts_once(alert_state):
    events = alert_state.events
    now = datetime(2026, 9, 22, 14, 50, tzinfo=UTC)
    event = _add(alert_state, "Call", "2026-09-22T17:00", "2026-09-22T17:30", [15], now=now)

    events.update(
        event["id"],
        {"start": "2026-09-22T15:00", "end": "2026-09-22T15:30"},
        event["revision"],
        now=now,
    )
    tick = now + timedelta(minutes=1)
    assert [a["title"] for a in due_reminders(alert_state, now=tick)] == ["Call"]

    # Delivered: an edit that leaves the reminder where it was keeps it so.
    moved = events.get(event["id"])
    events.update(event["id"], {"title": "Call Sam"}, moved["revision"], now=tick)
    assert due_reminders(alert_state, now=tick + timedelta(minutes=1)) == []


def test_a_timely_reminder_is_pushed_once(alert_state):
    _add(
        alert_state,
        "Lecture",
        "2026-09-22T12:00",
        "2026-09-22T13:00",
        [30],
        now=datetime(2026, 9, 22, 10, 0, tzinfo=UTC),
    )
    at = datetime(2026, 9, 22, 11, 35, tzinfo=UTC)

    assert [a["title"] for a in due_reminders(alert_state, now=at)] == ["Lecture"]
    assert due_reminders(alert_state, now=at + timedelta(minutes=1)) == []


def test_a_long_missed_reminder_is_left_for_the_morning_greeting(alert_state):
    event = _add(
        alert_state,
        "Lecture",
        "2026-09-22T12:00",
        "2026-09-22T13:00",
        [30],
        now=datetime(2026, 9, 22, 10, 0, tzinfo=UTC),
    )
    # Three hours after the trigger (the server was down), the lecture long begun.
    late = datetime(2026, 9, 22, 14, 30, tzinfo=UTC)

    assert due_reminders(alert_state, now=late) == []
    stored = alert_state.events.get(event["id"])
    assert "2026-09-22T12:00|30" not in stored["surfaced_reminders"]
    listed = alert_state.events.digest(late.date())["reminders"]
    assert [(r["title"], r["minutes_before"]) for r in listed] == [("Lecture", 30)]


def test_a_late_reminder_still_arrives_before_its_event_starts(alert_state):
    _add(
        alert_state,
        "Exam",
        "2026-09-22T12:00",
        "2026-09-22T14:00",
        [120],
        now=datetime(2026, 9, 22, 8, 0, tzinfo=UTC),
    )
    # An hour past the 10:00 trigger, but the exam has not started yet.
    at = datetime(2026, 9, 22, 11, 0, tzinfo=UTC)

    assert [a["title"] for a in due_reminders(alert_state, now=at)] == ["Exam"]


def test_consuming_reminders_requires_the_callers_clock(alert_state):
    """be-vault-index#7: the fallback was naive datetime.now(), UTC in the
    container and hours ahead of the local times it was compared with."""
    with pytest.raises(TypeError):
        alert_state.events.digest(date(2026, 9, 22), mark_surfaced=True)


def test_a_reminder_is_consumed_only_once_its_local_trigger_passes(alert_state):
    event = _add(
        alert_state,
        "Meeting",
        "2026-09-22T11:00",
        "2026-09-22T12:00",
        [15],
        now=datetime(2026, 9, 22, 7, 0, tzinfo=NEW_YORK),
        zone="America/New_York",
    )
    key = "2026-09-22T11:00|15"
    events = alert_state.events

    events.digest(
        date(2026, 9, 22), mark_surfaced=True, now=datetime(2026, 9, 22, 8, 0, tzinfo=NEW_YORK)
    )
    assert key not in events.get(event["id"])["surfaced_reminders"]

    events.digest(
        date(2026, 9, 22), mark_surfaced=True, now=datetime(2026, 9, 22, 10, 50, tzinfo=NEW_YORK)
    )
    assert key in events.get(event["id"])["surfaced_reminders"]


@pytest.fixture
def utc_process_clock(monkeypatch):
    """The container sets no TZ, so process-local time is UTC."""
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_the_morning_check_leaves_reminders_that_are_not_due_yet(
    tmp_path, monkeypatch, utc_process_clock
):
    """be-vault-index#7: opening the app at 08:00 in New York consumed every
    reminder due before 12:00, so a 10:45 reminder never fired."""
    from fastapi.testclient import TestClient

    from app import main as main_module
    from app.config import Settings

    settings = Settings(
        data_dir=tmp_path / "app-data",
        frontend_dir=Path(__file__).resolve().parents[2] / "frontend" / "build",
        embedding_backend="none",
        autocommit_debounce=0.01,
        auth_password=None,
        anthropic_api_key=None,
        openai_api_key=None,
        google_api_key=None,
        backup_remote=None,
    )
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)

    with TestClient(main_module.app) as client:
        state = client.app.state
        state.timezone = "America/New_York"
        local = datetime.now(NEW_YORK).replace(tzinfo=None, second=0, microsecond=0)

        def add(title, starts_in):
            start = local + starts_in
            return _add(
                state,
                title,
                start.isoformat(timespec="minutes"),
                (start + timedelta(hours=1)).isoformat(timespec="minutes"),
                [15],
                now=local - timedelta(hours=1),
                zone="America/New_York",
            )

        later = add("Later", timedelta(hours=2))
        due = add("Due", timedelta(minutes=10))

        response = client.post("/api/overseer/morning")
        assert response.status_code == 200, response.text
        assert response.json()["greet"] is True

        assert f"{later['start']}|15" not in state.events.get(later["id"])[
            "surfaced_reminders"
        ]
        assert f"{due['start']}|15" in state.events.get(due["id"])["surfaced_reminders"]
