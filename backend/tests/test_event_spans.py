"""A multi-day event belongs to every day it covers.

be-tasks-calendar-misc#8: occurrences() tested only an occurrence's start day
against the range, so an Oct 3-10 trip was on Oct 3 and nowhere else: absent
from the day view, the morning briefing's today_events and OMNIA's awareness
for Oct 4-10, and from the whole month view when it started the month before.
"""

from datetime import date, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import calendar as calendar_api
from app.event_store import EventStore
from app.vault.notes import NoteError

TZ = "America/New_York"


@pytest.fixture
def store(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    return EventStore(vault)


def add(
    store,
    title,
    start,
    end,
    *,
    all_day,
    recurrence=None,
    reminders=(),
    now=None,
):
    return store.add(
        store.normalize(
            title=title,
            start=start,
            end=end,
            all_day=all_day,
            timezone_name=TZ,
            notes=None,
            note_path=None,
            recurrence=recurrence,
            reminders=list(reminders),
        ),
        now=now,
    )


def days(first, last):
    return [
        date.fromordinal(ordinal).isoformat()
        for ordinal in range(first.toordinal(), last.toordinal() + 1)
    ]


def test_an_all_day_span_is_listed_on_each_day_it_covers(store):
    add(store, "Tokyo trip", "2026-10-03", "2026-10-10", all_day=True)

    middle = store.occurrences(date(2026, 10, 4), date(2026, 10, 4))
    assert len(middle) == 1
    assert middle[0]["occurrence_date"] == "2026-10-03"
    assert middle[0]["covers"] == days(date(2026, 10, 3), date(2026, 10, 10))
    assert len(store.occurrences(date(2026, 10, 10), date(2026, 10, 10))) == 1

    assert store.occurrences(date(2026, 10, 11), date(2026, 10, 11)) == []
    assert store.occurrences(date(2026, 9, 1), date(2026, 10, 2)) == []
    # One entry per occurrence, however much of it the range covers.
    assert len(store.occurrences(date(2026, 10, 1), date(2026, 10, 31))) == 1


def test_one_day_events_cover_only_their_own_day(store):
    add(store, "Dentist", "2026-10-05T09:00", "2026-10-05T10:00", all_day=False)
    add(store, "Holiday", "2026-10-12", None, all_day=True)
    add(
        store,
        "Standup",
        "2026-10-01T09:00",
        "2026-10-01T09:15",
        all_day=False,
        recurrence={"frequency": "daily", "interval": 1},
    )

    occurrences = store.occurrences(date(2026, 10, 1), date(2026, 10, 31))
    assert len(occurrences) == 33
    assert all(item["covers"] == [item["occurrence_date"]] for item in occurrences)


def test_a_timed_event_over_midnight_is_on_both_days(store):
    add(store, "Night shift", "2026-10-06T23:00", "2026-10-07T01:00", all_day=False)

    for day in (date(2026, 10, 6), date(2026, 10, 7)):
        occurrences = store.occurrences(day, day)
        assert [item["title"] for item in occurrences] == ["Night shift"]
        assert occurrences[0]["covers"] == ["2026-10-06", "2026-10-07"]
    assert store.occurrences(date(2026, 10, 8), date(2026, 10, 8)) == []

    today = store.digest(date(2026, 10, 7))["events_today"]
    assert [item["title"] for item in today] == ["Night shift"]


def test_a_timed_event_ending_at_midnight_stays_on_its_day(store):
    add(store, "Late film", "2026-10-06T22:00", "2026-10-07T00:00", all_day=False)

    occurrences = store.occurrences(date(2026, 10, 6), date(2026, 10, 6))
    assert occurrences[0]["covers"] == ["2026-10-06"]
    assert store.occurrences(date(2026, 10, 7), date(2026, 10, 7)) == []


def test_each_recurring_occurrence_covers_its_own_span(store):
    # Saturdays and Sundays, starting Saturday Oct 3.
    add(
        store,
        "Weekend away",
        "2026-10-03",
        "2026-10-04",
        all_day=True,
        recurrence={"frequency": "weekly", "interval": 1, "weekdays": [5]},
    )

    month = store.occurrences(date(2026, 10, 1), date(2026, 10, 31))
    assert [item["covers"] for item in month] == [
        ["2026-10-03", "2026-10-04"],
        ["2026-10-10", "2026-10-11"],
        ["2026-10-17", "2026-10-18"],
        ["2026-10-24", "2026-10-25"],
        ["2026-10-31", "2026-11-01"],
    ]
    # A range that starts on an occurrence's second day still finds it.
    sunday = store.occurrences(date(2026, 10, 11), date(2026, 10, 11))
    assert [item["occurrence_date"] for item in sunday] == ["2026-10-10"]
    november = store.occurrences(date(2026, 11, 1), date(2026, 11, 1))
    assert [item["occurrence_date"] for item in november] == ["2026-10-31"]


def test_a_skipped_occurrence_is_gone_from_all_its_days(store):
    add(
        store,
        "Weekend away",
        "2026-10-03",
        "2026-10-04",
        all_day=True,
        recurrence={
            "frequency": "weekly",
            "interval": 1,
            "weekdays": [5],
            "exceptions": ["2026-10-10"],
        },
    )

    assert store.occurrences(date(2026, 10, 11), date(2026, 10, 11)) == []


def test_a_span_reminds_once(store):
    # The occurrence started yesterday and runs into today's window; it is
    # still one occurrence with one reminder.
    add(
        store,
        "Conference",
        "2026-10-03",
        "2026-10-05",
        all_day=True,
        reminders=[0],
        now=datetime(2026, 10, 1, 12, 0),
    )

    reminders = store.digest(date(2026, 10, 4))["reminders"]
    assert [(item["occurrence_start"], item["overdue"]) for item in reminders] == [
        ("2026-10-03", True)
    ]

    later = add(
        store,
        "Retreat",
        "2026-10-03",
        "2026-10-05",
        all_day=True,
        reminders=[0],
        now=datetime(2026, 10, 4, 12, 0),
    )
    assert later["surfaced_reminders"] == ["2026-10-03|0"]


def make_client(state, events: EventStore) -> TestClient:
    app = FastAPI()
    app.state = state
    app.state.timezone = TZ
    state.task_tombstones = SimpleNamespace(all=lambda: {})
    state.activities = SimpleNamespace(range_totals=lambda first, last: {})
    state.events = events

    @app.exception_handler(NoteError)
    async def handler(_request, exc: NoteError):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=exc.status, content={"detail": exc.detail})

    app.include_router(calendar_api.router)
    return TestClient(app)


