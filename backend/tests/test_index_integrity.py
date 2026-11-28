"""The metadata index on the app's one shared aiosqlite connection.

Every await in a writer yields to other requests, and they all share one
connection and its one implicit transaction. Two saves of the same note used
to interleave inside it: the second died on UNIQUE notes.path after its file
write had succeeded, and notes_fts kept the older body. A failure between the
DELETEs and the commit left the half-applied rows open for the next writer's
commit to persist. And nothing recorded which derivation built the rows, so a
changed parse rule never reached a note nobody edited.
"""
import asyncio
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import overseer as overseer_api
from app.index import db as index_db
from app.index import parse
from app.index.db import INDEX_VERSION, content_hash
from app.index.indexer import Indexer
from app.index.paper_index import PaperIndex
from app.index.thread_index import ThreadIndex
from app.overseer.threads import ThreadStore
from app.overseer.tool_expansion import rebuild_search_index

from conftest import make_client, test_settings


def run(coro):
    return asyncio.run(coro)


def rows(state, sql: str, params: tuple = ()) -> list[tuple]:
    return [tuple(r) for r in run(state.db.execute_fetchall(sql, params))]


def note(body: str, tag: str = "", task: str = "", link: str = "") -> str:
    lines = [body]
    if tag:
        lines.append(f"#{tag}")
    if link:
        lines.append(f"[[{link}]]")
    if task:
        lines.append(f"- [ ] {task}")
    return "\n".join(lines) + "\n"


class Watched:
    """The Indexer's connection, with a look at the committed index before
    every statement it sends — what any other reader of the database file
    (a second connection, a restart) would see at that instant."""

    def __init__(self, db, look):
        self._db = db
        self._look = look
        self.statements = 0

    def __getattr__(self, name):
        attr = getattr(self._db, name)
        if name not in {"execute", "executemany", "execute_fetchall", "commit"}:
            return attr

        def call(*args, **kwargs):
            if name != "commit":
                self.statements += 1
            self._look()
            return attr(*args, **kwargs)

        return call


# --- the write lock ----------------------------------------------------------


def test_two_saves_of_one_note_at_once_both_land_and_the_later_wins(state):
    """The reproduction from the audit: gather two index_note calls."""
    older = note("version B", tag="bee", task="task B", link="Bee")
    newer = note("version C", tag="sea", task="task C", link="Sea")
    state.store.write_raw("X.md", newer)

    async def both():
        return await asyncio.gather(
            state.indexer.index_note("X.md", older, 2.0),
            state.indexer.index_note("X.md", newer, 3.0),
            return_exceptions=True,
        )

    assert run(both()) == [None, None]
    assert rows(state, "SELECT mtime, hash FROM notes WHERE path = 'X.md'") == [
        (3.0, content_hash(newer))
    ]
    fts = rows(state, "SELECT body FROM notes_fts WHERE path = 'X.md'")
    assert len(fts) == 1 and "version C" in fts[0][0]
    assert rows(state, "SELECT tag FROM tags WHERE path = 'X.md'") == [("sea",)]
    assert rows(state, "SELECT target FROM links WHERE source = 'X.md'") == [("Sea",)]
    assert rows(state, "SELECT text FROM tasks WHERE path = 'X.md'") == [("task C",)]


def test_many_writers_on_the_shared_lock_never_share_a_transaction(state):
    """Notes, Paper and the review tray all write the one connection; boot
    hands them one lock, and the fixture wires them the same way."""
    assert state.pending.write_lock is state.indexer.write_lock
    assert state.paper_index.write_lock is state.indexer.write_lock
    contents = [note(f"revision {n}", tag=f"t{n}", task=f"task {n}") for n in range(8)]

    async def burst():
        return await asyncio.gather(
            *(
                state.indexer.index_note("Busy.md", content, float(n))
                for n, content in enumerate(contents)
            ),
            state.pending.stage("s", "create", "Staged.md", new_content="x"),
            return_exceptions=True,
        )

    results = run(burst())
    assert not [r for r in results if isinstance(r, BaseException)]
    assert rows(state, "SELECT mtime FROM notes WHERE path = 'Busy.md'") == [(7.0,)]
    assert rows(state, "SELECT tag FROM tags WHERE path = 'Busy.md'") == [("t7",)]
    assert len(rows(state, "SELECT id FROM pending_changes")) == 1
    assert not state.db.in_transaction


