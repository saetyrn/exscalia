"""OMNIA Round 3 — in-note structure, tasks-from-prose, and study stats."""
import asyncio

from app.flashcards import FlashcardStore, new_card
from app.index.db import content_hash
from app.overseer.tool_expansion import (
    embed_attachment,
    extract_action_items,
    format_table,
    insert_toc,
    sort_list,
    study_stats,
)
from app.vault.notes import NoteStore


def add_note(state, path: str, content: str) -> None:
    created = state.store.create(path, content)
    asyncio.run(state.indexer.index_note(path, created))


def rev(state, path: str) -> str:
    return content_hash(state.store.read(path))


FM = '---\ntitle: "N"\n---\n\n# N\n\n'


# --- format_table ----------------------------------------------------------


def test_format_table_add_row(acting_state):
    add_note(acting_state, "t.md", FM + "| A | B |\n| --- | --- |\n| 1 | 2 |\n")
    asyncio.run(
        format_table(
            acting_state, "t", {"path": "t.md", "operation": "add_row", "row": ["3", "4"], "base_revision": rev(acting_state, "t.md")}
        )
    )
    body = acting_state.store.read("t.md")
    assert "3" in body and "4" in body


def test_format_table_sort(acting_state):
    add_note(acting_state, "t.md", FM + "| Name |\n| --- |\n| Charlie |\n| Alice |\n| Bob |\n")
    asyncio.run(
        format_table(acting_state, "t", {"path": "t.md", "operation": "sort", "column": 0, "base_revision": rev(acting_state, "t.md")})
    )
    body = acting_state.store.read("t.md")
    assert body.index("Alice") < body.index("Bob") < body.index("Charlie")


# --- sort_list -------------------------------------------------------------


def test_sort_list_with_dedupe(acting_state):
    add_note(acting_state, "l.md", FM + "- charlie\n- alice\n- bob\n- alice\n")
    asyncio.run(sort_list(acting_state, "t", {"path": "l.md", "dedupe": True, "base_revision": rev(acting_state, "l.md")}))
    body = acting_state.store.read("l.md")
    assert body.index("alice") < body.index("bob") < body.index("charlie")
    assert body.count("- alice") == 1


# --- insert_toc ------------------------------------------------------------


def test_insert_toc(acting_state):
    add_note(acting_state, "n.md", FM + "## First\n\ntext\n\n## Second\n\nmore\n")
    asyncio.run(insert_toc(acting_state, "t", {"path": "n.md", "base_revision": rev(acting_state, "n.md")}))
    body = acting_state.store.read("n.md")
    assert "## Contents" in body
    assert "- First" in body and "- Second" in body


# --- embed_attachment ------------------------------------------------------


def test_embed_attachment(acting_state):
    attach_dir = acting_state.settings.vault_dir / NoteStore.ATTACHMENT_DIR
    attach_dir.mkdir(parents=True, exist_ok=True)
    (attach_dir / "diagram.png").write_bytes(b"x")
    add_note(acting_state, "n.md", FM + "body\n")

    asyncio.run(embed_attachment(acting_state, "t", {"path": "n.md", "attachment": "diagram", "alt": "My Diagram"}))

    assert "![My Diagram](/api/attachments/diagram.png)" in acting_state.store.read("n.md")


# --- extract_action_items --------------------------------------------------


def test_extract_action_items(acting_state):
    add_note(acting_state, "board.md", '---\ntitle: "Board"\ntask_board: true\n---\n\n# Tasks\n')
    add_note(acting_state, "notes.md", FM + "- call bank\n- email prof\nsome prose\n")

    result = asyncio.run(
        extract_action_items(
            acting_state, "t", {"path": "notes.md", "board": "board.md", "base_revision": rev(acting_state, "notes.md")}
        )
    )

    assert result["extracted"] == 2
    rows = asyncio.run(acting_state.db.execute_fetchall("SELECT text FROM tasks WHERE path = 'board.md'"))
    assert {"call bank", "email prof"} <= {r["text"] for r in rows}
    assert "- call bank" not in acting_state.store.read("notes.md")


# --- study_stats -----------------------------------------------------------


def test_study_stats(acting_state):
    acting_state.flashcards = FlashcardStore(acting_state.settings.vault_dir)
    acting_state.flashcards.create("Bio", [new_card("Q1", "A1"), new_card("Q2", "A2")], [])

    result = asyncio.run(study_stats(acting_state, "t", {}))

    assert result["decks"] == 1
    assert result["total_cards"] == 2
    assert result["per_deck"][0]["name"] == "Bio"
