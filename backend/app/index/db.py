"""SQLite metadata index: notes, backlinks, tags, tasks, and FTS5 keyword search.

This is a rebuildable cache over the vault — the git repo stays the source of
truth. Anything here can be regenerated from the markdown files.
"""
import hashlib
from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    path     TEXT PRIMARY KEY,
    title    TEXT,
    created  TEXT,
    modified TEXT,
    mtime    REAL,
    hash     TEXT,
    pinned   INTEGER NOT NULL DEFAULT 0,
    task_board INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS links (
    source       TEXT NOT NULL,
    target       TEXT NOT NULL,
    target_lower TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_links_source ON links(source);
CREATE INDEX IF NOT EXISTS idx_links_target ON links(target_lower);
CREATE TABLE IF NOT EXISTS tags (
    path TEXT NOT NULL,
    tag  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tags_path ON tags(path);
CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags(tag);
CREATE TABLE IF NOT EXISTS tasks (
    id           TEXT PRIMARY KEY,
    path         TEXT NOT NULL,
    line         INTEGER NOT NULL,
    meta_line    INTEGER,
    text         TEXT NOT NULL,
    status       TEXT NOT NULL,
    created_at   TEXT,
    started_at   TEXT,
    completed_at TEXT,
    reopened_at  TEXT,
    due          TEXT,
    due_visible  INTEGER NOT NULL DEFAULT 0,
    description  TEXT,
    board        INTEGER NOT NULL DEFAULT 0,
    sort         REAL,
    reminder_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(due);
-- idx_tasks_board is created after the migrations below: on a database that
-- predates the column, CREATE TABLE IF NOT EXISTS is a no-op and indexing a
-- column that does not exist yet would fail before the ALTER ever runs.
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(path UNINDEXED, title, body);
-- The habit/preference store: OMNIA's learning layer. "Curious then settles":
-- ask once, record, reinforce on matches, decay on contradiction.
CREATE TABLE IF NOT EXISTS habits (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL,
    confidence      REAL NOT NULL DEFAULT 0.85,
    source          TEXT,
    last_seen       TEXT,
    times_confirmed INTEGER NOT NULL DEFAULT 0
);
-- Tiny app-level key/value state (e.g. last morning-greeting date).
CREATE TABLE IF NOT EXISTS app_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS pending_changes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    op          TEXT NOT NULL,      -- create | edit | delete | move
    path        TEXT NOT NULL,
    new_path    TEXT,
    base_hash   TEXT,               -- working-tree content hash when proposed (NULL = file absent)
    new_content TEXT,
    rationale   TEXT,
    created_at  TEXT NOT NULL
);
-- Media tracker: rebuilt from `media:` note frontmatter and ## Log sections.
-- Fresh tables, so their indexes may live here (the ALTER-then-INDEX dance
-- above is only for columns added to pre-existing tables).
CREATE TABLE IF NOT EXISTS media_items (
    path       TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    alias      TEXT,
    media_type TEXT NOT NULL,
    status     TEXT NOT NULL,
    creator    TEXT,
    year       INTEGER,
    cover      TEXT,
    rating     INTEGER,
    started    TEXT,
    finished   TEXT,
    total      INTEGER,
    modified   TEXT
);
CREATE INDEX IF NOT EXISTS idx_media_items_type ON media_items(media_type);
CREATE INDEX IF NOT EXISTS idx_media_items_status ON media_items(status);
CREATE TABLE IF NOT EXISTS media_log (
    path  TEXT NOT NULL,
    line  INTEGER NOT NULL,
    at    TEXT NOT NULL,
    kind  TEXT NOT NULL,
    value TEXT,
    text  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_media_log_path ON media_log(path);
CREATE INDEX IF NOT EXISTS idx_media_log_at ON media_log(at);
"""


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


async def open_db(index_dir: Path) -> aiosqlite.Connection:
    index_dir.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(index_dir / "metadata.db")
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.executescript(SCHEMA)
    # Migrations for databases created before a column existed.
    cur = await db.execute("PRAGMA table_info(notes)")
    cols = {row[1] for row in await cur.fetchall()}
    if "pinned" not in cols:
        await db.execute("ALTER TABLE notes ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
    if "task_board" not in cols:
        await db.execute(
            "ALTER TABLE notes ADD COLUMN task_board INTEGER NOT NULL DEFAULT 0"
        )

    # ``tasks`` is a rebuildable cache. Recreate the pre-lifecycle schema
    # instead of attempting a brittle multi-column in-place migration.
    task_cur = await db.execute("PRAGMA table_info(tasks)")
    task_cols = {row[1] for row in await task_cur.fetchall()}
    if "id" not in task_cols or "status" not in task_cols:
        await db.executescript(
            """
            DROP TABLE IF EXISTS tasks;
            CREATE TABLE tasks (
                id           TEXT PRIMARY KEY,
                path         TEXT NOT NULL,
                line         INTEGER NOT NULL,
                meta_line    INTEGER,
                text         TEXT NOT NULL,
                status       TEXT NOT NULL,
                created_at   TEXT,
                started_at   TEXT,
                completed_at TEXT,
                reopened_at  TEXT,
                due          TEXT,
                due_visible  INTEGER NOT NULL DEFAULT 0,
                description  TEXT,
                board        INTEGER NOT NULL DEFAULT 0,
                sort         REAL,
                reminder_at  TEXT
            );
            CREATE INDEX idx_tasks_due ON tasks(due);
            CREATE INDEX idx_tasks_path ON tasks(path);
            CREATE INDEX idx_tasks_status ON tasks(status);
            CREATE INDEX idx_tasks_board ON tasks(board);
            """
        )
    else:
        if "description" not in task_cols:
            await db.execute("ALTER TABLE tasks ADD COLUMN description TEXT")
        if "reopened_at" not in task_cols:
            await db.execute("ALTER TABLE tasks ADD COLUMN reopened_at TEXT")
        if "board" not in task_cols:
            await db.execute(
                "ALTER TABLE tasks ADD COLUMN board INTEGER NOT NULL DEFAULT 0"
            )
        if "sort" not in task_cols:
            await db.execute("ALTER TABLE tasks ADD COLUMN sort REAL")
        if "reminder_at" not in task_cols:
            await db.execute("ALTER TABLE tasks ADD COLUMN reminder_at TEXT")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_board ON tasks(board)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_path ON tasks(path)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)")

    media_cur = await db.execute("PRAGMA table_info(media_items)")
    media_cols = {row[1] for row in await media_cur.fetchall()}
    if "alias" not in media_cols:
        await db.execute("ALTER TABLE media_items ADD COLUMN alias TEXT")

    await db.commit()
    return db
