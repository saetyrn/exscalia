"""Approving staged OMNIA proposals must announce on the LiveBus — otherwise
an open editor never learns its note changed and only discovers the edit as a
save conflict. Mirrors the drain-queue pattern of test_live_consistency."""
import asyncio

from app.index.db import content_hash
from app.overseer.actions import ActionCoordinator


def drain(queue):
    events = []
    while True:
        try:
            events.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            return events


def wire_actions(state):
    # conftest's state has no coordinator; production wires both (main.py).
    state.actions = ActionCoordinator(state)
    state.pending.actions = state.actions
    return state


def test_approve_announces_the_changed_note(state):
    wire_actions(state)

    async def run():
        state.store.create("Draft.md", "original body\n")
        await state.indexer.index_note("Draft.md", state.store.read("Draft.md"))
        change_id = await state.pending.stage(
            "s", "edit", "Draft.md", new_content="rewritten body\n"
        )
        queue = state.live.subscribe()
        await state.pending.approve(change_id)
        events = drain(queue)
        assert ("note", "Draft.md") in {(e["type"], e["path"]) for e in events}
        (event,) = [e for e in events if e["path"] == "Draft.md" and e["type"] == "note"]
        assert event["revision"] == content_hash(state.store.read("Draft.md"))

    asyncio.run(run())


def test_approve_delete_announces_removed(state):
    wire_actions(state)

    async def run():
        state.store.create("Doomed.md", "goodbye\n")
        await state.indexer.index_note("Doomed.md", state.store.read("Doomed.md"))
        # Track it in git first (production autocommit would have) so the
        # approval's scoped commit of the deletion has a pathspec to match.
        await state.vault.commit_all("seed")
        change_id = await state.pending.stage("s", "delete", "Doomed.md")
        queue = state.live.subscribe()
        await state.pending.approve(change_id)
        assert ("note_removed", "Doomed.md") in {
            (e["type"], e["path"]) for e in drain(queue)
        }

    asyncio.run(run())


def test_approved_delete_goes_to_trash_and_is_undoable(state):
    # Production hands PendingStore the Trash (main.py), so an approved delete
    # is recoverable twice over: from the Trash by name, and as an undoable
    # action whose scoped commit removed the note.
    wire_actions(state)

    async def run():
        state.store.create("Doomed.md", "goodbye\n")
        await state.indexer.index_note("Doomed.md", state.store.read("Doomed.md"))
        await state.vault.commit_all("seed")
        original = state.store.read("Doomed.md")
        change_id = await state.pending.stage("s", "delete", "Doomed.md")
        result = await state.pending.approve(change_id)

        assert not state.store.exists("Doomed.md")
        (item,) = state.trash.list()
        assert item["kind"] == "note"
        assert item["original_path"] == "Doomed.md"

        # The commit is scoped to the note, so the Trash payload is not in it;
        # the action record is what makes the approval undoable.
        (action,) = [a for a in state.actions.list(10) if a["tool"] == "approve_pending"]
        assert action["commit"] == result["commit"] and action["commit"]
        assert action["undoable"] is True
        assert action["paths"] == ["Doomed.md"]

        restored = state.trash.restore(item["id"])
        assert restored["path"] == "Doomed.md"
        assert state.store.read("Doomed.md") == original
        assert state.trash.list() == []

    asyncio.run(run())


def test_approve_all_announces_every_touched_path(state):
    wire_actions(state)

    async def run():
        state.store.create("Edit Me.md", "before\n")
        await state.indexer.index_note("Edit Me.md", state.store.read("Edit Me.md"))
        state.store.create("Remove Me.md", "bye\n")
        await state.indexer.index_note("Remove Me.md", state.store.read("Remove Me.md"))
        await state.pending.stage("s", "edit", "Edit Me.md", new_content="after\n")
        await state.pending.stage("s", "create", "Fresh.md", new_content="new note\n")
        await state.pending.stage("s", "delete", "Remove Me.md")

        queue = state.live.subscribe()
        result = await state.pending.approve_all()
        assert len(result["applied"]) == 3
        seen = {(e["type"], e["path"]) for e in drain(queue)}
        assert ("note", "Edit Me.md") in seen
        assert ("note", "Fresh.md") in seen
        assert ("note_removed", "Remove Me.md") in seen

    asyncio.run(run())


def _move_events(events):
    return [(e["type"], e["path"], e.get("new_path")) for e in events]


