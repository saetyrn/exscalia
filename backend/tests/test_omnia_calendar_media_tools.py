"""OMNIA Round 3 — calendar availability and media reads/by-title update."""
import asyncio

import pytest

from app.event_store import EventStore
from app.overseer.tool_expansion import (
    bulk_log_media,
    create_event,
    create_task,
    find_free_time,
    list_media,
    media_diary,
    update_media_by_title,
    week_agenda,
)


@pytest.fixture
def omnia_state(acting_state):
    acting_state.timezone = "UTC"
    acting_state.events = EventStore(acting_state.settings.vault_dir)
    return acting_state


# --- find_free_time --------------------------------------------------------


def test_find_free_time_excludes_busy(omnia_state):
    asyncio.run(
        create_event(
            omnia_state, "t", {"title": "Meeting", "start": "2030-05-01T10:00", "end": "2030-05-01T11:00"}
        )
    )

    result = asyncio.run(find_free_time(omnia_state, "t", {"start": "2030-05-01", "end": "2030-05-01"}))

    slots = result["days"][0]["free"]
    assert any(s["start"] == "2030-05-01T09:00" and s["end"] == "2030-05-01T10:00" for s in slots)
    assert any(s["start"] == "2030-05-01T11:00" for s in slots)
    # Nothing overlaps the 10-11 meeting.
    assert not any(s["start"] == "2030-05-01T10:00" for s in slots)


# --- week_agenda -----------------------------------------------------------


def test_week_agenda_lays_out_events_and_tasks(omnia_state):
    asyncio.run(
        create_event(
            omnia_state, "t", {"title": "Standup", "start": "2030-05-02T09:00", "end": "2030-05-02T09:15"}
        )
    )
    asyncio.run(
        create_task(omnia_state, "t", {"text": "submit report", "path": "Tasks Inbox.md", "due": "2030-05-02"})
    )

    result = asyncio.run(week_agenda(omnia_state, "t", {"start": "2030-05-01", "end": "2030-05-03"}))

    day = result["agenda"]["2030-05-02"]
    assert any(e["title"] == "Standup" for e in day["events"])
    assert any(t["text"] == "submit report" for t in day["due_tasks"])


# --- list_media ------------------------------------------------------------


def test_list_media_by_status(omnia_state):
    asyncio.run(
        bulk_log_media(
            omnia_state,
            "t",
            {"items": [{"title": "Dune", "media_type": "book", "status": "active"}, {"title": "Solaris", "media_type": "book", "status": "planned"}]},
        )
    )

    active = asyncio.run(list_media(omnia_state, "t", {"status": "active"}))
    assert {i["title"] for i in active["items"]} == {"Dune"}

    watchlist = asyncio.run(list_media(omnia_state, "t", {"status": "watchlist"}))
    assert {i["title"] for i in watchlist["items"]} == {"Solaris"}


# --- media_diary + update_media_by_title -----------------------------------


def test_update_media_by_title_and_diary(omnia_state):
    asyncio.run(
        bulk_log_media(omnia_state, "t", {"items": [{"title": "Severance", "media_type": "tv", "status": "planned"}]})
    )

    asyncio.run(update_media_by_title(omnia_state, "t", {"title": "Severance", "status": "active", "progress": "S01E05"}))

    rows = asyncio.run(omnia_state.db.execute_fetchall("SELECT status FROM media_items WHERE title = 'Severance'"))
    assert rows[0]["status"] == "active"

    diary = asyncio.run(media_diary(omnia_state, "t", {}))
    assert any(a["title"] == "Severance" for a in diary["activity"])


def test_update_media_by_title_ambiguous_returns_candidates(omnia_state):
    asyncio.run(
        bulk_log_media(
            omnia_state,
            "t",
            {"items": [{"title": "The Wire", "media_type": "tv"}, {"title": "The Wire Reloaded", "media_type": "tv"}]},
        )
    )

    result = asyncio.run(update_media_by_title(omnia_state, "t", {"title": "wire", "status": "active"}))

    assert result.get("ambiguous") is True
    assert len(result["candidates"]) == 2
