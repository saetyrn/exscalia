"""Every mutation that rewrites a note must tell the LiveBus, and task writes
must respect the OMNIA mutation lock — a board click cannot interleave into
the middle of a bulk OMNIA operation, and an open editor learns about task
edits instead of forking a conflict copy on its next autosave."""
import asyncio
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.index.db import content_hash
from app.proofread import ProofreadStore
from app.task_service import create_task, delete_task, reorder_tasks, update_task
from app.vault.internal import TrashStore

from test_task_boards_api import make_client


def drain(queue):
    events = []
    while True:
        try:
            events.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            return events


def test_task_mutations_broadcast_note_changes(state):
    async def run():
        queue = state.live.subscribe()

        created = await create_task(state, text="hello", path="Tasks Inbox.md")
        (event,) = drain(queue)
        assert event["type"] == "note"
        assert event["path"] == "Tasks Inbox.md"
        assert event["revision"] == content_hash(state.store.read("Tasks Inbox.md"))

        await update_task(state, created["id"], status="done")
        (event,) = drain(queue)
        assert event["type"] == "note"
        assert event["revision"] == content_hash(state.store.read("Tasks Inbox.md"))

        await delete_task(state, created["id"])
        (event,) = drain(queue)
        assert event["type"] == "note"
        assert event["path"] == "Tasks Inbox.md"

    asyncio.run(run())


def test_reorder_broadcasts_every_note_it_touches(state):
    async def run():
        first = await create_task(state, text="one", path="Tasks Inbox.md")
        second = await create_task(state, text="two", path="Other Tasks.md")
        queue = state.live.subscribe()

        await reorder_tasks(state, [second["id"], first["id"]])
        events = drain(queue)
        assert {e["path"] for e in events} == {"Tasks Inbox.md", "Other Tasks.md"}
        for event in events:
            assert event["revision"] == content_hash(state.store.read(event["path"]))

    asyncio.run(run())


def test_note_endpoints_broadcast(state):
    from app.api import notes as notes_api

    state.trash = TrashStore(state.settings.vault_dir, state.store)
    state.proofread = ProofreadStore(state.settings.data_dir)
    client = make_client(state, notes_api.router)
    queue = state.live.subscribe()

    client.post("/api/notes", json={"path": "Fresh.md", "content": "# Fresh\n"})
    (event,) = drain(queue)
    assert (event["type"], event["path"]) == ("note", "Fresh.md")

    client.post("/api/notes/Fresh.md/rename", json={"new_path": "Moved.md"})
    events = drain(queue)
    assert ("note_removed", "Fresh.md") in [(e["type"], e["path"]) for e in events]
    assert ("note", "Moved.md") in [(e["type"], e["path"]) for e in events]

    client.delete("/api/notes/Moved.md")
    (event,) = drain(queue)
    assert (event["type"], event["path"]) == ("note_removed", "Moved.md")


def test_rename_echo_carries_the_renamers_client_id(state):
    """The client that renames must be able to recognise its own note_removed
    echo, or the open editor reads its own rename as a remote deletion and
    kicks the user to home."""
    from app.api import notes as notes_api

    state.trash = TrashStore(state.settings.vault_dir, state.store)
    state.proofread = ProofreadStore(state.settings.data_dir)
    client = make_client(state, notes_api.router)

    client.post("/api/notes", json={"path": "a/Keep.md", "content": "# Keep\n"})
    queue = state.live.subscribe()

    client.post(
        "/api/notes/a/Keep.md/rename",
        json={"new_path": "a/Kept.md", "client_id": "device-1"},
    )
    for event in drain(queue):
        assert event["origin"] == "device-1", event

    client.post(
        "/api/notes/folders/rename",
        json={"path": "a", "new_path": "b", "client_id": "device-1"},
    )
    for event in drain(queue):
        assert event["origin"] == "device-1", event


def test_task_api_takes_the_omnia_mutation_lock(state):
    """The endpoint guard hands back the coordinator's own lock, and a caller
    blocks while OMNIA holds it."""
    from app.api.tasks import _mutations
    from app.overseer.actions import ActionCoordinator

    state.actions = ActionCoordinator(state)

    async def run():
        request = SimpleNamespace(app=SimpleNamespace(state=state))
        lock = _mutations(request)
        assert lock is state.actions.mutation_lock

        release = asyncio.Event()

        async def omnia_bulk_edit():
            async with state.actions.mutation_lock:
                await release.wait()

        holder = asyncio.create_task(omnia_bulk_edit())
        await asyncio.sleep(0)  # let it acquire

        waiter = asyncio.create_task(lock.acquire())
        done, _pending = await asyncio.wait({waiter}, timeout=0.05)
        assert not done, "task write slipped past the held mutation lock"

        release.set()
        await holder
        await asyncio.wait_for(waiter, 1)
        lock.release()

    asyncio.run(run())


def test_rename_returns_fresh_revision_and_content(state):
    """The client rebinds the open note in place after a rename instead of
    reloading it, so the response must carry the post-rename content and its
    hash — align_title may have rewritten the frontmatter title."""
    from app.api import notes as notes_api

    state.trash = TrashStore(state.settings.vault_dir, state.store)
    state.proofread = ProofreadStore(state.settings.data_dir)
    client = make_client(state, notes_api.router)

    client.post(
        "/api/notes", json={"path": "Old.md", "content": "---\ntitle: Old\n---\n\nbody\n"}
    )
    result = client.post(
        "/api/notes/Old.md/rename", json={"new_path": "New.md"}
    ).json()
    on_disk = state.store.read("New.md")
    assert result["path"] == "New.md"
    assert result["content"] == on_disk
    assert result["revision"] == content_hash(on_disk)
    assert "title: New" in on_disk  # align_title ran, and the reply reflects it
