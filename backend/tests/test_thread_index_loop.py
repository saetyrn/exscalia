"""Conversation-index writes and the event loop.

ThreadIndex writes through plain sqlite3 from ThreadStore's synchronous call
sites, which run on the event loop, into the same metadata.db the app's
aiosqlite connection holds transactions on. A busy wait on the loop cannot
outlast a transaction that only commits once the loop is free again: every
request froze for the full five-second busy timeout, then the row was dropped.
Streaming flushes, four a second, made that collision likely; they no longer
index at all, and the finished reply indexes once.
"""
import asyncio
import sqlite3
import time
from contextlib import closing

import pytest

from app.index.db import open_db
from app.index.thread_index import ThreadIndex
from app.overseer.threads import ThreadStore


def wire(tmp_path) -> tuple[ThreadStore, ThreadIndex]:
    index = ThreadIndex(tmp_path / "index" / "metadata.db")
    return ThreadStore(tmp_path / "vault", tmp_path / "data", index=index), index


def rows_for(tmp_path, message_id: str) -> int:
    with closing(sqlite3.connect(tmp_path / "index" / "metadata.db")) as db:
        return db.execute(
            "SELECT count(*) FROM threads_fts WHERE message_id = ?", (message_id,)
        ).fetchone()[0]


def found(index: ThreadIndex, query: str) -> list[str]:
    return [row["message_id"] for row in index.search(query, 10)]


def streaming_reply(store: ThreadStore) -> tuple[str, dict]:
    """A turn as api/overseer.py opens it: the question, then a blank reply."""
    thread_id = store.create(title="Drafts")["id"]
    store.append_public(thread_id, {"role": "user", "text": "what lives in the gum trees"})
    reply = store.append_public(
        thread_id, {"role": "assistant", "text": "", "status": "streaming"}
    )
    return thread_id, reply


@pytest.mark.parametrize("final", ["complete", "error", "interrupted"])
def test_a_streaming_reply_indexes_once_when_it_finishes(tmp_path, final):
    store, index = wire(tmp_path)
    thread_id, reply = streaming_reply(store)

    store.update_message(thread_id, reply["id"], text="half a quokka", status="streaming")
    store.update_message(thread_id, reply["id"], text="half a quokka and", status="streaming")

    assert rows_for(tmp_path, reply["id"]) == 0
    assert found(index, "quokka") == []

    store.update_message(thread_id, reply["id"], text="a whole quokka", status=final)

    assert rows_for(tmp_path, reply["id"]) == 1
    assert found(index, "quokka") == [reply["id"]]


def test_a_reply_cut_off_by_a_restart_becomes_searchable(tmp_path):
    """With streaming flushes unindexed, the final update is the only one that
    indexes — and a restart mid-reply never sends it. The boot repair must."""
    store, index = wire(tmp_path)
    thread_id, reply = streaming_reply(store)
    store.update_message(thread_id, reply["id"], text="half a numbat", status="streaming")
    assert found(index, "numbat") == []

    rebooted, _ = wire(tmp_path)
    assert rebooted.reconcile_incomplete() == 1

    assert rebooted.get(thread_id)["messages"][-1]["status"] == "interrupted"
    assert found(index, "numbat") == [reply["id"]]


async def holding_a_write(tmp_path):
    """The app's aiosqlite connection, on the same file as main.py wires it,
    mid-transaction the way Indexer.index_note holds one across its awaits."""
    db = await open_db(tmp_path / "index")
    await db.execute("BEGIN IMMEDIATE")
    await db.execute("INSERT INTO notes (path, title) VALUES ('a.md', 'A')")
    return db


async def until_found(index: ThreadIndex, query: str, seconds: float = 2.0) -> list[str]:
    deadline = time.monotonic() + seconds
    while not (hits := found(index, query)) and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    return hits


def test_a_write_during_an_open_index_transaction_does_not_stall_the_loop(tmp_path):
    store, index = wire(tmp_path)
    thread_id, reply = streaming_reply(store)

    async def scenario():
        db = await holding_a_write(tmp_path)
        try:
            started = time.monotonic()
            store.update_message(
                thread_id, reply["id"], text="a finished quokka", status="complete"
            )
            stalled = time.monotonic() - started
            await db.commit()
            return stalled, await until_found(index, "quokka")
        finally:
            await db.close()

    stalled, hits = asyncio.run(scenario())

    assert stalled < 1.0, f"the event loop was blocked for {stalled:.2f}s"
    # Held up, not dropped: the write lands once the transaction commits.
    assert hits == [reply["id"]]


def test_writes_held_up_by_a_lock_still_land_in_order(tmp_path):
    """A reply finished and then regenerated while the index is busy: the
    truncate must win, not a late replay of the older upsert."""
    store, index = wire(tmp_path)
    thread_id, reply = streaming_reply(store)

    async def scenario():
        db = await holding_a_write(tmp_path)
        try:
            store.update_message(
                thread_id, reply["id"], text="a doomed numbat", status="complete"
            )
            store.truncate_from(thread_id, reply["id"])
            store.append_public(thread_id, {"role": "assistant", "text": "a quokka instead"})
            await db.commit()
            return await until_found(index, "quokka")
        finally:
            await db.close()

    assert asyncio.run(scenario())
    assert found(index, "numbat") == []
    assert rows_for(tmp_path, reply["id"]) == 0
