"""Event times float in the app zone, and reminders arrive when they should.

gap-time-dst#3: an event created with an explicit other zone kept that zone's
wall clock, which reminders and the calendar then read as app-zone time, so a
"3pm London" call was reminded at 3pm New York. gap-time-dst#7: times in the
spring-forward gap were stored with end_utc before start_utc, and sub-day
reminder offsets counted wall-clock minutes across the change. gap-time-dst#5:
reminders longer than the 30-day digest window arrived at midnight on the day.
"""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.alerts import due_reminders
from app.api import events as events_api
from app.event_store import EventStore
from app.overseer.tool_expansion import (
    create_event,
    create_reminder,
    reschedule_event,
    update_event,
)
from app.vault.notes import NoteError

NEW_YORK = ZoneInfo("America/New_York")


@pytest.fixture
def ny_state(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    return SimpleNamespace(events=EventStore(vault), timezone="America/New_York")


@pytest.fixture
def omnia_state(acting_state):
    acting_state.timezone = "America/New_York"
    acting_state.events = EventStore(acting_state.settings.vault_dir)
    return acting_state


def _values(store, start, end, zone, *, reminders=(), app_zone=None):
    return store.normalize(
        title="Call",
        start=start,
        end=end,
        all_day=False,
        timezone_name=zone,
        notes=None,
        note_path=None,
        recurrence=None,
        reminders=list(reminders),
        app_timezone=app_zone,
    )


def _ny(*args):
    return datetime(*args, tzinfo=NEW_YORK)


def test_a_time_in_another_zone_is_stored_as_app_zone_wall_time(ny_state):
    values = _values(
        ny_state.events,
        "2026-09-24T15:00",
        "2026-09-24T16:00",
        "Europe/London",
        reminders=[0],
        app_zone="America/New_York",
    )
    assert values["start"] == "2026-09-24T10:00"
    assert values["end"] == "2026-09-24T11:00"
    assert values["timezone"] == "America/New_York"
    assert values["start_utc"] == "2026-09-24T14:00:00Z"

    ny_state.events.add(values, now=_ny(2026, 9, 23, 12, 0))
    # The call starts at 14:00Z; before, this fired at 15:00 New York (19:00Z).
    assert due_reminders(ny_state, now=_ny(2026, 9, 24, 9, 59)) == []
    fired = due_reminders(ny_state, now=_ny(2026, 9, 24, 10, 0))
    assert [a["starts_at"] for a in fired] == ["2026-09-24T10:00"]


def test_an_all_day_event_keeps_its_dates_in_the_app_zone(ny_state):
    values = ny_state.events.normalize(
        title="Holiday",
        start="2026-12-25",
        end=None,
        all_day=True,
        timezone_name="Asia/Tokyo",
        notes=None,
        note_path=None,
        recurrence=None,
        reminders=[],
        app_timezone="America/New_York",
    )
    assert (values["start"], values["end"]) == ("2026-12-25", "2026-12-25")
    assert values["timezone"] == "America/New_York"


def test_a_title_edit_after_a_zone_change_keeps_the_wall_time(omnia_state):
    created = asyncio.run(
        create_event(
            omnia_state,
            "t",
            {"title": "Standup", "start": "2030-05-01T09:00", "end": "2030-05-01T09:30"},
        )
    )["event"]
    assert created["timezone"] == "America/New_York"

    omnia_state.timezone = "America/Los_Angeles"
    asyncio.run(
        update_event(
            omnia_state,
            "t",
            {
                "event_id": created["id"],
                "expected_revision": created["revision"],
                "title": "Team standup",
            },
        )
    )

    stored = omnia_state.events.get(created["id"])
    # The stored New York zone used to be fed back in and converted to 06:00.
    assert stored["start"] == "2030-05-01T09:00"
    assert stored["timezone"] == "America/Los_Angeles"


def test_a_reschedule_after_a_zone_change_uses_the_app_zone(omnia_state):
    asyncio.run(
        create_event(
            omnia_state,
            "t",
            {"title": "Dentist", "start": "2030-05-01T14:00", "end": "2030-05-01T15:00"},
        )
    )
    omnia_state.timezone = "America/Los_Angeles"

    asyncio.run(
        reschedule_event(omnia_state, "t", {"title": "Dentist", "start": "2030-05-03T14:00"})
    )

    stored = [e for e in omnia_state.events.all() if e["title"] == "Dentist"][0]
    assert (stored["start"], stored["end"]) == ("2030-05-03T14:00", "2030-05-03T15:00")
    assert stored["timezone"] == "America/Los_Angeles"


def test_the_api_converts_on_create_and_floats_on_update(ny_state):
    ny_state.autocommit = SimpleNamespace(touch=lambda: None)
    request = SimpleNamespace(app=SimpleNamespace(state=ny_state))

    created = asyncio.run(
        events_api.create_event(
            request,
            events_api.EventBody(
                title="Call",
                start="2030-05-01T15:00",
                end="2030-05-01T16:00",
                all_day=False,
                timezone="Europe/London",
            ),
        )
    )
    assert (created["start"], created["timezone"]) == (
        "2030-05-01T10:00",
        "America/New_York",
    )

    ny_state.timezone = "America/Los_Angeles"
    updated = asyncio.run(
        events_api.update_event(
            request, created["id"], events_api.EventUpdate(title="Call Sam")
        )
    )
    assert (updated["start"], updated["timezone"]) == (
        "2030-05-01T10:00",
        "America/Los_Angeles",
    )


def test_create_reminder_converts_a_time_in_another_zone(omnia_state):
    result = asyncio.run(
        create_reminder(
            omnia_state,
            "t",
            {"text": "call London", "when": "2030-06-01T15:00", "timezone": "Europe/London"},
        )
    )

    stored = omnia_state.events.get(result["event"]["id"])
    assert (stored["start"], stored["end"]) == ("2030-06-01T10:00", "2030-06-01T10:15")
    assert stored["timezone"] == "America/New_York"


def test_a_time_in_the_spring_forward_gap_is_rejected(ny_state):
    with pytest.raises(NoteError) as raised:
        _values(ny_state.events, "2027-03-14T02:30", "2027-03-14T03:00", "America/New_York")
    assert raised.value.status == 400
    assert "02:30 does not exist on 2027-03-14" in raised.value.detail

    # The end is checked too.
    with pytest.raises(NoteError):
        _values(ny_state.events, "2027-03-14T01:30", "2027-03-14T02:15", "America/New_York")


def test_an_ambiguous_fall_back_time_means_its_first_instance(ny_state):
    values = _values(
        ny_state.events, "2027-11-07T01:30", "2027-11-07T02:15", "America/New_York"
    )
    assert values["start"] == "2027-11-07T01:30"
    assert values["start_utc"].endswith("05:30:00Z")  # 01:30 EDT, not EST


def test_a_sub_day_reminder_counts_real_minutes_across_the_change(ny_state):
    ny_state.events.add(
        _values(
            ny_state.events,
            "2027-03-14T03:30",
            "2027-03-14T04:30",
            "America/New_York",
            reminders=[120],
        ),
        now=_ny(2027, 3, 13, 12, 0),
    )
    # 03:30 EDT is 07:30Z; two hours earlier is 00:30 EST, not 01:30.
    assert due_reminders(ny_state, now=_ny(2027, 3, 14, 0, 29)) == []
    fired = due_reminders(ny_state, now=_ny(2027, 3, 14, 0, 30))
    assert [a["minutes_before"] for a in fired] == [120]


def test_a_day_before_reminder_keeps_its_wall_time_across_the_change(ny_state):
    ny_state.events.add(
        _values(
            ny_state.events,
            "2027-03-14T09:00",
            "2027-03-14T10:00",
            "America/New_York",
            reminders=[1440],
        ),
        now=_ny(2027, 3, 12, 12, 0),
    )
    # 24 elapsed hours before 09:00 EDT would be 08:00 EST the day before.
    assert due_reminders(ny_state, now=_ny(2027, 3, 13, 8, 59)) == []
    fired = due_reminders(ny_state, now=_ny(2027, 3, 13, 9, 0))
    assert [a["minutes_before"] for a in fired] == [1440]


def test_reminders_longer_than_a_month_arrive_on_their_own_day(ny_state):
    ny_state.events.add(
        _values(
            ny_state.events,
            "2026-11-10T09:00",
            "2026-11-10T11:00",
            "America/New_York",
            reminders=[60480, 1440],  # six weeks and one day before
        ),
        now=_ny(2026, 9, 22, 12, 0),
    )

    assert due_reminders(ny_state, now=_ny(2026, 9, 29, 8, 55)) == []
    six_weeks = due_reminders(ny_state, now=_ny(2026, 9, 29, 9, 5))
    assert [(a["minutes_before"], a["overdue"]) for a in six_weeks] == [(60480, False)]

    one_day = due_reminders(ny_state, now=_ny(2026, 11, 9, 9, 5))
    assert [a["minutes_before"] for a in one_day] == [1440]

    # Both were delivered; the event's own midnight has nothing left to send.
    assert due_reminders(ny_state, now=_ny(2026, 11, 10, 0, 1)) == []


def test_a_year_long_reminder_is_seen_on_its_day(ny_state):
    ny_state.events.add(
        _values(
            ny_state.events,
            "2027-09-20T09:00",
            "2027-09-20T10:00",
            "America/New_York",
            reminders=[525600],
        ),
        now=_ny(2026, 9, 1, 12, 0),
    )
    fired = due_reminders(ny_state, now=_ny(2026, 9, 20, 9, 5))
    assert [a["starts_at"] for a in fired] == ["2027-09-20T09:00"]


def test_creating_an_event_consumes_its_already_past_long_reminder(ny_state):
    # Created 41 days out with a six-week reminder: that trigger is already
    # past, so it must not be pushed later (or listed as missed on the day).
    ny_state.events.add(
        _values(
            ny_state.events,
            "2026-11-10T09:00",
            "2026-11-10T10:00",
            "America/New_York",
            reminders=[60480],
        ),
        now=_ny(2026, 9, 30, 12, 0),
    )
    assert due_reminders(ny_state, now=_ny(2026, 11, 10, 0, 1)) == []
    assert ny_state.events.digest(_ny(2026, 11, 10, 0, 1).date())["reminders"] == []
