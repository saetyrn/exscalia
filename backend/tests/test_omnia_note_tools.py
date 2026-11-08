"""OMNIA Round 2 — note-editing conveniences."""
import asyncio

import pytest

from app.index.db import content_hash
from app.overseer.tool_expansion import (
    append_to_note,
    convert_bullets_to_tasks,
    create_note,
    create_task,
    duplicate_note,
    get_outline,
    move_section,
    rename_note,
    save_note_as_template,
)
from app.vault.notes import NoteError


def add_note(state, path: str, content: str) -> str:
    created = state.store.create(path, content)
    asyncio.run(state.indexer.index_note(path, created))
    return created


def rev(state, path: str) -> str:
    return content_hash(state.store.read(path))


def note(title: str, body: str) -> str:
    return f'---\ntitle: "{title}"\n---\n\n# {title}\n\n{body}\n'


# --- append_to_note --------------------------------------------------------


def test_append_to_note_end(acting_state):
    add_note(acting_state, "n.md", note("N", "first"))
    asyncio.run(
        append_to_note(
            acting_state, "t", {"path": "n.md", "text": "second", "base_revision": rev(acting_state, "n.md")}
        )
    )
    assert acting_state.store.read("n.md").rstrip().endswith("second")


def test_append_under_heading(acting_state):
    add_note(acting_state, "n.md", note("N", "## Ideas\n\nalpha\n\n## Other\n\nx"))
    asyncio.run(
        append_to_note(
            acting_state,
            "t",
            {"path": "n.md", "text": "beta", "heading": "Ideas", "base_revision": rev(acting_state, "n.md")},
        )
    )
    body = acting_state.store.read("n.md")
    assert body.index("## Ideas") < body.index("beta") < body.index("## Other")


# --- duplicate_note --------------------------------------------------------


def test_duplicate_note_mints_fresh_task_ids(acting_state):
    made = asyncio.run(create_task(acting_state, "t", {"text": "a task", "path": "tasks.md"}))["task"]

    result = asyncio.run(duplicate_note(acting_state, "t", {"path": "tasks.md"}))

    assert acting_state.store.exists(result["path"])
    assert made["id"] not in acting_state.store.read(result["path"])
    rows = asyncio.run(
        acting_state.db.execute_fetchall("SELECT id FROM tasks WHERE path = ?", (result["path"],))
    )
    assert rows and rows[0]["id"] != made["id"]


def test_duplicating_a_board_does_not_make_a_second_board(acting_state):
    from app import task_boards

    add_note(
        acting_state,
        "Projects/Tasks.md",
        '---\ntitle: "Tasks"\ntask_board: true\n---\n\n# Tasks\n',
    )

    result = asyncio.run(duplicate_note(acting_state, "t", {"path": "Projects/Tasks.md"}))

    assert result["path"].startswith("Projects/")
    rows = asyncio.run(
        acting_state.db.execute_fetchall(
            "SELECT task_board FROM notes WHERE path = ?", (result["path"],)
        )
    )
    assert rows and rows[0]["task_board"] == 0
    assert "task_board" not in acting_state.store.read(result["path"])
    assert (
        asyncio.run(task_boards.board_for_folder(acting_state.db, "Projects"))
        == "Projects/Tasks.md"
    )


# --- rename_note -----------------------------------------------------------


def test_rename_note_moves_and_rewrites_links(acting_state):
    # Source is trashed on rename (git delete), so create it through the tool
    # (which commits) rather than an untracked raw file.
    asyncio.run(create_note(acting_state, "t", {"path": "Old.md", "content": note("Old", "body")}))
    add_note(acting_state, "ref.md", note("Ref", "See [[Old]] here."))

    result = asyncio.run(
        rename_note(
            acting_state, "t", {"path": "Old.md", "new_title": "New Name", "base_revision": rev(acting_state, "Old.md")}
        )
    )

    assert result["path"] == "New Name.md"
    assert not acting_state.store.exists("Old.md")
    assert "[[New Name]]" in acting_state.store.read("ref.md")


def test_rename_note_onto_a_taken_name_points_links_at_the_renamed_note(acting_state):
    asyncio.run(create_note(acting_state, "t", {"path": "Old.md", "content": note("Old", "body")}))
    add_note(acting_state, "New Name.md", note("New Name", "someone else"))
    add_note(acting_state, "ref.md", note("Ref", "See [[Old]] and [[New Name]]."))

    result = asyncio.run(
        rename_note(
            acting_state, "t", {"path": "Old.md", "new_title": "New Name", "base_revision": rev(acting_state, "Old.md")}
        )
    )

    assert result["path"] == "New Name 2.md"
    assert "See [[New Name 2]] and [[New Name]]." in acting_state.store.read("ref.md")


def test_rename_note_refuses_exempt_roots(acting_state):
    add_note(acting_state, "templates/thing.md", "---\ntitle: \"thing\"\n---\n\n# thing\n")
    with pytest.raises(NoteError):
        asyncio.run(
            rename_note(
                acting_state,
                "t",
                {"path": "templates/thing.md", "new_title": "Nope", "base_revision": rev(acting_state, "templates/thing.md")},
            )
        )


# --- get_outline -----------------------------------------------------------


def test_get_outline_lists_headings_and_counts(acting_state):
    add_note(acting_state, "n.md", note("N", "## One\n\nsome words here\n\n## Two\n\nmore"))
    result = asyncio.run(get_outline(acting_state, "t", {"path": "n.md"}))
    texts = [h["text"] for h in result["headings"]]
    assert "One" in texts and "Two" in texts
    assert result["word_count"] > 0


# --- move_section ----------------------------------------------------------


def test_move_section_carries_subsections(acting_state):
    add_note(
        acting_state,
        "src.md",
        note("Src", "## Keep\n\nkeep me\n\n## Move\n\nmove me\n\n### sub\n\nsub content"),
    )
    add_note(acting_state, "dst.md", note("Dst", "existing"))

    asyncio.run(
        move_section(
            acting_state,
            "t",
            {"path": "src.md", "heading": "Move", "target_path": "dst.md", "base_revision": rev(acting_state, "src.md")},
        )
    )

    src = acting_state.store.read("src.md")
    dst = acting_state.store.read("dst.md")
    assert "## Move" not in src and "move me" not in src
    assert "## Keep" in src
    assert "move me" in dst and "sub content" in dst


# --- convert_bullets_to_tasks ----------------------------------------------


def test_convert_bullets_to_tasks(acting_state):
    add_note(acting_state, "list.md", note("List", "- one\n- two\n- [ ] already"))

    result = asyncio.run(
        convert_bullets_to_tasks(acting_state, "t", {"path": "list.md", "base_revision": rev(acting_state, "list.md")})
    )

    assert result["converted"] == 2
    body = acting_state.store.read("list.md")
    assert "- [ ] one" in body and "- [ ] two" in body
    rows = asyncio.run(acting_state.db.execute_fetchall("SELECT text FROM tasks WHERE path = ?", ("list.md",)))
    assert {r["text"] for r in rows} >= {"one", "two", "already"}


# --- save_note_as_template -------------------------------------------------


def test_save_note_as_template(acting_state):
    add_note(acting_state, "n.md", note("N", "reusable body"))

    result = asyncio.run(save_note_as_template(acting_state, "t", {"path": "n.md", "name": "My Template"}))

    assert result["path"] == "templates/My Template.md"
    assert "reusable body" in acting_state.store.read("templates/My Template.md")