def test_a_failed_write_is_rolled_back_not_left_for_the_next_commit(state):
    """A statement that fails after the DELETEs must not strand them: the
    next unrelated commit used to persist the note's disappearance."""
    original = note("the original", tag="kept", task="kept task", link="Kept")
    state.store.write_raw("X.md", original)
    run(state.indexer.index_note("X.md", original, 1.0))
    run(
        state.db.execute(
            "CREATE TEMP TRIGGER fail_tags BEFORE INSERT ON tags"
            " BEGIN SELECT RAISE(ABORT, 'disk said no'); END"
        )
    )

    with pytest.raises(sqlite3.IntegrityError, match="disk said no"):
        run(state.indexer.index_note("X.md", note("rewritten", tag="gone"), 2.0))
    assert not state.db.in_transaction

    run(state.db.execute("DROP TRIGGER fail_tags"))
    run(state.indexer.index_note("Other.md", note("unrelated"), 1.0))

    assert rows(state, "SELECT mtime, hash FROM notes WHERE path = 'X.md'") == [
        (1.0, content_hash(original))
    ]
    assert rows(state, "SELECT tag FROM tags WHERE path = 'X.md'") == [("kept",)]
    assert rows(state, "SELECT text FROM tasks WHERE path = 'X.md'") == [("kept task",)]
    assert rows(state, "SELECT target FROM links WHERE source = 'X.md'") == [("Kept",)]
    assert len(rows(state, "SELECT path FROM notes_fts WHERE path = 'X.md'")) == 1


def test_a_note_that_fails_to_parse_keeps_its_previous_rows(state, monkeypatch):
    original = note("the original", tag="kept", task="kept task")
    run(state.indexer.index_note("X.md", original, 1.0))

    def broken(meta, body):
        raise TypeError("'int' object is not iterable")

    monkeypatch.setattr(parse, "extract_tags", broken)
    with pytest.raises(TypeError):
        run(state.indexer.index_note("X.md", note("tags: 2024"), 2.0))
    monkeypatch.undo()
    run(state.indexer.index_note("Other.md", note("unrelated"), 1.0))

    assert rows(state, "SELECT mtime FROM notes WHERE path = 'X.md'") == [(1.0,)]
    assert rows(state, "SELECT tag FROM tags WHERE path = 'X.md'") == [("kept",)]
    assert rows(state, "SELECT text FROM tasks WHERE path = 'X.md'") == [("kept task",)]


def test_rename_is_one_transaction(state, tmp_path: Path):
    content = note("moving", tag="m", task="carried task")
    state.store.write_raw("Old.md", content)
    run(state.indexer.index_note("Old.md", content))
    state.store.rename("Old.md", "New.md")

    seen: list[set[str]] = []

    def look():
        with sqlite3.connect(tmp_path / "index" / "metadata.db") as other:
            seen.append(
                {
                    r[0]
                    for r in other.execute(
                        "SELECT path FROM notes WHERE path IN ('Old.md', 'New.md')"
                    )
                }
            )

    real = state.indexer.db
    state.indexer.db = Watched(real, look)
    try:
        run(state.indexer.rename_note("Old.md", "New.md"))
    finally:
        state.indexer.db = real
    look()

    assert seen and all(seen), seen  # never neither
    assert seen[-1] == {"New.md"}
    assert rows(
        state, "SELECT path, text FROM tasks WHERE path IN ('Old.md', 'New.md')"
    ) == [("New.md", "carried task")]


def test_a_300_task_note_is_written_in_a_handful_of_statements(state):
    body = "\n".join(f"- [ ] task {n} #t{n} [[L{n}]]" for n in range(300))
    state.store.write_raw("Big.md", body + "\n")
    watched = Watched(state.db, lambda: None)
    state.indexer.db = watched
    try:
        run(state.indexer.index_note("Big.md", body + "\n"))
    finally:
        state.indexer.db = state.db

    assert rows(state, "SELECT COUNT(*) FROM tasks WHERE path = 'Big.md'") == [(300,)]
    assert rows(state, "SELECT COUNT(*) FROM links WHERE source = 'Big.md'") == [(300,)]
    assert rows(state, "SELECT COUNT(*) FROM tags WHERE path = 'Big.md'") == [(300,)]
    # One per table, not one per row: the lock is held for a few awaits.
    assert watched.statements < 20, watched.statements


def test_paper_and_tray_default_to_their_own_lock(state):
    """Bare constructions (tests, tools) still get a working lock."""
    assert PaperIndex(state.db, state.paper).write_lock is not None
    pending = type(state.pending)(state.db, state.store, state.vault, state.indexer)
    assert pending.write_lock is not state.indexer.write_lock
    assert run(pending.stage("s", "create", "Solo.md", new_content="x")) > 0


# --- the index version -------------------------------------------------------


def _boot(settings) -> None:
    from app import main as main_module

    with TestClient(main_module.app) as client:
        assert client.get("/api/health").status_code == 200


def _poison(index_dir: Path, version: int | None) -> None:
    """Edit the index behind the app's back, as an older derivation would
    have left it: a task row that disagrees with the markdown."""
    with sqlite3.connect(index_dir / "metadata.db") as db:
        db.execute("UPDATE tasks SET text = 'WRONG' WHERE path = 'Chores.md'")
        db.execute("UPDATE threads_fts SET text = 'WRONG'")
        if version is not None:
            db.execute(f"PRAGMA user_version = {version}")


