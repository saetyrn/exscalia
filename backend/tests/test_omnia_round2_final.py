"""OMNIA Round 2 final batch — calendar, media, connections, settings."""
import asyncio

import pytest

from app.event_store import EventStore
from app.overseer.tool_expansion import (
    _config,
    bulk_log_media,
    create_event,
    edit_media_profile,
    link_notes,
    reschedule_event,
    set_omnia_housekeeping,
)


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
