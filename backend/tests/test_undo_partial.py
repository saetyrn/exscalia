"""Undo of a multi-note OMNIA action after some of its notes changed again.

The whole-commit revert conflicts as soon as one touched note has a newer
edit; the fallback undoes every note still exactly as the action left it and
keeps the rest.
"""
import asyncio

import pytest

from app.overseer.tool_expansion import (
    create_note,
    find_replace_across_vault,
    move_folder,
)
from app.vault.notes import NoteError


def make(state, path: str, body: str) -> str:
    asyncio.run(create_note(state, "t", {"path": path, "content": body}))
    return state.store.read(path)


def replace_everywhere(state, find: str, replace: str) -> dict:
    result = asyncio.run(
        find_replace_across_vault(state, "t", {"find": find, "replace": replace})
    )
    return result["_action"]


def user_edit(state, path: str, content: str, *, commit: bool = True) -> str:
    """Save like the editor does; returns the file as written (the store
    restamps `modified`)."""
    written = state.store.write(path, content)
    if commit:
        asyncio.run(state.vault.commit_paths("user edit", [path]))
    return written


def tracked_dirty(state) -> bool:
    return state.vault.repo.is_dirty(
        index=True, working_tree=True, untracked_files=False
    )


def test_undo_keeps_newer_edit_and_restores_the_rest(acting_state):
    state = acting_state
    make(state, "a.md", "alpha colour")
    b_before = make(state, "b.md", "beta colour")
    action = replace_everywhere(state, "colour", "color")
    edited = state.store.read("a.md").replace(
        "alpha color", "alpha color, rewritten by the user"
    )
    user_edit(state, "a.md", edited)

    result = asyncio.run(state.actions.undo(action["id"], "t"))

    assert state.store.read("b.md") == b_before
    assert "alpha color, rewritten by the user" in state.store.read("a.md")
    assert result["skipped"] == ["a.md"]
    assert result["summary"].endswith("kept newer edits in a.md")
    head = state.vault.repo.head.commit
    assert head.hexsha == result["commit"]
    assert head.message.endswith("(partial)")
    assert set(head.stats.files) == {"b.md"}
    assert not tracked_dirty(state)
    record = state.actions.get(action["id"])
    assert record["undone_at"]
    assert record["undo_commit"] == result["commit"]
    assert record["undoable"] is False
    with pytest.raises(NoteError) as err:
        asyncio.run(state.actions.undo(action["id"], "t"))
    assert err.value.status == 409


def test_undo_keeps_unsaved_autosave_in_the_working_tree(acting_state):
    state = acting_state
    make(state, "a.md", "alpha colour")
    b_before = make(state, "b.md", "beta colour")
    action = replace_everywhere(state, "colour", "color")
    # Autosaved to disk, not yet committed: still the user's newer text.
    typed = user_edit(
        state,
        "a.md",
        state.store.read("a.md").replace("alpha color", "alpha color typed"),
        commit=False,
    )

    result = asyncio.run(state.actions.undo(action["id"], "t"))

    assert result["skipped"] == ["a.md"]
    assert state.store.read("a.md") == typed
    assert state.store.read("b.md") == b_before
    assert set(state.vault.repo.head.commit.stats.files) == {"b.md"}


def test_clean_undo_reports_nothing_skipped(acting_state):
    state = acting_state
    a_before = make(state, "a.md", "alpha colour")
    b_before = make(state, "b.md", "beta colour")
    action = replace_everywhere(state, "colour", "color")

    result = asyncio.run(state.actions.undo(action["id"], "t"))

    assert result["skipped"] == []
    assert set(result) == {
        "ok", "action_id", "summary", "commit", "refresh", "skipped",
    }
    assert result["summary"] == f"Undid: {action['summary']}"
    assert state.store.read("a.md") == a_before
    assert state.store.read("b.md") == b_before
    assert not state.vault.repo.head.commit.message.endswith("(partial)")
    assert state.actions.get(action["id"])["undone_at"]


def test_undo_with_every_note_edited_still_conflicts(acting_state):
    state = acting_state
    make(state, "a.md", "alpha colour")
    action = replace_everywhere(state, "colour", "color")
    edited = user_edit(
        state,
        "a.md",
        state.store.read("a.md").replace("alpha color", "alpha color, mine"),
    )
    head = state.vault.repo.head.commit.hexsha

    with pytest.raises(NoteError) as err:
        asyncio.run(state.actions.undo(action["id"], "t"))

    assert err.value.status == 409
    assert "conflicts with newer vault changes" in err.value.detail
    assert state.store.read("a.md") == edited
    assert state.vault.repo.head.commit.hexsha == head
    assert not tracked_dirty(state)
    assert state.actions.get(action["id"])["undone_at"] is None


def test_moved_note_with_newer_edit_stays_put_as_one_unit(acting_state):
    """A folder move is recorded as the two folder paths, and each note in it
    is a rename. Restoring the old path of an edited note while keeping the
    new one would leave that note twice."""
    state = acting_state
    make(state, "Old/x.md", "x body")
    y_before = make(state, "Old/y.md", "y body")
    action = asyncio.run(
        move_folder(state, "t", {"path": "Old", "new_path": "New"})
    )["_action"]
    typed = user_edit(
        state,
        "New/x.md",
        state.store.read("New/x.md").replace("x body", "x body, typed"),
        commit=False,
    )

    result = asyncio.run(state.actions.undo(action["id"], "t"))

    assert result["skipped"] == ["New/x.md"]
    assert state.store.read("New/x.md") == typed
    assert not state.store.exists("Old/x.md")
    assert state.store.read("Old/y.md") == y_before
    assert not state.store.exists("New/y.md")
