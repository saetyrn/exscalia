"""Every mutation that rewrites a note must tell the LiveBus, and task writes
must respect the OMNIA mutation lock — a board click cannot interleave into
the middle of a bulk OMNIA operation, and an open editor learns about task
edits instead of forking a conflict copy on its next autosave."""
import asyncio
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.index.db import content_hash
from app.overseer.tools import execute_tool
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
    # The move first; the removal + appearance pair still follows it for
    # clients that predate note_moved.
    event, *legacy = drain(queue)
    assert (event["type"], event["path"], event["new_path"]) == (
        "note_moved",
        "Fresh.md",
        "Moved.md",
    )
    assert [(e["type"], e["path"]) for e in legacy] == [
        ("note_removed", "Fresh.md"),
        ("note", "Moved.md"),
    ]

    client.delete("/api/notes/Moved.md")
    (event,) = drain(queue)
    assert (event["type"], event["path"]) == ("note_removed", "Moved.md")


def test_link_pin_conflict_copy_and_daily_broadcast(state):
    """The quieter writers: a Related-panel link, a pin, a conflict copy and a
    new daily note each change what another device shows."""
    from app.api import meta as meta_api
    from app.api import notes as notes_api

    state.trash = TrashStore(state.settings.vault_dir, state.store)
    state.proofread = ProofreadStore(state.settings.data_dir)
    client = make_client(state, notes_api.router)
    client.post("/api/notes", json={"path": "Hub.md", "content": "# Hub\n"})
    client.post("/api/notes", json={"path": "Spoke.md", "content": "# Spoke\n"})
    queue = state.live.subscribe()

    base = content_hash(state.store.read("Hub.md"))
    linked = client.post(
        "/api/notes/Hub.md/add-link",
        json={"target": "Spoke.md", "base_revision": base, "client_id": "device-1"},
    ).json()
    on_disk = state.store.read("Hub.md")
    assert "[[Spoke]]" in on_disk
    assert (linked["status"], linked["linked"]) == ("saved", "Spoke")
    assert linked["content"] == on_disk
    assert linked["revision"] == content_hash(on_disk)
    (event,) = drain(queue)
    assert (event["type"], event["path"], event["origin"]) == ("note", "Hub.md", "device-1")
    assert event["revision"] == linked["revision"]

    # Appending to a version the editor never saw is refused, not merged blind.
    stale = client.post(
        "/api/notes/Hub.md/add-link", json={"target": "Spoke.md", "base_revision": base}
    )
    assert stale.status_code == 409
    assert state.store.read("Hub.md") == on_disk
    assert drain(queue) == []

    client.post("/api/notes/Spoke.md/pin", json={"pinned": True, "client_id": "device-1"})
    (event,) = drain(queue)
    assert (event["type"], event["path"], event["origin"]) == ("note", "Spoke.md", "device-1")
    assert event["revision"] == content_hash(state.store.read("Spoke.md"))

    saved = client.put(
        "/api/notes/Hub.md", json={"content": "# Hub\n\nmine\n", "base_revision": base}
    ).json()
    assert saved["status"] == "conflict_copy"
    (event,) = drain(queue)
    assert (event["type"], event["path"], event["origin"]) == (
        "note",
        saved["conflict_path"],
        None,
    )
    assert event["revision"] == saved["conflict_revision"]

    daily = make_client(state, meta_api.router)
    assert daily.post("/api/daily/2026-01-05").json()["created"] is True
    (event,) = drain(queue)
    assert (event["type"], event["path"]) == ("note", "daily/2026-01-05.md")
    assert event["revision"] == content_hash(state.store.read("daily/2026-01-05.md"))
    # Opening it again writes nothing, so says nothing.
    assert daily.post("/api/daily/2026-01-05").json()["created"] is False
    assert drain(queue) == []


def test_rename_echo_carries_the_renamers_client_id(state):
    """The client that renames must be able to recognise its own note_moved
    echo; it has already rebound the open editor in place."""
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


