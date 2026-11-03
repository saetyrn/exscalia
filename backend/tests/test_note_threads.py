"""A note's OMNIA conversation is bound to the note on the server, so every
device finds it and every move carries it. Clients used to keep the pointer
in their own localStorage only: a rename they did not see live (on another
device, or OMNIA's while the laptop slept) orphaned the conversation, and each
other device started a thread of its own for the same note."""
import asyncio
import json

import pytest

from app.api import notes as notes_api
from app.api import overseer as overseer_api
from app.overseer.threads import ThreadStore
from app.overseer.tools import execute_tool
from app.proofread import ProofreadStore

from conftest import make_client


@pytest.fixture
def threads(acting_state, tmp_path):
    acting_state.threads = ThreadStore(acting_state.settings.vault_dir, tmp_path)
    return acting_state.threads


@pytest.fixture
def client(acting_state, threads):
    acting_state.proofread = ProofreadStore(acting_state.settings.data_dir)
    return make_client(acting_state, notes_api.router, overseer_api.router)


def add(client, path: str, content: str = "body\n") -> None:
    assert client.post("/api/notes", json={"path": path, "content": content}).status_code == 200


def bound(client, path: str) -> str | None:
    res = client.get("/api/omnia/note-thread", params={"path": path})
    assert res.status_code == 200, res.text
    return res.json()["id"]


def thread_for(client, path: str) -> str:
    res = client.post("/api/omnia/threads", json={"title": "Plan", "note_path": path})
    assert res.status_code == 200, res.text
    return res.json()["id"]


# --- the store ----------------------------------------------------------------


def test_a_note_gets_one_thread_however_many_devices_ask(threads):
    first = threads.create_for_note("Plan.md", "Plan")
    again = threads.create_for_note("Plan.md", "Plan")
    assert again["id"] == first["id"]
    assert threads.for_note("Plan.md") == first["id"]
    assert threads.for_note("Other.md") is None


def test_the_first_binding_stands(threads):
    mine = threads.create(title="mine")["id"]
    theirs = threads.create(title="theirs")["id"]
    assert threads.bind_note("Plan.md", mine) == mine
    assert threads.bind_note("Plan.md", theirs) == mine
    # Until that thread is deleted: its binding goes with it.
    threads.delete(mine)
    assert threads.for_note("Plan.md") is None
    assert threads.bind_note("Plan.md", theirs) == theirs


def test_moves_carry_bindings_all_at_once(threads):
    a = threads.create_for_note("a.md", "a")["id"]
    b = threads.create_for_note("b.md", "b")["id"]
    # A swap, as an undo can produce: neither binding may overwrite the other.
    assert threads.notes_moved([("a.md", "b.md"), ("b.md", "a.md")])
    assert threads.for_note("a.md") == b
    assert threads.for_note("b.md") == a
    assert not threads.notes_moved([("c.md", "d.md"), ("a.md", "a.md")])


def test_a_note_moved_onto_a_deleted_notes_path_does_not_inherit_its_chat(threads):
    threads.create_for_note("Old.md", "Old")  # its note went to the Trash
    assert threads.notes_moved([("Fresh.md", "Old.md")])
    assert threads.for_note("Old.md") is None


def test_an_unreadable_binding_file_never_fails_a_move(threads):
    threads.note_threads_path.parent.mkdir(parents=True, exist_ok=True)
    threads.note_threads_path.write_text("{not json", encoding="utf-8")
    assert not threads.notes_moved([("a.md", "b.md")])
    assert threads.for_note("a.md") is None


# --- the routes -----------------------------------------------------------------


def test_routes_bind_look_up_and_adopt(client, threads):
    assert bound(client, "Plan.md") is None
    made = thread_for(client, "Plan.md")
    assert bound(client, "Plan.md") == made
    # Another device asking for the note's thread gets the same one.
    assert thread_for(client, "Plan.md") == made

    # A thread one device only remembered locally is bound once; a second
    # device's legacy thread gives way to it.
    legacy = client.post("/api/omnia/threads", json={"title": "x"}).json()["id"]
    other = client.post("/api/omnia/threads", json={"title": "y"}).json()["id"]
    res = client.put("/api/omnia/note-thread", json={"path": "Log.md", "thread_id": legacy})
    assert res.json() == {"id": legacy}
    res = client.put("/api/omnia/note-thread", json={"path": "Log.md", "thread_id": other})
    assert res.json() == {"id": legacy}

    # A pointer left behind at a path its note moved away from binds nothing.
    res = client.put("/api/omnia/note-thread", json={"path": "New.md", "thread_id": made})
    assert res.json() == {"id": None}
    assert bound(client, "New.md") is None

    assert client.get("/api/omnia/note-thread", params={"path": "media:hub"}).status_code == 400
    res = client.put("/api/omnia/note-thread", json={"path": "Log.md", "thread_id": "missing1"})
    assert res.status_code == 404


def test_rest_renames_carry_the_conversation(client):
    add(client, "Plan.md")
    add(client, "School/Essay.md")
    plan = thread_for(client, "Plan.md")
    essay = thread_for(client, "School/Essay.md")

    res = client.post("/api/notes/Plan.md/rename", json={"new_path": "Roadmap.md"})
    assert res.status_code == 200, res.text
    res = client.post(
        "/api/notes/folders/rename", json={"path": "School", "new_path": "Archive/School"}
    )
    assert res.status_code == 200, res.text

    assert bound(client, "Roadmap.md") == plan
    assert bound(client, "Plan.md") is None
    assert bound(client, "Archive/School/Essay.md") == essay
    assert bound(client, "School/Essay.md") is None


def test_a_note_restored_from_the_trash_finds_its_conversation(client, acting_state):
    add(client, "Plan.md")
    plan = thread_for(client, "Plan.md")
    assert client.delete("/api/notes/Plan.md").status_code == 200

    (item,) = acting_state.trash.list()
    acting_state.trash.restore(item["id"])

    assert bound(client, "Plan.md") == plan


def _run_tool(state, name, inp):
    result = json.loads(asyncio.run(execute_tool(state, "t", name, inp)))
    assert "error" not in result, result
    return result


def test_omnia_moves_and_their_undo_carry_the_conversation(acting_state, threads):
    state = acting_state
    for name in ("Essay", "Reading"):
        path = f"School/{name}.md"
        content = state.store.create(path, f"# {name}\n\nDraft paragraph.\n")
        asyncio.run(state.indexer.index_note(path, content))
    asyncio.run(state.vault.commit_all("seed"))
    essay = threads.create_for_note("School/Essay.md", "Essay")["id"]

    result = _run_tool(state, "move_folder", {"path": "School", "new_path": "Archive/School"})
    assert threads.for_note("Archive/School/Essay.md") == essay
    assert threads.for_note("School/Essay.md") is None

    asyncio.run(state.actions.undo(result["_action"]["id"], "t"))
    assert threads.for_note("School/Essay.md") == essay
