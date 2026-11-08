"""OMNIA Round 3 — in-note structure, tasks-from-prose, and study stats."""
import asyncio

import pytest

from app.flashcards import FlashcardStore, new_card
from app.index.db import content_hash
from app.overseer import tool_expansion
from app.overseer.tool_expansion import (
    embed_attachment,
    extract_action_items,
    format_table,
    insert_toc,
    sort_list,
    study_stats,
)
from app.task_service import create_task, set_task_reminder
from app.vault.notes import NoteError, NoteStore


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


BOARD = '---\ntitle: "Board"\ntask_board: true\n---\n\n# Tasks\n'


def board_rows(state) -> dict[str, dict]:
    rows = asyncio.run(state.db.execute_fetchall("SELECT * FROM tasks WHERE path = 'board.md'"))
    return {row["text"]: dict(row) for row in rows}


def test_extract_action_items_keeps_task_identity_and_checkbox_state(acting_state):
    state = acting_state
    add_note(state, "board.md", BOARD)
    add_note(
        state,
        "notes.md",
        FM
        + "- attendees: Ana, Bo\n- [x] sent invoice\n- [ ] call bank\n"
        + "- [/] draft slides 📅 2026-10-02\n",
    )
    task = asyncio.run(create_task(state, text="renew passport", path="notes.md"))
    asyncio.run(set_task_reminder(state, task["id"], "2026-10-05T09:00"))

    result = asyncio.run(
        extract_action_items(
            state, "t", {"path": "notes.md", "board": "board.md", "base_revision": rev(state, "notes.md")}
        )
    )

    assert result["extracted"] == 3 and result["moved"] == 1
    assert result["skipped_done"] == 1 and result["skipped_bullets"] == 1
    rows = board_rows(state)
    assert set(rows) == {"call bank", "draft slides", "renew passport"}
    # Moved, not recreated: same id, reminder intact, never shown as deleted.
    assert rows["renew passport"]["id"] == task["id"]
    assert rows["renew passport"]["reminder_at"] == "2026-10-05T09:00"
    assert task["id"] not in state.task_tombstones.all()
    assert rows["call bank"]["status"] == "todo"
    assert rows["draft slides"]["status"] == "in_progress"
    assert rows["draft slides"]["due"] == "2026-10-02"
    source = state.store.read("notes.md")
    assert "- [x] sent invoice" in source and "- attendees: Ana, Bo" in source
    for text in ("call bank", "draft slides", "renew passport", task["id"]):
        assert text not in source
    lines = source.splitlines()
    comments = [i for i, line in enumerate(lines) if "exscalia-task:" in line]
    assert all(lines[i + 1].startswith("- [") for i in comments), "orphan task comment"


def test_extract_action_items_include_done_and_bullets(acting_state):
    state = acting_state
    add_note(state, "board.md", BOARD)
    add_note(state, "notes.md", FM + "- attendees: Ana\n- [x] sent invoice\n")

    result = asyncio.run(
        extract_action_items(
            state,
            "t",
            {
                "path": "notes.md",
                "board": "board.md",
                "include_done": True,
                "include_bullets": True,
                "base_revision": rev(state, "notes.md"),
            },
        )
    )

    assert result["extracted"] == 2
    assert {text: row["status"] for text, row in board_rows(state).items()} == {
        "attendees: Ana": "todo",
        "sent invoice": "done",
    }
    source = state.store.read("notes.md")
    assert "sent invoice" not in source and "attendees" not in source


def test_extract_action_items_refuses_its_own_note_as_board(acting_state):
    state = acting_state
    add_note(state, "board.md", BOARD + "\n- call bank\n- [ ] email prof\n")
    before = state.store.read("board.md")

    with pytest.raises(NoteError) as err:
        asyncio.run(
            extract_action_items(
                state, "t", {"path": "board.md", "board": "board.md", "base_revision": rev(state, "board.md")}
            )
        )

    assert err.value.status == 400 and "convert_bullets_to_tasks" in err.value.detail
    assert state.store.read("board.md") == before


def test_extract_action_items_keeps_an_edit_made_while_it_works(acting_state, monkeypatch):
    state = acting_state
    add_note(state, "board.md", BOARD)
    add_note(state, "notes.md", FM + "- [ ] call bank\n")
    real = tool_expansion.create_task_record

    async def create_during_autosave(st, **kwargs):
        task = await real(st, **kwargs)
        # The editor's autosave lands while OMNIA awaits the board write.
        state.store.write("notes.md", state.store.read("notes.md") + "typed meanwhile\n")
        return task

    monkeypatch.setattr(tool_expansion, "create_task_record", create_during_autosave)
    result = asyncio.run(
        extract_action_items(
            state, "t", {"path": "notes.md", "board": "board.md", "base_revision": rev(state, "notes.md")}
        )
    )

    assert "changed while OMNIA was working" in result["error"]
    assert "(partial)" in result["_action"]["summary"]
    source = state.store.read("notes.md")
    assert "typed meanwhile" in source and "- [ ] call bank" in source
    assert set(board_rows(state)) == {"call bank"}


def test_extract_action_items_refuses_unloadable_frontmatter_before_the_board(acting_state):
    state = acting_state
    add_note(state, "board.md", BOARD)
    # Broken YAML: the editor refuses to save it, so its checkboxes stay bare.
    source = "---\ntitle: {{title}}\n---\n\n- [ ] alpha\n- [ ] beta\n"
    state.store.write_raw("notes.md", source)
    asyncio.run(state.indexer.index_note("notes.md", source))

    for _ in range(2):  # the model retries after an error
        with pytest.raises(NoteError) as err:
            asyncio.run(
                extract_action_items(
                    state, "t", {"path": "notes.md", "board": "board.md", "base_revision": rev(state, "notes.md")}
                )
            )
        assert err.value.status == 422 and "remove=false" in err.value.detail

    assert board_rows(state) == {}
    assert state.store.read("notes.md") == source

    result = asyncio.run(
        extract_action_items(
            state,
            "t",
            {"path": "notes.md", "board": "board.md", "remove": False, "base_revision": rev(state, "notes.md")},
        )
    )

    assert result["extracted"] == 2
    assert set(board_rows(state)) == {"alpha", "beta"}
    assert state.store.read("notes.md") == source


# --- study_stats -----------------------------------------------------------


def test_study_stats(acting_state):
    acting_state.flashcards = FlashcardStore(acting_state.settings.vault_dir)
    acting_state.flashcards.create("Bio", [new_card("Q1", "A1"), new_card("Q2", "A2")], [])

    result = asyncio.run(study_stats(acting_state, "t", {}))

    assert result["decks"] == 1
    assert result["total_cards"] == 2
    assert result["per_deck"][0]["name"] == "Bio"
