"""What an OMNIA Undo refreshes, and the config writers that race it.

Undo reverts one commit, and the revert's own diff is what changed: a note
renamed since the action is reverted under its new name, an undone event
touches no note at all. The live flags boot mirrors out of config.json must
follow a revert of that file, and the media-profile writers must not write a
stale snapshot of it back over a concurrent settings save.
"""
import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.api import media as media_api
from app.api import meta as meta_api
from app.index.db import content_hash
from app.overseer import tool_expansion as tx
from app.overseer.tools import execute_tool
from app.scheduler import SCHEDULE_PATH
from app.vault import fm
from app.vault.internal import atomic_json, read_json
from app.vault.notes import NoteError

TZ = "America/New_York"


@pytest.fixture
def undo_state(acting_state):
    acting_state.settings.timezone = TZ
    acting_state.timezone = TZ
    return acting_state


def drain(queue):
    events = []
    while True:
        try:
            events.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            return events


def run_tool(state, name, inp):
    result = json.loads(asyncio.run(execute_tool(state, "t", name, inp)))
    assert "error" not in result, result
    return result


def undo(state, result):
    return asyncio.run(state.actions.undo(result["_action"]["id"], "t"))


def config_path(state):
    return state.settings.vault_dir / tx.CONFIG_PATH


# --- the index follows the revert's diff -------------------------------------


def test_undo_after_a_rename_reindexes_and_announces_the_renamed_note(
    undo_state, monkeypatch
):
    """git revert follows renames: the OMNIA edit is undone in the renamed
    file. Reindexing the recorded path left the new path's row, search hit and
    desktop-sync revision on OMNIA's text."""
    state = undo_state
    # One stamp throughout: a rename restamps `modified`, and so did the edit,
    # which two different seconds would turn into a revert conflict.
    monkeypatch.setattr(fm, "now_iso", lambda: "2026-09-23T10:00:00Z")
    original = state.store.create("Essay.md", "Draft paragraph.\n")
    asyncio.run(state.indexer.index_note("Essay.md", original))
    asyncio.run(state.vault.commit_all("seed"))

    edited = original.replace(
        "Draft paragraph.\n", "Draft paragraph.\n\nZanzibarian flourish.\n"
    )
    result = run_tool(
        state,
        "edit_note",
        {"path": "Essay.md", "new_content": edited, "base_revision": content_hash(original)},
    )
    # The editor's rename, then its autosave commit.
    state.store.rename("Essay.md", "Essay Final.md")
    asyncio.run(state.indexer.rename_note("Essay.md", "Essay Final.md"))
    asyncio.run(state.vault.commit_all("autosave"))
    queue = state.live.subscribe()

    undo(state, result)

    on_disk = state.store.read("Essay Final.md")
    assert "Zanzibarian" not in on_disk
    rows = asyncio.run(state.db.execute_fetchall("SELECT path, hash FROM notes"))
    hashes = {row["path"]: row["hash"] for row in rows}
    assert hashes["Essay Final.md"] == content_hash(on_disk)
    assert "Essay.md" not in hashes
    hits = run_tool(state, "keyword_search", {"query": "Zanzibarian"})["results"]
    assert hits == []

    events = drain(queue)
    assert [(e["type"], e["path"]) for e in events] == [("note", "Essay Final.md")]
    assert events[0]["revision"] == content_hash(on_disk)


@pytest.mark.parametrize(
    "rel,refresh",
    [
        (tx.EVENTS_PATH, ["calendar"]),
        (SCHEDULE_PATH, ["calendar"]),
        (tx.ACTIVITY_PATH, ["activity", "calendar"]),
        ("Projects/.gitkeep", ["notes"]),
        (tx.CONFIG_PATH, ["notes"]),
    ],
)
def test_undo_that_touched_no_note_leaves_the_index_alone(
    undo_state, monkeypatch, rel, refresh
):
    """Undoing an event, a schedule, an activity entry, a folder or a sidebar
    order used to force-reindex (and re-embed) every note in the vault."""
    state = undo_state
    target = state.settings.vault_dir / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("" if rel.endswith(".gitkeep") else "{}\n", encoding="utf-8")
    action = asyncio.run(
        state.actions.record(
            thread_id="t",
            tool="create_event",
            summary=f"Changed {rel}",
            paths=[rel],
            refresh=refresh,
            result={},
        )
    )

    touched = []

    async def whole_vault(*args, **kwargs):
        raise AssertionError("undo resynced the whole vault")

    async def note(path, *args, **kwargs):
        touched.append(path)

    monkeypatch.setattr(state.indexer, "sync_all", whole_vault)
    monkeypatch.setattr(state.indexer, "index_note", note)
    monkeypatch.setattr(state.indexer, "remove_note", note)

    undone = undo(state, action)

    assert undone["ok"] is True
    assert touched == []


