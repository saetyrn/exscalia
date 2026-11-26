"""Folder task boards: the frontmatter marker, and the index flags derived
from it."""
import asyncio

import pytest

from app import task_boards
from app.index.db import open_db


def test_folder_of_handles_root_and_nesting():
    assert task_boards.folder_of("Projects/2026/plan.md") == "Projects/2026"
    assert task_boards.folder_of("Projects/plan.md") == "Projects"
    assert task_boards.folder_of("plan.md") == ""


def test_only_a_literal_true_marks_a_board():
    assert task_boards.is_board({"task_board": True})
    # A stray value must leave the note visible rather than silently hiding it.
    assert not task_boards.is_board({"task_board": "yes"})
    assert not task_boards.is_board({"task_board": 1})
    assert not task_boards.is_board({})


def test_board_path_steps_past_names_already_taken():
    assert task_boards.next_board_path("Projects", set()) == "Projects/Tasks.md"
    assert (
        task_boards.next_board_path("Projects", {"Projects/Tasks.md"})
        == "Projects/Tasks 2.md"
    )
    assert (
        task_boards.next_board_path(
            "Projects", {"projects/tasks.md", "Projects/Tasks 2.md"}
        )
        == "Projects/Tasks 3.md"
    )


def board_note(title: str = "Tasks") -> str:
    return f'---\ntitle: "{title}"\ntask_board: true\n---\n\n# Tasks\n\n- [ ] first\n- [x] second\n'


def test_indexer_flags_board_notes_and_their_tasks(state):
    async def run():
        await state.indexer.index_note("Projects/Tasks.md", board_note())
        await state.indexer.index_note(
            "Projects/plain.md", "---\ntitle: plain\n---\n\n- [ ] ordinary\n"
        )
        notes = {
            row["path"]: row["task_board"]
            for row in await state.db.execute_fetchall(
                "SELECT path, task_board FROM notes"
            )
        }
        tasks = await state.db.execute_fetchall("SELECT path, board FROM tasks")
        by_path: dict[str, list[int]] = {}
        for row in tasks:
            by_path.setdefault(row["path"], []).append(row["board"])
        return notes, by_path

    notes, by_path = asyncio.run(run())
    assert notes["Projects/Tasks.md"] == 1
    assert notes["Projects/plain.md"] == 0
    assert by_path["Projects/Tasks.md"] == [1, 1]
    assert by_path["Projects/plain.md"] == [0]


def test_removing_the_marker_returns_the_note_and_its_tasks(state):
    async def run():
        await state.indexer.index_note("Projects/Tasks.md", board_note())
        # The marker is re-read on every save, so deleting the line in the
        # editor hands the tasks back to the universal board.
        await state.indexer.index_note(
            "Projects/Tasks.md",
            '---\ntitle: "Tasks"\n---\n\n# Tasks\n\n- [ ] first\n- [x] second\n',
        )
        note = await state.db.execute_fetchall(
            "SELECT task_board FROM notes WHERE path = ?", ("Projects/Tasks.md",)
        )
        boards = {
            row["board"]
            for row in await state.db.execute_fetchall(
                "SELECT board FROM tasks WHERE path = ?", ("Projects/Tasks.md",)
            )
        }
        return note[0]["task_board"], boards

    flag, boards = asyncio.run(run())
    assert flag == 0
    assert boards == {0}


def test_board_lookup_walks_up_to_the_nearest_ancestor(state):
    async def run():
        await state.indexer.index_note("Projects/Tasks.md", board_note())
        await state.indexer.index_note("Projects/2026/Tasks.md", board_note())
        return {
            "own": await task_boards.board_for_folder(state.db, "Projects"),
            # A subfolder board is its own, not its parent's.
            "sub": await task_boards.board_for_folder(state.db, "Projects/2026"),
            "none": await task_boards.board_for_folder(state.db, "Ideas"),
            "exact": await task_boards.nearest_board(state.db, "Projects/2026/plan.md"),
            # No board of its own — the closest ancestor's governs.
            "ancestor": await task_boards.nearest_board(
                state.db, "Projects/notes/deep/thing.md"
            ),
            "unboarded": await task_boards.nearest_board(state.db, "Ideas/thing.md"),
            "null": await task_boards.nearest_board(state.db, None),
        }

    r = asyncio.run(run())
    assert r["own"] == "Projects/Tasks.md"
    assert r["sub"] == "Projects/2026/Tasks.md"
    assert r["none"] is None
    assert r["exact"] == "Projects/2026/Tasks.md"
    assert r["ancestor"] == "Projects/Tasks.md"
    assert r["unboarded"] is None
    assert r["null"] is None


def test_migration_upgrades_a_database_that_predates_boards(tmp_path):
    """The columns land on a live vault's index, not just a fresh one.

    A CREATE INDEX in the schema block would run before the ALTER and take the
    server down on startup, which no fresh-database test can catch.
    """
    import sqlite3

    index_dir = tmp_path / "index"
    index_dir.mkdir()
    con = sqlite3.connect(index_dir / "metadata.db")
    con.executescript(
        """
        CREATE TABLE notes (path TEXT PRIMARY KEY, title TEXT, created TEXT,
            modified TEXT, mtime REAL, hash TEXT, pinned INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE tasks (id TEXT PRIMARY KEY, path TEXT NOT NULL, line INTEGER NOT NULL,
            meta_line INTEGER, text TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT,
            started_at TEXT, completed_at TEXT, reopened_at TEXT, due TEXT,
            due_visible INTEGER NOT NULL DEFAULT 0, description TEXT);
        INSERT INTO tasks (id, path, line, text, status)
            VALUES ('old', 'Tasks Inbox.md', 1, 'legacy', 'todo');
        """
    )
    con.commit()
    con.close()

    async def run():
        db = await open_db(index_dir)
        rows = await db.execute_fetchall("SELECT id, board FROM tasks")
        cur = await db.execute("PRAGMA table_info(notes)")
        note_cols = {row[1] for row in await cur.fetchall()}
        await db.close()
        return rows, note_cols

    rows, note_cols = asyncio.run(run())
    assert "task_board" in note_cols
    # Existing tasks belong to the universal board, which is what 0 means.
    assert [(r["id"], r["board"]) for r in rows] == [("old", 0)]


def test_migration_is_idempotent(tmp_path):
    async def run():
        db = await open_db(tmp_path / "index")
        await db.close()
        db = await open_db(tmp_path / "index")
        cur = await db.execute("PRAGMA table_info(notes)")
        note_cols = {row[1] for row in await cur.fetchall()}
        cur = await db.execute("PRAGMA table_info(tasks)")
        task_cols = {row[1] for row in await cur.fetchall()}
        await db.close()
        return note_cols, task_cols

    note_cols, task_cols = asyncio.run(run())
    assert "task_board" in note_cols
    assert "board" in task_cols