def test_the_calendar_shows_a_span_on_every_day_across_months(state):
    events = EventStore(state.settings.vault_dir)
    trip = add(events, "Trip", "2026-09-28", "2026-10-05", all_day=True)
    add(events, "Ski week", "2027-02-27", "2027-03-02", all_day=True)
    client = make_client(state, events)

    october = client.get("/api/calendar/2026/10").json()
    assert october["event_activity"] == {
        day: 1 for day in days(date(2026, 10, 1), date(2026, 10, 5))
    }
    assert [item["event_id"] for item in october["events"]] == [trip["id"]]

    september = client.get("/api/calendar/2026/9").json()
    assert september["event_activity"] == {
        day: 1 for day in days(date(2026, 9, 28), date(2026, 9, 30))
    }

    # February's query runs into March; only February's days count there.
    february = client.get("/api/calendar/2027/2").json()
    assert february["event_activity"] == {"2027-02-27": 1, "2027-02-28": 1}
    march = client.get("/api/calendar/2027/3").json()
    assert march["event_activity"] == {"2027-03-01": 1, "2027-03-02": 1}
    assert client.get("/api/calendar/2026/11").json()["events"] == []

    ongoing = client.get("/api/calendar/day/2026-10-04").json()["events"]
    assert [(item["title"], item["continuation"]) for item in ongoing] == [
        ("Trip", True)
    ]
    first = client.get("/api/calendar/day/2026-09-28").json()["events"]
    assert [(item["title"], item["continuation"]) for item in first] == [
        ("Trip", False)
    ]
    assert client.get("/api/calendar/day/2026-10-06").json()["events"] == []
