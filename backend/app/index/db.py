"""SQLite metadata index: notes, backlinks, tags, tasks, and FTS5 keyword search.

This is a rebuildable cache over the vault — the git repo stays the source of
truth. Anything here can be regenerated from the markdown files.
"""
import asyncio
import hashlib
import unicodedata
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

# What the rows in this database were derived by. Bump it whenever the schema
# changes or anything Indexer.index_note, PaperIndex.index_page or ThreadIndex
# derive from the files changes (a parse rule, a new frontmatter key, a new
# column filled from existing text): boot compares it with the database's
# PRAGMA user_version and re-derives every row on a mismatch in either
# direction. The mtime/hash skips in the sync_all methods would otherwise keep
# the old derivation for every note nobody edits.
# 2: inline #tags run through every combining mark, on NFC text.
INDEX_VERSION = 2

# Conversation recall: what OMNIA and the user actually said to each other,
# beside notes_fts and just as rebuildable — the thread JSON in the vault is
# the source of truth. Kept as its own constant because app/index/thread_index.py
# writes to this table through a plain sqlite3 connection (its call sites in
# ThreadStore are synchronous) and must be able to create it in any boot order.
THREADS_FTS_DDL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS threads_fts USING fts5("
    "thread_id UNINDEXED, message_id UNINDEXED, role UNINDEXED, "
    "created_at UNINDEXED, text)"
)

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
    source        TEXT NOT NULL,
    target        TEXT NOT NULL,
    target_lower  TEXT NOT NULL,
    -- The note the link opens (index/links.py), NULL while nothing matches.
    resolved_path TEXT
);
CREATE INDEX IF NOT EXISTS idx_links_source ON links(source);
CREATE INDEX IF NOT EXISTS idx_links_target ON links(target_lower);
-- idx_links_resolved is created after the migrations, like idx_tasks_board.
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
-- Paper: handwritten notebooks under `paper/`, rebuildable from the package
-- directories exactly as `notes` is from the markdown. `seq` mirrors the
-- counter PaperStore stamps into every JSON file, so the iPad's change feed is
-- an indexed query rather than a walk of the whole tree. Fresh tables, so
-- their indexes may live inline here.
CREATE TABLE IF NOT EXISTS paper_notebooks (
    path       TEXT PRIMARY KEY,
    id         TEXT,
    title      TEXT NOT NULL,
    folder     TEXT,
    template   TEXT,
    paper_size TEXT,
    cover      TEXT,
    tags       TEXT,
    favorite   INTEGER NOT NULL DEFAULT 0,
    created    TEXT,
    modified   TEXT,
    page_count INTEGER NOT NULL DEFAULT 0,
    seq        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_paper_notebooks_folder ON paper_notebooks(folder);
CREATE INDEX IF NOT EXISTS idx_paper_notebooks_seq ON paper_notebooks(seq);
CREATE TABLE IF NOT EXISTS paper_pages (
    path            TEXT PRIMARY KEY,
    notebook        TEXT NOT NULL,
    page_id         TEXT NOT NULL,
    title           TEXT,
    position        INTEGER NOT NULL DEFAULT 0,
    revision        INTEGER NOT NULL DEFAULT 0,
    render_revision INTEGER NOT NULL DEFAULT 0,
    text_revision   INTEGER NOT NULL DEFAULT 0,
    transcript      TEXT NOT NULL DEFAULT '',
    conflict_of     TEXT,
    created         TEXT,
    modified        TEXT,
    mtime           REAL NOT NULL DEFAULT 0,
    seq             INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_paper_pages_notebook ON paper_pages(notebook);
CREATE INDEX IF NOT EXISTS idx_paper_pages_seq ON paper_pages(seq);
CREATE VIRTUAL TABLE IF NOT EXISTS paper_fts USING fts5(
    path UNINDEXED, notebook UNINDEXED, title, text);
"""

SCHEMA += THREADS_FTS_DDL + ";\n"


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


async def index_version(db: aiosqlite.Connection) -> int:
    """The INDEX_VERSION the rows were last derived by (0 = never stamped)."""
    rows = await db.execute_fetchall("PRAGMA user_version")
    return int(rows[0][0]) if rows else 0


async def stamp_index_version(db: aiosqlite.Connection) -> None:
    await db.execute(f"PRAGMA user_version = {int(INDEX_VERSION)}")
    await db.commit()


@asynccontextmanager
async def write_transaction(db: aiosqlite.Connection, lock: asyncio.Lock):
    """One multi-statement write on the app's shared connection.

    Every await inside yields to other requests, and they all share this one
    connection and its one implicit transaction: without the lock two writers
    interleave (a second index of the same note hit UNIQUE notes.path), and
    without the rollback a failure between a DELETE and the commit left the
    half-applied rows for the next writer's commit to persist. Cancellation
    rolls back too. Hold the lock only around statements: never around a call
    that takes it again (Indexer.index_note) or waits on the embedding model.
    """
    async with lock:
        try:
            yield
            await db.commit()
        except BaseException:
            await db.rollback()
            raise


def fold(value: str | None) -> str | None:
    """User text in the form case-insensitive matching compares, as SQL
    `fold()`. SQLite's own lower() and LIKE fold ASCII only, so 'über' never
    found 'Über …' and an NFD title never equalled the NFC query."""
    if value is None:
        return None
    return unicodedata.normalize("NFC", str(value)).casefold()


async def open_db(index_dir: Path) -> aiosqlite.Connection:
    index_dir.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240 (once, at startup)
    db = await aiosqlite.connect(index_dir / "metadata.db")
    db.row_factory = aiosqlite.Row
    await db.create_function("fold", 1, fold, deterministic=True)
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

    link_cur = await db.execute("PRAGMA table_info(links)")
    link_cols = {row[1] for row in await link_cur.fetchall()}
    if "resolved_path" not in link_cols:
        # Starts NULL everywhere; Indexer.sync_all's closing pass fills it in.
        await db.execute("ALTER TABLE links ADD COLUMN resolved_path TEXT")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_links_resolved ON links(resolved_path)")

    media_cur = await db.execute("PRAGMA table_info(media_items)")
    media_cols = {row[1] for row in await media_cur.fetchall()}
    if "alias" not in media_cols:
        await db.execute("ALTER TABLE media_items ADD COLUMN alias TEXT")

    await db.commit()
    return db
