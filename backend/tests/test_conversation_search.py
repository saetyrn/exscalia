"""Conversation recall over FTS5.

search_conversations used to re-parse every thread file and test for a literal
lowercase substring, so "what did we decide about pricing" matched nothing at
all unless the user had typed that exact phrase. The transcripts now feed a
`threads_fts` cache beside `notes_fts`, rebuildable from the files.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from app.index.thread_index import ThreadIndex, fts_query
from app.overseer.threads import ThreadStore
from app.overseer.tool_expansion import read_conversation, search_conversations
from app.vault.notes import NoteError

CONVERSATION_KEYS = {"thread_id", "title", "updated_at", "match_count", "matches"}
MATCH_KEYS = {"role", "excerpt"}


@pytest.fixture
def store(tmp_path):
    return ThreadStore(
        tmp_path / "vault",
        tmp_path / "data",
        index=ThreadIndex(tmp_path / "index" / "metadata.db"),
    )


def conversation(store, title: str, *messages: tuple[str, str]) -> str:
    thread = store.create(title=title)
    for role, text in messages:
        store.append_public(thread["id"], {"role": role, "text": text})
    return thread["id"]


def find(store, query: str, **inp) -> dict:
    state = SimpleNamespace(threads=store)
    return asyncio.run(search_conversations(state, "t", {"query": query, **inp}))


# --- the write path ----------------------------------------------------------


def test_an_appended_message_is_searchable_at_once(store):
    thread_id = conversation(store, "Billing", ("user", "let's use annual pricing"))

    found = find(store, "pricing")["conversations"]

    assert [c["thread_id"] for c in found] == [thread_id]
    assert found[0]["matches"][0]["role"] == "user"
    assert "pricing" in found[0]["matches"][0]["excerpt"]


def test_a_question_finds_the_answer_by_its_words(store):
    """The case the substring scan could never answer."""
    conversation(
        store,
        "Billing",
        ("user", "how should we charge for the pro tier?"),
        ("assistant", "Annual pricing, billed up front — we decided that in March."),
    )

    found = find(store, "what did we decide about pricing")["conversations"]

    assert len(found) == 1
    assert found[0]["match_count"] >= 1


def test_updating_a_message_replaces_what_is_findable(store):
    thread_id = store.create(title="Drafts")["id"]
    message = store.append_public(thread_id, {"role": "assistant", "text": "quokkas"})

    store.update_message(thread_id, message["id"], text="numbats", status="complete")

    assert find(store, "numbats")["conversations"]
    assert find(store, "quokkas")["conversations"] == []


def test_deleting_a_thread_purges_its_rows(store):
    thread_id = conversation(store, "Billing", ("user", "annual pricing it is"))
    assert find(store, "pricing")["conversations"]

    store.delete(thread_id)

    assert find(store, "pricing")["conversations"] == []


def test_truncating_a_thread_forgets_the_dropped_exchange(store):
    thread_id = store.create(title="Drafts")["id"]
    store.append_public(thread_id, {"role": "user", "text": "keep this quokka"})
    doomed = store.append_public(thread_id, {"role": "assistant", "text": "drop this numbat"})

    store.truncate_from(thread_id, doomed["id"])

    assert find(store, "quokka")["conversations"]
    assert find(store, "numbat")["conversations"] == []


def test_a_blank_reply_becomes_searchable_only_once_it_says_something(store):
    """Every answer is appended empty and filled in as it streams."""
    thread_id = store.create(title="Drafts")["id"]
    message = store.append_public(thread_id, {"role": "assistant", "text": ""})
    assert find(store, "anything")["conversations"] == []

    store.update_message(thread_id, message["id"], text="a quokka is a marsupial")

    assert find(store, "quokka")["conversations"]


def test_a_store_with_no_index_still_serves_chat(tmp_path):
    plain = ThreadStore(tmp_path / "vault", tmp_path / "data")
    thread_id = conversation(plain, "Billing", ("user", "annual pricing"))

    result = find(plain, "pricing")

    assert result["conversations"] == []
    assert plain.get(thread_id)["messages"][0]["text"] == "annual pricing"


# --- boot reconciliation -----------------------------------------------------


def test_boot_rebuilds_from_the_files_when_the_table_is_empty(tmp_path):
    """An upgrade from before the table existed, or a wiped index directory."""
    unindexed = ThreadStore(tmp_path / "vault", tmp_path / "data")
    conversation(unindexed, "Billing", ("user", "we settled on annual pricing"))

    index = ThreadIndex(tmp_path / "index" / "metadata.db")
    indexed = index.sync(unindexed.list(include_messages=True))

    assert indexed == 1
    store = ThreadStore(tmp_path / "vault", tmp_path / "data", index=index)
    assert find(store, "pricing")["conversations"]


def test_boot_leaves_a_populated_table_alone(store, tmp_path):
    conversation(store, "Billing", ("user", "annual pricing"))
    index = ThreadIndex(tmp_path / "index" / "metadata.db")

    assert index.sync(store.list(include_messages=True)) == 0
    assert find(store, "pricing")["conversations"]


def test_boot_purges_threads_whose_files_are_gone(store, tmp_path):
    thread_id = conversation(store, "Billing", ("user", "annual pricing"))
    (tmp_path / "vault" / ".exscalia" / "omnia" / "threads" / f"{thread_id}.json").unlink()

    ThreadIndex(tmp_path / "index" / "metadata.db").sync(store.list(include_messages=True))

    assert find(store, "pricing")["conversations"] == []


def test_boot_on_an_empty_vault_does_nothing(tmp_path):
    assert ThreadIndex(tmp_path / "index" / "metadata.db").sync([]) == 0


# --- query handling ----------------------------------------------------------


def test_fts_query_neutralizes_the_syntax_a_sentence_carries():
    assert fts_query("what did we decide") == '"what" OR "did" OR "we" OR "decide"'
    assert fts_query('NEAR("x" -y*)') == '"NEAR" OR "x" OR "y"'
    assert fts_query("!?") == ""


def test_a_query_of_pure_punctuation_returns_the_empty_note(store):
    conversation(store, "Billing", ("user", "annual pricing"))

    result = find(store, "!?")

    assert result["conversations"] == []
    assert "note" in result


def test_a_query_full_of_operators_does_not_error(store):
    conversation(store, "Billing", ("user", "annual pricing"))

    assert find(store, 'pricing NEAR( "unbalanced -*')["conversations"]


def test_a_malformed_match_expression_falls_back_instead_of_raising(store, monkeypatch):
    """Belt and braces: if sanitizing ever emitted something FTS5 rejects, the
    whole query goes back as one quoted phrase rather than 500ing the turn."""
    monkeypatch.setattr("app.index.thread_index.fts_query", lambda q: '"unbalanced')
    conversation(store, "Billing", ("user", "annual pricing"))

    assert find(store, "annual pricing")["conversations"]


def test_a_blank_query_is_still_rejected(store):
    with pytest.raises(NoteError):
        find(store, "   ")


def test_the_limit_caps_conversations_not_messages(store):
    for n in range(4):
        conversation(store, f"Chat {n}", ("user", "annual pricing"), ("assistant", "pricing agreed"))

    found = find(store, "pricing", limit=2)["conversations"]

    assert len(found) == 2
    assert all(c["match_count"] >= 1 for c in found)


# --- the contract read_conversation depends on -------------------------------


def test_the_result_shape_is_unchanged(store):
    conversation(store, "Billing", ("user", "annual pricing"), ("assistant", "pricing agreed"))

    result = find(store, "pricing")

    assert set(result) == {"conversations", "query"}
    assert result["query"] == "pricing"
    entry = result["conversations"][0]
    assert set(entry) == CONVERSATION_KEYS
    assert entry["title"] == "Billing"
    assert entry["updated_at"]
    assert all(set(m) == MATCH_KEYS for m in entry["matches"])


def test_at_most_three_excerpts_ride_along_per_conversation(store):
    conversation(store, "Billing", *[("user", f"pricing note {n}") for n in range(6)])

    entry = find(store, "pricing")["conversations"][0]

    assert entry["match_count"] == 6
    assert len(entry["matches"]) == 3


def test_a_result_chains_straight_into_read_conversation(store):
    conversation(store, "Billing", ("user", "annual pricing"))
    state = SimpleNamespace(threads=store)

    found = find(store, "pricing")["conversations"][0]
    transcript = asyncio.run(
        read_conversation(state, "t", {"thread_id": found["thread_id"]})
    )

    assert transcript["title"] == "Billing"
    assert transcript["messages"][0]["text"] == "annual pricing"


def test_the_tool_result_is_json_serializable(store):
    conversation(store, "Billing", ("user", "annual pricing"))

    assert json.loads(json.dumps(find(store, "pricing")))["conversations"]


# --- the boot wiring ---------------------------------------------------------


def test_a_booted_app_has_the_index_wired_into_its_thread_store(tmp_path, monkeypatch):
    """ThreadStore is the single choke point, but only if main.py hands it one."""
    from pathlib import Path

    from fastapi.testclient import TestClient

    from app import main as main_module
    from app.config import Settings

    monkeypatch.setattr(
        main_module,
        "get_settings",
        lambda: Settings(
            data_dir=tmp_path / "app-data",
            frontend_dir=Path(__file__).resolve().parents[2] / "frontend" / "build",
            embedding_backend="none",
            autocommit_debounce=0.01,
            auth_password=None,
            anthropic_api_key=None,
            openai_api_key=None,
            google_api_key=None,
            backup_remote=None,
        ),
    )

    with TestClient(main_module.app):
        state = main_module.app.state
        thread_id = conversation(state.threads, "Billing", ("user", "annual pricing"))
        found = find(state.threads, "pricing")["conversations"]

    assert [c["thread_id"] for c in found] == [thread_id]
