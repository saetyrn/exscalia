"""OMNIA Round 2 final batch — calendar, media, connections, settings."""
import asyncio

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app import media_theme
from app.api import events as events_module
from app.event_store import EventStore
from app.overseer.tool_expansion import (
    EXPANDED_TOOLS,
    MAX_BULK_MEDIA,
    _config,
    bulk_log_media,
    create_event,
    edit_media_profile,
    link_notes,
    reschedule_event,
    set_omnia_housekeeping,
    update_event,
)
from app.vault.notes import NoteError


@pytest.fixture
def omnia_state(acting_state):
    acting_state.timezone = "UTC"
    acting_state.events = EventStore(acting_state.settings.vault_dir)
    return acting_state


def add_note(state, path: str, content: str) -> str:
    created = state.store.create(path, content)
    asyncio.run(state.indexer.index_note(path, created))
    return created


# --- reschedule_event ------------------------------------------------------


def test_reschedule_event_moves_and_keeps_duration(omnia_state):
    asyncio.run(
        create_event(
            omnia_state,
            "t",
            {"title": "Dentist", "start": "2030-05-01T14:00", "end": "2030-05-01T15:00"},
        )
    )

    asyncio.run(reschedule_event(omnia_state, "t", {"title": "Dentist", "start": "2030-05-03T14:00"}))

    event = [e for e in omnia_state.events.all() if e["title"] == "Dentist"][0]
    assert event["start"].startswith("2030-05-03T14:00")
    assert event["end"].startswith("2030-05-03T15:00")  # 1h duration preserved


def test_reschedule_to_a_bare_date_keeps_the_time_of_day(omnia_state):
    """'Push my dentist appointment to Friday' names a day, not a time. It used
    to move the appointment to 00:00 and fire its reminder at 23:30."""
    asyncio.run(
        create_event(
            omnia_state,
            "t",
            {
                "title": "Dentist",
                "start": "2030-05-01T14:00",
                "end": "2030-05-01T15:00",
                "reminders": [30],
            },
        )
    )

    asyncio.run(reschedule_event(omnia_state, "t", {"title": "Dentist", "start": "2030-05-03"}))

    event = [e for e in omnia_state.events.all() if e["title"] == "Dentist"][0]
    assert (event["start"], event["end"]) == ("2030-05-03T14:00", "2030-05-03T15:00")
    assert event["all_day"] is False
    assert event["reminders"] == [30]


# --- update_event / create_event -------------------------------------------


@pytest.mark.parametrize(
    "new_start, new_end",
    [
        ("2030-05-01T14:00", "2030-05-01T15:00"),  # earlier: was a 49-hour event
        ("2030-05-06T09:30", "2030-05-06T10:30"),  # later: was a 400
    ],
)
def test_update_event_start_only_keeps_the_duration(omnia_state, new_start, new_end):
    event = asyncio.run(
        create_event(
            omnia_state,
            "t",
            {"title": "Lecture", "start": "2030-05-03T14:00", "end": "2030-05-03T15:00"},
        )
    )["event"]

    updated = asyncio.run(
        update_event(
            omnia_state,
            "t",
            {"event_id": event["id"], "expected_revision": event["revision"], "start": new_start},
        )
    )["event"]

    assert (updated["start"], updated["end"]) == (new_start, new_end)


def test_create_event_with_a_time_and_no_end_lasts_an_hour(omnia_state):
    event = asyncio.run(
        create_event(omnia_state, "t", {"title": "Library", "start": "2030-05-01T15:00"})
    )["event"]

    assert (event["start"], event["end"]) == ("2030-05-01T15:00", "2030-05-01T16:00")
    assert event["all_day"] is False


def test_create_event_with_a_bare_date_is_all_day(omnia_state):
    event = asyncio.run(
        create_event(omnia_state, "t", {"title": "Exam", "start": "2030-05-12"})
    )["event"]

    assert event["all_day"] is True
    assert event["start"] == "2030-05-12"


def test_an_explicit_offset_is_converted_not_dropped(omnia_state):
    omnia_state.timezone = "America/New_York"

    event = asyncio.run(
        create_event(
            omnia_state,
            "t",
            {"title": "Call", "start": "2030-05-01T14:00Z", "end": "2030-05-01T15:00Z"},
        )
    )["event"]

    # 14:00Z is 10:00 EDT; stripping the Z stored it as 14:00 local.
    assert (event["start"], event["end"]) == ("2030-05-01T10:00", "2030-05-01T11:00")
    assert event["start_utc"] == "2030-05-01T14:00:00Z"


