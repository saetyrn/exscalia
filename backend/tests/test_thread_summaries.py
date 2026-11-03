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


def test_a_restamp_that_says_nothing_new_leaves_the_summary_standing(tmp_path):
    """Freshness is the message count, not `updated_at`. Renaming a thread, or
    any other write that restamps it without adding a word, used to throw the
    summary away until the next nightly pass."""
    threads = store(tmp_path)
    thread = threads.create(title="Reading")
    threads.append_public(thread["id"], {"role": "assistant", "text": "Three options, all short."})
    threads.set_summary(thread["id"], "Picked the next book to read")

    threads.rename(thread["id"], "Books")

    assert threads.get(thread["id"])["updated_at"] != thread["updated_at"]
    assert summary_of(threads, thread["id"]) == "Picked the next book to read"


def test_a_legacy_summary_still_works_off_the_timestamp(tmp_path):
    """Summaries written before the count was recorded must not all evaporate
    on deploy."""
    from app.vault.internal import atomic_json

    threads = store(tmp_path)
    thread = threads.create(title="Reading")
    threads.append_public(thread["id"], {"role": "assistant", "text": "Three options."})
    raw = threads.get(thread["id"])
    raw["summary"] = {
        "text": "Picked the next book to read",
        "source_updated_at": raw["updated_at"],
        "created_at": raw["updated_at"],
    }
    atomic_json(threads._path(thread["id"]), raw)

    assert summary_of(threads, thread["id"]) == "Picked the next book to read"


def test_being_judged_is_not_a_change_to_the_conversation(tmp_path):
    threads = store(tmp_path)
    thread = threads.create(title="Reading")
    threads.append_public(thread["id"], {"role": "assistant", "text": "Three options."})
    before = threads.get(thread["id"])["updated_at"]

    threads.set_housekept(thread["id"], 1)

    stamped = threads.get(thread["id"])
    assert stamped["housekept"]["message_count"] == 1
    assert stamped["updated_at"] == before
    # And it rides along in the listing, which is what housekeeping reads.
    assert next(t for t in threads.list() if t["id"] == thread["id"])["housekept"]
