"""FTS5 recall over OMNIA's own conversations.

A rebuildable cache over the thread JSON in the vault, exactly as `notes_fts`
is over the markdown — anything here can be regenerated from the files, and the
boot sync does.

It writes through plain sqlite3 connections rather than the app's aiosqlite
one because every call site is synchronous (ThreadStore.append_public and
friends) and a message has to be searchable the moment it is written, not
whenever a background task happens to land. Connections are per operation, so
a write and a read on another thread never share one.

Writes run on one dedicated thread, never on the caller's. The call sites sit
on the event loop, and the aiosqlite connection's transactions on this same
file only commit once the loop gets back to them — so a busy wait on the loop
waited on itself: every request froze for the full busy timeout, then the row
was dropped. The loop now waits a moment at most; see _write.
"""
import asyncio
import logging
import re
import sqlite3
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

from .db import THREADS_FTS_DDL

log = logging.getLogger(__name__)

# Long enough to ride out a concurrent write from the aiosqlite connection.
# Writes spend it on the writer thread, never on the event loop.
BUSY_TIMEOUT = 5.0

# How long a write issued on the event loop holds the loop for itself.
# Uncontended it lands in about a millisecond, well inside this.
LOOP_WAIT = 0.1

# One thread, so writes land in the order they were issued even when a lock
# holds some of them up: a late upsert must not resurrect a message that a
# later truncate removed.
_writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="thread-index")


def _on_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _report(future: Future) -> None:
    error = future.exception()
    if error is not None:
        log.warning("conversation index write failed", exc_info=error)


def fts_query(query: str) -> str:
    """Free text as a valid FTS5 MATCH expression.

    Quoting each word neutralizes the query syntax a typed sentence routinely
    contains (`-`, `*`, `:`, `NEAR`, an unbalanced quote), and OR keeps the
    recall: FTS5 neither stems nor drops stopwords, so "what did we decide
    about pricing" AND-ed together matches nothing at all. bm25 still floats
    the messages carrying the most, and the rarest, of those words.
    """
    words = re.findall(r"[\w']+", query)
    return " OR ".join(f'"{word}"' for word in words)


class ThreadIndex:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)

    @contextmanager
    def _connect(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.db_path, timeout=BUSY_TIMEOUT)
        try:
            db.row_factory = sqlite3.Row
            # open_db owns the schema, but this store is wired before it at
            # boot; a thread write must not fail on a table that is one
            # statement away from existing.
            db.execute(THREADS_FTS_DDL)
            yield db
            db.commit()
        finally:
            db.close()

    # -- write path ----------------------------------------------------------

    def _write(self, statements) -> None:
        """Run `statements(db)` in one transaction on the writer thread.

        Off the loop the caller simply waits for it, as before. On the loop it
        waits LOOP_WAIT at most: uncontended the write has landed by then, so
        a message is still searchable the moment it is written; behind an
        open aiosqlite transaction it carries on alone and lands once that
        transaction commits, which it can now that the loop is free.
        """

        def run() -> None:
            with self._connect() as db:
                statements(db)

        future = _writer.submit(run)
        try:
            future.result(timeout=LOOP_WAIT if _on_event_loop() else None)
        except TimeoutError:
            future.add_done_callback(_report)

    @staticmethod
    def _insert(db: sqlite3.Connection, thread_id: str, message: dict) -> bool:
        text = str(message.get("text") or "")
        if not text.strip():
            return False
        db.execute(
            "INSERT INTO threads_fts (thread_id, message_id, role, created_at, text) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                thread_id,
                str(message.get("id") or ""),
                str(message.get("role") or ""),
                str(message.get("created_at") or ""),
                text,
            ),
        )
        return True

    def index_message(self, thread_id: str, message: dict) -> None:
        """Upsert one message. Empty text leaves no row — a streaming reply is
        appended blank and only becomes searchable once it says something."""
        # A snapshot: the write may land after this returns, by which time the
        # caller is free to have changed its dict.
        message = dict(message)

        def statements(db: sqlite3.Connection) -> None:
            db.execute(
                "DELETE FROM threads_fts WHERE thread_id = ? AND message_id = ?",
                (thread_id, str(message.get("id") or "")),
            )
            self._insert(db, thread_id, message)

        self._write(statements)

    def index_thread(self, thread: dict) -> None:
        """Replace a whole thread's rows — for edits that move more than one
        message, such as truncating a conversation to regenerate an answer."""
        thread_id = str(thread.get("id") or "")
        if not thread_id:
            return
        messages = [dict(message) for message in thread.get("messages") or []]

        def statements(db: sqlite3.Connection) -> None:
            db.execute("DELETE FROM threads_fts WHERE thread_id = ?", (thread_id,))
            for message in messages:
                self._insert(db, thread_id, message)

        self._write(statements)

    def drop_thread(self, thread_id: str) -> None:
        self._write(
            lambda db: db.execute(
                "DELETE FROM threads_fts WHERE thread_id = ?", (thread_id,)
            )
        )

    def rebuild(self, threads: list[dict]) -> int:
        """Full reindex from thread dicts (with their messages). Returns rows."""
        with self._connect() as db:
            db.execute("DELETE FROM threads_fts")
            rows = 0
            for thread in threads:
                thread_id = str(thread.get("id") or "")
                if not thread_id:
                    continue
                for message in thread.get("messages") or []:
                    rows += self._insert(db, thread_id, message)
            return rows

    def sync(self, threads: list[dict]) -> int:
        """Reconcile the cache with the thread files at boot.

        Rows for threads that no longer exist are purged (housekeeping deletes
        conversations nightly), and an empty table beside existing transcripts
        is rebuilt from them — a fresh database, or an upgrade from before this
        table existed. Returns the number of rows written.
        """
        try:
            with self._connect() as db:
                live = {str(t.get("id")) for t in threads if t.get("id")}
                stored = {
                    row["thread_id"]
                    for row in db.execute("SELECT DISTINCT thread_id FROM threads_fts")
                }
                for orphan in stored - live:
                    db.execute("DELETE FROM threads_fts WHERE thread_id = ?", (orphan,))
                empty = not (stored & live)
            return self.rebuild(threads) if empty and threads else 0
        except sqlite3.Error:
            log.warning("conversation index sync failed", exc_info=True)
            return 0

    # -- search --------------------------------------------------------------

    def search(self, query: str, limit: int) -> list[dict]:
        """Matching messages, best first. Never raises on a malformed query."""
        match = fts_query(query)
        if not match:
            return []
        sql = (
            "SELECT thread_id, message_id, role, created_at, rank, "
            "snippet(threads_fts, 4, '', '', ' … ', 24) AS excerpt "
            "FROM threads_fts WHERE threads_fts MATCH ? ORDER BY rank LIMIT ?"
        )
        with self._connect() as db:
            try:
                rows = db.execute(sql, (match, limit)).fetchall()
            except sqlite3.OperationalError:
                # fts_query should have made this impossible; the whole string
                # as one quoted phrase is the last resort that always parses.
                phrase = '"' + query.replace('"', '""') + '"'
                try:
                    rows = db.execute(sql, (phrase, limit)).fetchall()
                except sqlite3.OperationalError:
                    log.warning("conversation search rejected %r", query)
                    return []
            return [dict(row) for row in rows]