def test_rename_announces_a_move_ahead_of_the_removal(state):
    """A device with the note open follows a rename to the new path instead of
    reading the note_removed half as a deletion. The move goes out first, per
    note, and carries the revision the new path holds; the removed/changed
    pair stays behind it for clients that predate the event."""
    from app.api import notes as notes_api

    state.trash = TrashStore(state.settings.vault_dir, state.store)
    state.proofread = ProofreadStore(state.settings.data_dir)
    client = make_client(state, notes_api.router)

    client.post("/api/notes", json={"path": "a/One.md", "content": "# One\n"})
    client.post("/api/notes", json={"path": "a/Two.md", "content": "# Two\n"})
    queue = state.live.subscribe()

    client.post(
        "/api/notes/a/One.md/rename",
        json={"new_path": "a/Uno.md", "client_id": "device-1"},
    )
    moved, removed, changed = drain(queue)
    assert moved == {
        "type": "note_moved",
        "path": "a/One.md",
        "new_path": "a/Uno.md",
        "revision": content_hash(state.store.read("a/Uno.md")),
        "origin": "device-1",
    }
    assert (removed["type"], removed["path"]) == ("note_removed", "a/One.md")
    assert (changed["type"], changed["path"]) == ("note", "a/Uno.md")
    assert changed["revision"] == moved["revision"]

    client.post(
        "/api/notes/folders/rename",
        json={"path": "a", "new_path": "b"},
        headers={"X-Client-Id": "device-2"},
    )
    events = drain(queue)
    assert len(events) == 6
    seen = set()
    for moved, removed, changed in (events[i : i + 3] for i in range(0, 6, 3)):
        assert moved["type"] == "note_moved"
        assert moved["new_path"] == "b/" + moved["path"].removeprefix("a/")
        assert moved["revision"] == content_hash(state.store.read(moved["new_path"]))
        assert moved["origin"] == "device-2"
        assert removed == {
            "type": "note_removed",
            "path": moved["path"],
            "origin": "device-2",
        }
        assert (changed["type"], changed["path"]) == ("note", moved["new_path"])
        assert changed["revision"] == moved["revision"]
        seen.add(moved["path"])
    assert seen == {"a/Uno.md", "a/Two.md"}

    # Retitling in place moves nothing.
    client.post(
        "/api/notes/b/Two.md/rename",
        json={"new_path": "b/Two.md", "new_title": "Two"},
    )
    assert "note_moved" not in [e["type"] for e in drain(queue)]


def test_delete_echo_carries_the_deleters_client_id(state):
    """A delete has no body, so the origin rides on the X-Client-Id header the
    app sends with every request. Without it, trashing the open note reads as
    a remote deletion: a red toast and a second navigation home."""
    from app.api import notes as notes_api

    state.trash = TrashStore(state.settings.vault_dir, state.store)
    state.proofread = ProofreadStore(state.settings.data_dir)
    client = make_client(state, notes_api.router)
    header = {"X-Client-Id": "device-1"}

    client.post("/api/notes", json={"path": "a/Keep.md", "content": "# Keep\n"})
    client.post("/api/notes", json={"path": "b/Inside.md", "content": "# Inside\n"})
    queue = state.live.subscribe()

    assert client.delete("/api/notes/a/Keep.md", headers=header).status_code == 200
    (event,) = drain(queue)
    assert (event["type"], event["path"]) == ("note_removed", "a/Keep.md")
    assert event["origin"] == "device-1"

    assert client.delete("/api/notes/folders/b", headers=header).status_code == 200
    events = drain(queue)
    assert [(e["type"], e["path"]) for e in events] == [("note_removed", "b/Inside.md")]
    for event in events:
        assert event["origin"] == "device-1", event

    # The header is also the default for a rename that names no client_id.
    client.post("/api/notes", json={"path": "c/Old.md", "content": "# Old\n"})
    drain(queue)
    client.post("/api/notes/c/Old.md/rename", json={"new_path": "c/New.md"}, headers=header)
    events = drain(queue)
    assert events
    for event in events:
        assert event["origin"] == "device-1", event

    client.post("/api/notes/folders/rename", json={"path": "c", "new_path": "d"}, headers=header)
    events = drain(queue)
    assert events
    for event in events:
        assert event["origin"] == "device-1", event

    # No header, no body id: still an anonymous (remote) change.
    client.delete("/api/notes/d/New.md")
    (event,) = drain(queue)
    assert event["origin"] is None


def test_desktop_preflight_allows_the_client_id_header():
    """The desktop SPA is cross-origin and every request carries x-client-id;
    a preflight that refused it would fail every desktop request."""
    from app.main import app

    res = TestClient(app).options(
        "/api/notes/a.md",
        headers={
            "Origin": "tauri://localhost",
            "Access-Control-Request-Method": "DELETE",
            "Access-Control-Request-Headers": "authorization, x-client-id",
        },
    )
    assert res.status_code == 200
    assert "x-client-id" in res.headers["access-control-allow-headers"].lower()


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


# --- moves: one note_moved per note, then the pair older clients read -------