def test_patch_with_only_a_start_keeps_the_duration(omnia_state):
    app = FastAPI()
    app.state = omnia_state
    app.include_router(events_module.router)

    @app.exception_handler(NoteError)
    async def note_err(_request: Request, exc: NoteError):
        return JSONResponse(status_code=exc.status, content={"detail": exc.detail})

    client = TestClient(app)
    event = client.post(
        "/api/events",
        json={
            "title": "Lecture",
            "start": "2030-05-03T14:00",
            "end": "2030-05-03T15:00",
            "all_day": False,
        },
    ).json()

    response = client.patch(
        f"/api/events/{event['id']}",
        json={"start": "2030-05-06T09:30", "expected_revision": event["revision"]},
    )

    assert response.status_code == 200, response.text
    assert (response.json()["start"], response.json()["end"]) == (
        "2030-05-06T09:30",
        "2030-05-06T10:30",
    )


# --- bulk_log_media / edit_media_profile -----------------------------------


def test_bulk_log_media_creates_items(omnia_state):
    result = asyncio.run(
        bulk_log_media(
            omnia_state,
            "t",
            {"items": [{"title": "Dune", "media_type": "book"}, {"title": "Solaris", "media_type": "book", "status": "finished"}]},
        )
    )

    assert len(result["items"]) == 2
    rows = asyncio.run(omnia_state.db.execute_fetchall("SELECT title FROM media_items"))
    assert {"Dune", "Solaris"} <= {r["title"] for r in rows}


def test_bulk_log_media_caps_one_call(omnia_state, monkeypatch):
    queued = []
    monkeypatch.setattr(media_theme, "enrich_soon", lambda _state, path: queued.append(path))
    items = [{"title": f"Book {n}", "media_type": "book"} for n in range(MAX_BULK_MEDIA + 1)]

    result = asyncio.run(bulk_log_media(omnia_state, "t", {"items": items}))

    assert len(result["items"]) == MAX_BULK_MEDIA == 50
    assert result["truncated"] == 1
    assert len(queued) == MAX_BULK_MEDIA
    # The list is taken in order; the overflow is reported, not logged.
    assert result["items"][-1]["title"] == f"Book {MAX_BULK_MEDIA - 1}"
    assert all(item["title"] != f"Book {MAX_BULK_MEDIA}" for item in result["items"])
    (schema,) = [d.input_schema for d, _fn in EXPANDED_TOOLS if d.name == "bulk_log_media"]
    assert schema["properties"]["items"]["maxItems"] == MAX_BULK_MEDIA


def test_edit_media_profile_sets_name_and_favorites(omnia_state):
    from app.api.media import _PROFILE_KEY

    made = asyncio.run(
        bulk_log_media(omnia_state, "t", {"items": [{"title": "Dune", "media_type": "book"}]})
    )["items"][0]

    asyncio.run(
        edit_media_profile(
            omnia_state, "t", {"name": "Reader", "location": "Arrakis", "favorites": [made["path"]]}
        )
    )

    profile = _config(omnia_state)[_PROFILE_KEY]
    assert profile["name"] == "Reader"
    assert profile["location"] == "Arrakis"
    assert profile["favorites"] == [made["path"]]


def test_edit_media_profile_rejects_untracked_favorite(omnia_state):
    from app.vault.notes import NoteError

    with pytest.raises(NoteError):
        asyncio.run(edit_media_profile(omnia_state, "t", {"favorites": ["media/not-real.md"]}))


# --- link_notes ------------------------------------------------------------


def test_link_notes_bidirectional(omnia_state):
    add_note(omnia_state, "a.md", "# A\n")
    add_note(omnia_state, "b.md", "# B\n")

    asyncio.run(link_notes(omnia_state, "t", {"path_a": "a.md", "path_b": "b.md"}))

    assert "[[b]]" in omnia_state.store.read("a.md")
    assert "[[a]]" in omnia_state.store.read("b.md")


# --- set_omnia_housekeeping ------------------------------------------------


def test_set_omnia_housekeeping_toggles(omnia_state):
    asyncio.run(set_omnia_housekeeping(omnia_state, "t", {"enabled": False}))
    assert omnia_state.omnia_housekeeping is False
    assert _config(omnia_state)["omnia_housekeeping"] is False

    asyncio.run(set_omnia_housekeeping(omnia_state, "t", {"enabled": True}))
    assert omnia_state.omnia_housekeeping is True