def test_approve_all_announces_a_staged_move_as_a_move(state):
    """approve_all announced its touched paths one by one, so a staged move
    reached an open editor as a deletion of the old path, and the note page
    left for '/' and flushed its typing to the dead path."""
    wire_actions(state)

    async def run():
        state.store.create("Plan.md", "# Plan\n\nsteps\n")
        await state.indexer.index_note("Plan.md", state.store.read("Plan.md"))
        state.store.create("Mine.md", "# Mine\n\nbody\n")
        await state.vault.commit_all("seed")
        await state.pending.stage("s", "move", "Plan.md", new_path="Work/Plan.md")
        # An unrelated edit of the user's that the sweeping commit takes too.
        state.store.write_raw("Mine.md", "# Mine\n\nedited\n")

        queue = state.live.subscribe()
        await state.pending.approve_all()
        revision = content_hash(state.store.read("Work/Plan.md"))
        events = drain(queue)
        assert _move_events(events) == [
            ("note_moved", "Plan.md", "Work/Plan.md"),
            # The pair clients that predate note_moved read.
            ("note_removed", "Plan.md", None),
            ("note", "Work/Plan.md", None),
        ]
        assert events[0]["revision"] == events[2]["revision"] == revision

    asyncio.run(run())


def test_approve_all_announces_a_chain_of_moves_as_one_move(state):
    wire_actions(state)

    async def run():
        state.store.create("Plan.md", "# Plan\n\nsteps\n")
        await state.indexer.index_note("Plan.md", state.store.read("Plan.md"))
        await state.vault.commit_all("seed")
        await state.pending.stage("s", "move", "Plan.md", new_path="Work/Plan.md")
        await state.pending.stage("s", "move", "Work/Plan.md", new_path="Done/Plan.md")

        queue = state.live.subscribe()
        await state.pending.approve_all()
        events = _move_events(drain(queue))
        assert events[:3] == [
            ("note_moved", "Plan.md", "Done/Plan.md"),
            ("note_removed", "Plan.md", None),
            ("note", "Done/Plan.md", None),
        ]
        # The stop in between never reached a client; its removal is inert.
        assert events[3:] == [("note_removed", "Work/Plan.md", None)]

    asyncio.run(run())


def test_approve_without_actions_wired_still_works(state):
    # Graceful degrade: no coordinator (as in a bare test app) — approval
    # applies fine, it just cannot announce.
    async def run():
        state.store.create("Quiet.md", "body\n")
        await state.indexer.index_note("Quiet.md", state.store.read("Quiet.md"))
        change_id = await state.pending.stage("s", "edit", "Quiet.md", new_content="new\n")
        queue = state.live.subscribe()
        await state.pending.approve(change_id)
        assert state.store.read("Quiet.md") == "new\n" or "new" in state.store.read("Quiet.md")
        assert drain(queue) == []

    asyncio.run(run())


async def _stage_three(state):
    state.store.create("Edit Me.md", "before\n")
    await state.indexer.index_note("Edit Me.md", state.store.read("Edit Me.md"))
    # Seed commit, so approve-all's sweeping commit shows only what it applied.
    await state.vault.commit_all("seed")
    return [
        await state.pending.stage("s", "edit", "Edit Me.md", new_content="after\n"),
        await state.pending.stage("s", "create", "Seen.md", new_content="reviewed\n"),
        # Staged after the review page loaded (another device, the scheduler).
        await state.pending.stage("s", "create", "Unseen.md", new_content="unreviewed\n"),
    ]


def test_approve_all_applies_only_the_reviewed_ids(state):
    wire_actions(state)

    async def run():
        first, second, third = await _stage_three(state)
        result = await state.pending.approve_all(ids=[first, second])
        assert len(result["applied"]) == 2
        assert result["remaining"] == 1
        assert [c.id for c in await state.pending.list_changes()] == [third]
        assert not state.store.exists("Unseen.md")
        committed = set(state.vault.repo.commit(result["commit"]).stats.files)
        assert committed == {"Edit Me.md", "Seen.md"}

    asyncio.run(run())


def test_approve_all_with_unknown_ids_applies_nothing(state):
    wire_actions(state)

    async def run():
        await _stage_three(state)
        result = await state.pending.approve_all(ids=[999])
        assert result == {"applied": [], "commit": None, "remaining": 3}
        assert len(await state.pending.list_changes()) == 3

    asyncio.run(run())


def test_approve_all_endpoint_forwards_ids(state):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import overseer as overseer_api

    wire_actions(state)
    first, _second, _third = asyncio.run(_stage_three(state))
    app = FastAPI()
    app.state = state
    app.include_router(overseer_api.router)
    client = TestClient(app)

    reviewed = client.post("/api/pending-approve-all", json={"ids": [first]}).json()
    assert (len(reviewed["applied"]), reviewed["remaining"]) == (1, 2)
    # No ids: the id-less contract (the corner PendingBox) still applies everything.
    rest = client.post("/api/pending-approve-all", json={}).json()
    assert (len(rest["applied"]), rest["remaining"]) == (2, 0)
