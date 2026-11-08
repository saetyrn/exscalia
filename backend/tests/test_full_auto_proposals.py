"""Full auto applies review-tray proposals the moment they are staged: each
lands as an undoable action, counted as unseen until the user looks."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from app.api import overseer as overseer_api
from app.overseer.tools import TurnTaint, execute_tool


@pytest.fixture
def auto_state(acting_state):
    state = acting_state
    state.pending.actions = state.actions
    state.omnia_full_auto = True
    content = state.store.create("Plan.md", "first draft\n")
    asyncio.run(state.indexer.index_note("Plan.md", content))
    asyncio.run(state.vault.commit_paths("Add fixture", ["Plan.md"]))
    return state


def run(state, name, inp, taint=None):
    return json.loads(
        asyncio.run(execute_tool(state, "thread-a", name, inp, taint=taint))
    )


def tainted() -> TurnTaint:
    taint = TurnTaint()
    taint.add("note text")
    return taint


def counts(state) -> dict:
    request = SimpleNamespace(app=SimpleNamespace(state=state))
    return asyncio.run(overseer_api.pending_count(request))


def test_full_auto_applies_a_proposed_edit_as_an_undoable_action(auto_state):
    state = auto_state
    result = run(state, "propose_note_edit", {"path": "Plan.md", "new_content": "second draft\n"})

    assert "staged" not in result
    assert result["applied"] == "edit Plan.md"
    assert result["_action"]["tool"] == "propose_note_edit"
    assert result["_action"]["undoable"] is True
    assert "second draft" in state.store.read("Plan.md")
    assert counts(state) == {"count": 0, "applied": 1}

    asyncio.run(state.actions.undo(result["_action"]["id"], "thread-a"))
    assert "first draft" in state.store.read("Plan.md")
    # Undone is dealt with: the corner box stops counting it.
    assert counts(state)["applied"] == 0


def test_full_auto_applies_create_move_and_untainted_delete(auto_state):
    state = auto_state
    run(state, "propose_note_create", {"path": "New.md", "content": "hello\n"})
    run(state, "propose_note_move", {"path": "New.md", "new_path": "Moved.md"})
    assert state.store.exists("Moved.md") and not state.store.exists("New.md")
    run(state, "propose_note_delete", {"path": "Moved.md"})
    assert not state.store.exists("Moved.md")
    assert counts(state) == {"count": 0, "applied": 3}


def test_without_full_auto_a_proposal_waits_in_the_tray(auto_state):
    state = auto_state
    state.omnia_full_auto = False
    result = run(state, "propose_note_edit", {"path": "Plan.md", "new_content": "second\n"})
    assert result["staged"] is True
    assert "first draft" in state.store.read("Plan.md")
    assert counts(state) == {"count": 1, "applied": 0}


def test_a_tainted_delete_stays_in_the_tray_but_a_tainted_edit_applies(auto_state):
    state = auto_state
    deleted = run(state, "propose_note_delete", {"path": "Plan.md"}, taint=tainted())
    assert deleted["staged"] is True
    assert state.store.exists("Plan.md")

    asyncio.run(state.pending.discard_all())
    edited = run(
        state, "propose_note_edit", {"path": "Plan.md", "new_content": "edited\n"}, taint=tainted()
    )
    assert edited["applied"] == "edit Plan.md"
    assert counts(state) == {"count": 0, "applied": 1}


def test_a_proposal_building_on_a_staged_one_waits_beside_it(auto_state):
    state = auto_state
    state.omnia_full_auto = False
    run(state, "propose_note_create", {"path": "Draft.md", "content": "one\n"})
    state.omnia_full_auto = True
    result = run(state, "propose_note_edit", {"path": "Draft.md", "new_content": "two\n"})
    assert result["staged"] is True
    assert "Left in the review tray" in result["note"]
    assert counts(state) == {"count": 2, "applied": 0}


def test_seen_clears_the_count_and_review_approvals_are_never_counted(auto_state):
    state = auto_state
    run(state, "propose_note_edit", {"path": "Plan.md", "new_content": "auto\n"})
    request = SimpleNamespace(app=SimpleNamespace(state=state))
    assert asyncio.run(overseer_api.pending_applied_seen(request)) == {"seen": 1}
    assert counts(state)["applied"] == 0
    seen = [a for a in state.actions.list() if a.get("auto_applied")]
    assert seen and seen[0]["seen_at"]

    state.omnia_full_auto = False
    staged = run(state, "propose_note_edit", {"path": "Plan.md", "new_content": "by hand\n"})
    asyncio.run(state.pending.approve(staged["pending_id"]))
    assert counts(state) == {"count": 0, "applied": 0}
    assert state.actions.list()[0]["tool"] == "approve_pending"


def test_a_malformed_record_neither_counts_nor_blocks_dismiss(auto_state):
    state = auto_state
    run(state, "propose_note_edit", {"path": "Plan.md", "new_content": "auto\n"})
    state.actions.root.joinpath("bad.json").write_text(
        json.dumps({"id": "bad", "auto_applied": True, "seen_at": None}), encoding="utf-8"
    )
    assert counts(state)["applied"] == 1
    assert asyncio.run(state.actions.mark_auto_applied_seen()) == 1
    assert counts(state)["applied"] == 0
