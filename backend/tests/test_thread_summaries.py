"""A conversation's summary stands in for its preview, but only while it still
describes the conversation."""
from pathlib import Path

from app.overseer.threads import ThreadStore


def store(tmp_path: Path) -> ThreadStore:
    return ThreadStore(tmp_path / "vault", tmp_path / "data")


def summary_of(threads: ThreadStore, thread_id: str) -> str:
    return next(t for t in threads.list() if t["id"] == thread_id)["preview"]


def test_the_summary_replaces_the_preview_without_reordering(tmp_path):
    threads = store(tmp_path)
    thread = threads.create(title="Reading")
    threads.append_public(thread["id"], {"role": "user", "text": "what should I read next"})
    threads.append_public(thread["id"], {"role": "assistant", "text": "Three options, all short."})

    before = threads.get(thread["id"])["updated_at"]
    assert summary_of(threads, thread["id"]) == "Three options, all short."

    threads.set_summary(thread["id"], "Picked the next book to read")

    assert summary_of(threads, thread["id"]) == "Picked the next book to read"
    # Describing a conversation must not count as touching it: `updated_at`
    # drives the sidebar order, and restamping it would also make the summary
    # instantly stale by its own freshness rule.
    assert threads.get(thread["id"])["updated_at"] == before


def test_the_summary_steps_aside_once_the_conversation_moves_on(tmp_path):
    threads = store(tmp_path)
    thread = threads.create(title="Reading")
    threads.append_public(thread["id"], {"role": "assistant", "text": "Three options, all short."})
    threads.set_summary(thread["id"], "Picked the next book to read")
    assert summary_of(threads, thread["id"]) == "Picked the next book to read"

    threads.append_public(thread["id"], {"role": "user", "text": "actually make it a long one"})

    # Stale summaries stay on disk and self-ignore; tonight's pass recomputes.
    assert summary_of(threads, thread["id"]) == "actually make it a long one"
    assert threads.get(thread["id"])["summary"]["text"] == "Picked the next book to read"


def test_an_empty_conversation_still_reads_as_empty(tmp_path):
    threads = store(tmp_path)
    thread = threads.create(title="New chat")
    assert summary_of(threads, thread["id"]) == ""
