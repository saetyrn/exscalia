"""OMNIA can see its own work: the review tray, its action log, undo, and the
queued reminders/confirmations."""
import asyncio

import pytest

from app.alerts import AlertQueue
from app.overseer.tool_expansion import (
    create_note,
    list_pending,
    list_queued_reminders,
    list_recent_actions,
    undo_last_action,
)
from app.vault.notes import NoteError


# --- list_pending ----------------------------------------------------------


def test_list_pending_reports_staged_changes(acting_state):
    asyncio.run(
        acting_state.pending.stage(
            "t", "create", "idea.md", new_content="# Idea\n\nthe body", rationale="worth it"
        )
    )

    result = asyncio.run(list_pending(acting_state, "t", {}))

    assert result["count"] == 1
    change = result["pending"][0]
    assert change["path"] == "idea.md"
    assert change["op"] == "create"
    assert "the body" in change["preview"]


# --- list_recent_actions ---------------------------------------------------


def test_list_recent_actions_projects_safe_fields(acting_state):
    asyncio.run(create_note(acting_state, "t", {"path": "a.md", "content": "# A\n\nsecret body"}))

    result = asyncio.run(list_recent_actions(acting_state, "t", {"limit": 5}))

    top = result["actions"][0]
    assert top["tool"] == "create_note"
    assert "a.md" in (top["paths"] or [])
    assert "result" not in top  # raw payload (which holds note content) not leaked


# --- undo_last_action ------------------------------------------------------


def test_undo_last_action_reverts_the_change(acting_state):
    asyncio.run(create_note(acting_state, "t", {"path": "gone.md", "content": "# Gone"}))
    assert acting_state.store.exists("gone.md")

    result = asyncio.run(undo_last_action(acting_state, "t", {}))

    assert result["ok"] is True
    assert not acting_state.store.exists("gone.md")


def test_undo_last_action_with_nothing_to_undo(acting_state):
    with pytest.raises(NoteError):
        asyncio.run(undo_last_action(acting_state, "t", {}))


def test_undo_is_scoped_to_this_thread(acting_state):
    asyncio.run(create_note(acting_state, "thread-A", {"path": "x.md", "content": "# X"}))
    # A different thread has nothing of its own to undo.
    with pytest.raises(NoteError):
        asyncio.run(undo_last_action(acting_state, "thread-B", {}))


# --- list_queued_reminders -------------------------------------------------


def test_list_queued_reminders_peeks_without_consuming(acting_state):
    acting_state.alerts = AlertQueue(acting_state.settings.data_dir)
    acting_state.alerts.add([{"key": "k1", "title": "Call dentist"}])
    acting_state.actions.request_confirmation("t", "delete_note", {"path": "x.md"})

    result = asyncio.run(list_queued_reminders(acting_state, "t", {}))

    assert any(a["title"] == "Call dentist" for a in result["queued_alerts"])
    assert result["pending_confirmations"]
    assert "inputs" not in result["pending_confirmations"][0]  # raw inputs not leaked
    # Peeking didn't drain the queue.
    assert acting_state.alerts.peek()
