"""OMNIA Round 3 — discovery, hygiene, and history."""
import asyncio

import pytest

from app.overseer.tool_expansion import (
    create_note,
    edit_note,
    find_broken_links,
    find_notes,
    get_forward_links,
    note_changes,
    related_notes,
    restore_note_to,
    summarize_notes,
    vault_overview,
)
from app.vault.notes import NoteError


def add_note(state, path: str, content: str) -> None:
    created = state.store.create(path, content)
    asyncio.run(state.indexer.index_note(path, created))


# --- related_notes ---------------------------------------------------------


def test_related_notes_empty_without_semantic(acting_state):
    add_note(acting_state, "n.md", "# N\n\nbody\n")
    result = asyncio.run(related_notes(acting_state, "t", {"path": "n.md"}))
    assert result["related"] == []


# --- links -----------------------------------------------------------------


def test_get_forward_links_flags_missing(acting_state):
    add_note(acting_state, "target.md", "# Target\n")
    add_note(acting_state, "src.md", "# Src\n\nSee [[target]] and [[ghost]].\n")

    result = asyncio.run(get_forward_links(acting_state, "t", {"path": "src.md"}))

    by_target = {f["target"].lower(): f for f in result["forward_links"]}
    assert by_target["target"]["exists"] is True
    assert by_target["ghost"]["exists"] is False


def test_find_broken_links(acting_state):
    add_note(acting_state, "real.md", "# Real\n")
    add_note(acting_state, "src.md", "# Src\n\n[[real]] and [[ghost]].\n")

    result = asyncio.run(find_broken_links(acting_state, "t", {}))

    targets = {b["target"].lower() for b in result["broken_links"]}
    assert "ghost" in targets and "real" not in targets


# --- find_notes ------------------------------------------------------------


def test_find_notes_orphan(acting_state):
    add_note(acting_state, "lonely.md", "# Lonely\n\nno links\n")
    add_note(acting_state, "a.md", "# A\n\n[[b]]\n")
    add_note(acting_state, "b.md", "# B\n")

    result = asyncio.run(find_notes(acting_state, "t", {"filter": "orphan"}))

    paths = {n["path"] for n in result["notes"]}
    assert "lonely.md" in paths
    assert "a.md" not in paths  # links out
    assert "b.md" not in paths  # linked to


def test_find_notes_large_sorts_biggest_first(acting_state):
    add_note(acting_state, "big.md", "# Big\n\n" + "word " * 500)

    result = asyncio.run(find_notes(acting_state, "t", {"filter": "large"}))

    assert result["notes"][0]["path"] == "big.md"


# --- vault_overview --------------------------------------------------------


def test_vault_overview(acting_state):
    add_note(acting_state, "n.md", "# N\n")
    result = asyncio.run(vault_overview(acting_state, "t", {}))
    assert result["notes"] >= 1
    assert "open_tasks" in result and "notes_by_folder" in result


# --- summarize_notes -------------------------------------------------------


def test_summarize_notes_gathers_a_folder(acting_state):
    acting_state.store.create_folder("proj")
    add_note(acting_state, "proj/a.md", "# A\n\nalpha body\n")
    add_note(acting_state, "proj/b.md", "# B\n\nbeta body\n")

    result = asyncio.run(summarize_notes(acting_state, "t", {"folder": "proj"}))

    assert {n["path"] for n in result["notes"]} == {"proj/a.md", "proj/b.md"}
    assert any("alpha body" in n["excerpt"] for n in result["notes"])


# --- note_changes / restore_note_to (git history) --------------------------


def test_note_changes_shows_a_diff(acting_state):
    made = asyncio.run(create_note(acting_state, "t", {"path": "a.md", "content": "# A\n\nfirst\n"}))
    asyncio.run(
        edit_note(
            acting_state,
            "t",
            {"path": "a.md", "new_content": "# A\n\nfirst\nsecond\n", "base_revision": made["revision"]},
        )
    )

    result = asyncio.run(note_changes(acting_state, "t", {"path": "a.md"}))

    assert result["changed"] is True
    assert "second" in result["diff"]


def test_restore_note_to_errors_before_history(acting_state):
    asyncio.run(create_note(acting_state, "t", {"path": "a.md", "content": "# A\n"}))
    with pytest.raises(NoteError):
        asyncio.run(restore_note_to(acting_state, "t", {"path": "a.md", "date": "1990-01-01"}))


def test_restore_note_to_restores_latest_at_or_before(acting_state):
    made = asyncio.run(create_note(acting_state, "t", {"path": "a.md", "content": "# A\n\norig\n"}))
    asyncio.run(
        edit_note(
            acting_state,
            "t",
            {"path": "a.md", "new_content": "# A\n\nchanged\n", "base_revision": made["revision"]},
        )
    )

    result = asyncio.run(restore_note_to(acting_state, "t", {"path": "a.md", "date": "2099-01-01"}))

    assert result["path"] == "a.md"
    assert result.get("revision")