def _seed_school(state):
    """Two committed notes under School/, the shape of the reported case."""
    for name in ("Essay", "Reading"):
        path = f"School/{name}.md"
        content = state.store.create(path, f"# {name}\n\nDraft paragraph.\n")
        asyncio.run(state.indexer.index_note(path, content))
    asyncio.run(state.vault.commit_all("seed"))


def _moves(state, events):
    """Each move as (old, new), checking it went out as note_moved followed by
    the removal + appearance pair: the installed desktop build drops
    note_moved and hears about the move only through that pair."""
    assert len(events) % 3 == 0, events
    moves = []
    for moved, removed, appeared in zip(events[::3], events[1::3], events[2::3]):
        assert moved["type"] == "note_moved", moved
        old, new = moved["path"], moved["new_path"]
        revision = content_hash(state.store.read(new))
        assert moved["revision"] == revision
        assert (removed["type"], removed["path"]) == ("note_removed", old)
        assert (appeared["type"], appeared["path"], appeared["revision"]) == (
            "note",
            new,
            revision,
        )
        for event in (moved, removed, appeared):
            assert event["origin"] is None  # OMNIA's own chat tab must react too
        moves.append((old, new))
    return sorted(moves)


def _run_tool(state, name, inp, **kwargs):
    result = json.loads(asyncio.run(execute_tool(state, "t", name, inp, **kwargs)))
    assert "error" not in result, result
    return result


def test_omnia_folder_move_announces_each_note_as_moved(acting_state):
    """move_folder records folder names, which name no note, so this used to
    announce nothing and an open editor kept saving to the old path."""
    state = acting_state
    _seed_school(state)
    queue = state.live.subscribe()

    result = _run_tool(state, "move_folder", {"path": "School", "new_path": "Archive/School"})

    assert _moves(state, drain(queue)) == [
        ("School/Essay.md", "Archive/School/Essay.md"),
        ("School/Reading.md", "Archive/School/Reading.md"),
    ]

    # Undo announces the revert's diff: the notes move back.
    asyncio.run(state.actions.undo(result["_action"]["id"], "t"))
    assert _moves(state, drain(queue)) == [
        ("Archive/School/Essay.md", "School/Essay.md"),
        ("Archive/School/Reading.md", "School/Reading.md"),
    ]


def test_omnia_folder_delete_announces_each_note_removed(acting_state):
    """delete_folder moves the notes into the Trash, which git sees as renames
    into .exscalia/ — that is a removal, never a move into the Trash."""
    state = acting_state
    _seed_school(state)
    queue = state.live.subscribe()

    result = _run_tool(state, "delete_folder", {"path": "School"}, confirmed=True)

    assert sorted((e["type"], e["path"]) for e in drain(queue)) == [
        ("note_removed", "School/Essay.md"),
        ("note_removed", "School/Reading.md"),
    ]

    # Undoing it brings the notes back as plain appearances.
    asyncio.run(state.actions.undo(result["_action"]["id"], "t"))
    events = drain(queue)
    assert sorted((e["type"], e["path"]) for e in events) == [
        ("note", "School/Essay.md"),
        ("note", "School/Reading.md"),
    ]
    for event in events:
        assert event["revision"] == content_hash(state.store.read(event["path"]))


def test_omnia_move_note_is_one_move_even_for_a_short_note(acting_state):
    """Moving rewrites the frontmatter title and modified stamp — half of a
    short note, below git's rename threshold — and it must still read as a
    move, not a deletion elsewhere."""
    state = acting_state
    # Written as it sits on disk after a day: the move restamps `modified`.
    (state.settings.vault_dir / "Fresh.md").write_text(
        "---\ntitle: Fresh\ncreated: '2026-01-01T00:00:00Z'\n"
        "modified: '2026-01-01T00:00:00Z'\n---\n\n",
        encoding="utf-8",
    )
    asyncio.run(state.vault.commit_all("seed"))
    queue = state.live.subscribe()

    _run_tool(
        state,
        "move_note",
        {
            "path": "Fresh.md",
            "new_path": "School/Essay.md",
            "base_revision": content_hash(state.store.read("Fresh.md")),
        },
    )

    assert _moves(state, drain(queue)) == [("Fresh.md", "School/Essay.md")]