# --- config.json flags follow a revert of the file ---------------------------


def test_undo_of_the_housekeeping_toggle_restores_the_live_flag(undo_state):
    """set_omnia_housekeeping records no refresh tags, so the reload keyed on
    them never ran: the nightly job kept the undone value until a restart."""
    state = undo_state
    atomic_json(
        config_path(state), {"omnia_housekeeping": True, "omnia_full_auto": True}
    )
    asyncio.run(state.vault.commit_all("seed"))
    state.omnia_housekeeping = True
    state.omnia_full_auto = True
    state.omnia_autocorrect = False

    result = run_tool(state, "set_omnia_housekeeping", {"enabled": False})
    assert state.omnia_housekeeping is False

    undo(state, result)

    assert read_json(config_path(state), {})["omnia_housekeeping"] is True
    assert state.omnia_housekeeping is True
    assert state.omnia_full_auto is True
    assert state.omnia_autocorrect is False


def test_undo_of_a_first_timezone_restores_the_default(undo_state):
    """With no timezone in the file before, the old reload kept the undone one."""
    state = undo_state

    result = run_tool(state, "set_timezone", {"timezone": "Asia/Tokyo"})
    assert state.timezone == "Asia/Tokyo"

    undo(state, result)

    assert "timezone" not in read_json(config_path(state), {})
    assert state.timezone == TZ


def test_undo_survives_a_config_file_it_cannot_read(undo_state):
    """read_json sets a corrupt file aside and raises; the revert has already
    landed, so the undo must still succeed and leave the live flags alone."""
    state = undo_state
    config_path(state).parent.mkdir(parents=True, exist_ok=True)
    config_path(state).write_text("{not json", encoding="utf-8")
    asyncio.run(state.vault.commit_all("seed"))
    atomic_json(config_path(state), {"omnia_housekeeping": False})
    state.omnia_housekeeping = False
    action = asyncio.run(
        state.actions.record(
            thread_id="t",
            tool="set_omnia_housekeeping",
            summary="Turned nightly housekeeping off",
            paths=[tx.CONFIG_PATH],
            refresh=[],
            result={},
        )
    )

    undone = undo(state, action)

    assert undone["ok"] is True
    assert list(config_path(state).parent.glob("config.json.corrupt-*"))
    assert state.omnia_housekeeping is False


# --- media-profile writers vs concurrent config writes -----------------------


def test_profile_save_keeps_a_note_order_saved_while_it_validates(state):
    """save_profile awaited the favorites lookups between reading config.json
    and writing it back, so a sidebar drag landing in between was lost."""
    app = FastAPI()
    app.state = state
    app.state.timezone = TZ

    @app.exception_handler(NoteError)
    async def handler(_request, exc: NoteError):
        return JSONResponse(status_code=exc.status, content={"detail": exc.detail})

    app.include_router(media_api.router)
    app.include_router(meta_api.router)

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post(
                "/api/media", json={"title": "Dune", "media_type": "book"}
            )
            assert created.status_code == 200, created.text
            item = created.json()["path"]
            for i in range(10):
                order = [f"Note {i}.md"]
                profile, note_order = await asyncio.gather(
                    client.put("/api/media/profile", json={"favorites": [item]}),
                    client.put("/api/settings/note-order", json={"order": order}),
                )
                assert profile.status_code == 200, profile.text
                assert note_order.status_code == 200, note_order.text
                config = read_json(config_path(state), {})
                assert config.get("note_order") == order, i
                assert config["media_profile"]["favorites"] == [item]

    asyncio.run(run())


def test_edit_media_profile_keeps_a_config_write_made_while_it_validates(
    undo_state, monkeypatch
):
    state = undo_state
    path = config_path(state)

    async def racing_item(_db, rel):
        # A note-order save landing while the favorite is looked up.
        atomic_json(path, {**read_json(path, {}), "note_order": ["Mid.md"]})
        return {"path": rel}

    monkeypatch.setattr(media_api, "_item", racing_item)

    asyncio.run(
        tx.edit_media_profile(
            state, "t", {"name": "daytona", "favorites": ["media/Dune.md"]}
        )
    )

    config = read_json(path, {})
    assert config["note_order"] == ["Mid.md"]
    assert config["media_profile"]["name"] == "daytona"
    assert config["media_profile"]["favorites"] == ["media/Dune.md"]
