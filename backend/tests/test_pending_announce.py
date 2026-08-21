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