def _read(index_dir: Path, sql: str) -> list[tuple]:
    with sqlite3.connect(index_dir / "metadata.db") as db:
        return [tuple(r) for r in db.execute(sql)]


def test_boot_rederives_rows_built_by_another_index_version(tmp_path, monkeypatch):
    from app import main as main_module

    settings = test_settings(tmp_path)
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    forced: list[tuple[str, bool]] = []
    note_sync, paper_sync = Indexer.sync_all, PaperIndex.sync_all

    async def record_notes(self, force=False):
        forced.append(("notes", force))
        return await note_sync(self, force=force)

    async def record_paper(self, force=False):
        forced.append(("paper", force))
        return await paper_sync(self, force=force)

    monkeypatch.setattr(Indexer, "sync_all", record_notes)
    monkeypatch.setattr(PaperIndex, "sync_all", record_paper)

    settings.vault_dir.mkdir(parents=True)
    (settings.vault_dir / "Chores.md").write_text("- [ ] water the plants\n")
    threads = ThreadStore(
        settings.vault_dir,
        settings.data_dir,
        index=ThreadIndex(settings.index_dir / "metadata.db"),
    )
    thread = threads.create(title="Garden")
    threads.append_public(thread["id"], {"role": "user", "text": "ferns need shade"})

    # A fresh database has never been stamped, so the first boot derives
    # everything and records the version.
    _boot(settings)
    assert forced == [("notes", True), ("paper", True)]
    assert _read(settings.index_dir, "PRAGMA user_version") == [(INDEX_VERSION,)]
    assert _read(settings.index_dir, "SELECT text FROM tasks WHERE path = 'Chores.md'") == [
        ("water the plants",)
    ]

    # Same version: the mtime skip stands, so a stale row stays stale. This
    # is what boot did for every upgrade before the version existed.
    forced.clear()
    _poison(settings.index_dir, version=None)
    _boot(settings)
    assert forced == [("notes", False), ("paper", False)]
    assert _read(settings.index_dir, "SELECT text FROM tasks WHERE path = 'Chores.md'") == [
        ("WRONG",)
    ]

    # Any other version, older or newer, re-derives notes, Paper and threads.
    for other in (0, INDEX_VERSION + 1):
        forced.clear()
        _poison(settings.index_dir, version=other)
        _boot(settings)
        assert forced == [("notes", True), ("paper", True)]
        assert _read(
            settings.index_dir, "SELECT text FROM tasks WHERE path = 'Chores.md'"
        ) == [("water the plants",)]
        assert _read(settings.index_dir, "SELECT text FROM threads_fts") == [
            ("ferns need shade",)
        ]
        assert _read(settings.index_dir, "PRAGMA user_version") == [(INDEX_VERSION,)]


def test_index_version_reads_and_stamps_user_version(tmp_path):
    async def go():
        db = await index_db.open_db(tmp_path / "index")
        try:
            before = await index_db.index_version(db)
            await index_db.stamp_index_version(db)
            return before, await index_db.index_version(db)
        finally:
            await db.close()

    assert run(go()) == (0, INDEX_VERSION)


# --- manual rebuilds cover conversation search -------------------------------


def _with_threads(state, tmp_path: Path) -> str:
    state.thread_index = ThreadIndex(tmp_path / "index" / "metadata.db")
    state.threads = ThreadStore(
        state.settings.vault_dir, state.settings.data_dir, index=state.thread_index
    )
    thread = state.threads.create(title="Pricing")
    state.threads.append_public(thread["id"], {"role": "user", "text": "annual pricing"})
    state.threads.append_public(thread["id"], {"role": "assistant", "text": "noted"})
    with sqlite3.connect(tmp_path / "index" / "metadata.db") as db:
        db.execute("DELETE FROM threads_fts")
    return thread["id"]


def test_rebuild_route_rebuilds_conversation_search(state, tmp_path: Path):
    thread_id = _with_threads(state, tmp_path)

    async def no_vectors(documents):
        return 0

    state.semantic.rebuild = no_vectors
    response = make_client(state, overseer_api.router).post("/api/index/rebuild")

    assert response.status_code == 200
    assert response.json()["conversation_messages_indexed"] == 2
    assert [r["thread_id"] for r in state.thread_index.search("pricing", 5)] == [thread_id]


def test_rebuild_tool_rebuilds_conversation_search(acting_state, tmp_path: Path):
    thread_id = _with_threads(acting_state, tmp_path)

    result = run(rebuild_search_index(acting_state, "t", {}))

    assert result["conversation_messages_indexed"] == 2
    assert [r["thread_id"] for r in acting_state.thread_index.search("pricing", 5)] == [
        thread_id
    ]