def test_notes_an_autosave_took_mid_tool_are_still_announced(acting_state):
    """The autosave commit can land while a multi-note tool awaits between
    writes. OMNIA's scoped commit then holds only the notes written after it,
    and the ones the autosave took were never announced, so open editors kept
    the old text."""
    state = acting_state
    for name in ("One", "Two"):
        state.store.create(f"{name}.md", f"# {name}\n\nold\n")
    asyncio.run(state.vault.commit_all("seed"))
    queue = state.live.subscribe()

    state.store.write_raw("One.md", "# One\n\nnew\n")
    asyncio.run(state.vault.commit_all("autosave"))  # takes One.md
    state.store.write_raw("Two.md", "# Two\n\nnew\n")
    result = asyncio.run(
        state.actions.record(
            thread_id="t",
            tool="find_replace_across_vault",
            summary="old -> new",
            paths=["One.md", "Two.md"],
            refresh=["notes"],
            result={},
        )
    )

    committed = set(state.vault.repo.commit(result["_action"]["commit"]).stats.files)
    assert committed == {"Two.md"}
    events = drain(queue)
    assert sorted((e["type"], e["path"], e["revision"]) for e in events) == [
        ("note", "One.md", content_hash(state.store.read("One.md"))),
        ("note", "Two.md", content_hash(state.store.read("Two.md"))),
    ]


def test_unrelated_delete_and_create_are_not_a_move(state):
    """Git calls deleting one note and creating a similar one (the same
    template, the same boilerplate) a rename. Different `created` stamps mean
    different notes: that must stay a removal and an appearance, or an editor
    open on the deleted note would rebind onto a stranger."""
    boilerplate = "".join(f"- [ ] step {i}\n" for i in range(12))
    stamped = "---\ntitle: {t}\ncreated: '{c}'\n---\n\n" + boilerplate
    state.store.create("Old.md", stamped.format(t="Old", c="2026-01-01T00:00:00Z"))
    asyncio.run(state.vault.commit_all("seed"))
    state.store.delete("Old.md")
    state.store.create("New.md", stamped.format(t="New", c="2026-09-01T00:00:00Z"))
    sha = asyncio.run(
        state.vault.commit_paths("swap", ["Old.md", "New.md"], author=state.vault.user_author)
    )
    raw = state.vault.repo.git.diff_tree("-r", "-M", "--no-commit-id", "--name-status", sha)
    assert raw.startswith("R"), raw  # what git alone would have announced

    changes = asyncio.run(state.vault.changed_paths(sha))
    assert sorted(changes) == [("A", "New.md", None), ("D", "Old.md", None)]


def test_api_renames_publish_moves_with_the_renamers_origin(state):
    from app.api import notes as notes_api

    state.trash = TrashStore(state.settings.vault_dir, state.store)
    state.proofread = ProofreadStore(state.settings.data_dir)
    client = make_client(state, notes_api.router)
    client.post("/api/notes", json={"path": "a/One.md", "content": "# One\n"})
    queue = state.live.subscribe()

    client.post(
        "/api/notes/a/One.md/rename",
        json={"new_path": "a/Two.md", "client_id": "device-1"},
    )
    event, *legacy = drain(queue)
    assert (event["type"], event["path"], event["new_path"], event["origin"]) == (
        "note_moved",
        "a/One.md",
        "a/Two.md",
        "device-1",
    )
    assert event["revision"] == content_hash(state.store.read("a/Two.md"))
    # The pair older clients read carries the renamer's origin too.
    assert [(e["type"], e["path"], e["origin"]) for e in legacy] == [
        ("note_removed", "a/One.md", "device-1"),
        ("note", "a/Two.md", "device-1"),
    ]

    client.post(
        "/api/notes/folders/rename",
        json={"path": "a", "new_path": "b", "client_id": "device-1"},
    )
    event, *legacy = drain(queue)
    assert (event["type"], event["path"], event["new_path"], event["origin"]) == (
        "note_moved",
        "a/Two.md",
        "b/Two.md",
        "device-1",
    )


def test_a_large_folder_rename_reaches_a_subscriber_whole(state):
    """Three events per note: at a 64-event queue, a rename of 22 notes lost
    the tail, and an editor open on one of those never learned it moved."""
    from app.api import notes as notes_api

    state.trash = TrashStore(state.settings.vault_dir, state.store)
    state.proofread = ProofreadStore(state.settings.data_dir)
    client = make_client(state, notes_api.router)
    for index in range(60):
        client.post("/api/notes", json={"path": f"big/N{index}.md", "content": "x\n"})
    queue = state.live.subscribe()

    client.post("/api/notes/folders/rename", json={"path": "big", "new_path": "large"})

    moved = [e for e in drain(queue) if e["type"] == "note_moved"]
    assert len(moved) == 60
