"""Regressions for the audit pass.

Each of these reproduces a defect that shipped while the whole suite was green.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.flashcards import FlashcardStore
from app.overseer.provider import _append_text
from app.overseer.threads import ThreadStore
from app.overseer.tool_expansion import MEMORY_NOTE, forget, remember
from app.vault import templates as tpl
from app.vault.internal import NoteError, read_json


# --- the morning greeting route --------------------------------------------


def test_the_morning_route_does_not_500(tmp_path: Path, monkeypatch):
    """It called `.weekday()` on a string, so it raised on every request —
    outside the try, and past the guard that would have short-circuited it, so
    it stayed broken forever. The route had no test at all.
    """
    from app import main as main_module

    settings = Settings(
        data_dir=tmp_path / "app-data",
        frontend_dir=Path(__file__).resolve().parents[2] / "frontend" / "build",
        embedding_backend="none",
        autocommit_debounce=0.01,
        auth_password=None,
        anthropic_api_key=None,
        openai_api_key=None,
        google_api_key=None,
        backup_remote=None,
    )
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)

    with TestClient(main_module.app) as client:
        first = client.post("/api/overseer/morning")
        assert first.status_code == 200, first.text
        assert "thread_id" in first.json()

        # And again: the second call takes the other branch through the guard.
        assert client.post("/api/overseer/morning").status_code == 200


def _morning_settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "app-data",
        frontend_dir=Path(__file__).resolve().parents[2] / "frontend" / "build",
        embedding_backend="none",
        autocommit_debounce=0.01,
        auth_password=None,
        anthropic_api_key=None,
        openai_api_key=None,
        google_api_key=None,
        backup_remote=None,
    )


BRIEFING_KEYS = {
    "today_events",
    "in_progress",
    "week",
    "stats",
    "backup_stale",
    "backup_failing_since",
}


def test_morning_returns_the_briefing_on_every_open(tmp_path: Path, monkeypatch):
    """The greeting is generated once a day, but the briefing (today's events,
    in-progress tasks, the week ahead) rides along on every open — including the
    already-greeted reload — so the banner can re-show until dismissed.
    """
    from app import main as main_module

    monkeypatch.setattr(main_module, "get_settings", lambda: _morning_settings(tmp_path))

    with TestClient(main_module.app) as client:
        first = client.post("/api/overseer/morning").json()
        assert first["greet"] is True
        briefing = first["briefing"]
        assert set(briefing) == BRIEFING_KEYS
        assert briefing["backup_stale"] is False  # no backup remote configured
        # The week ahead is exactly the seven days after today.
        assert len(briefing["week"]) == 7
        assert all(set(day) == {"date", "tasks", "events"} for day in briefing["week"])

        # Reload: no new greeting, but the persisted message and a fresh briefing
        # still come back (and OMNIA is never re-invoked — no key is configured,
        # so a regeneration would have produced a code-built line, not None-safe).
        again = client.post("/api/overseer/morning").json()
        assert again["greet"] is False
        assert again["message"] == first["message"]
        assert set(again["briefing"]) == BRIEFING_KEYS


BRIEFING_STATS = {
    "notes_created",
    "notes_modified",
    "tasks_completed",
    "flashcards_due",
    "activity_hours_yesterday",
    "pending_changes",
    "media_in_progress",
}


def test_briefing_stats_are_always_present_and_zeroed(tmp_path: Path, monkeypatch):
    """The banner renders the counts unconditionally, so a quiet vault has to
    return zeros rather than a missing key — and every one of them has to be a
    number, not None."""
    from app import main as main_module

    monkeypatch.setattr(main_module, "get_settings", lambda: _morning_settings(tmp_path))

    with TestClient(main_module.app) as client:
        stats = client.post("/api/overseer/morning").json()["briefing"]["stats"]

        assert set(stats) == BRIEFING_STATS
        assert all(isinstance(value, (int, float)) for value in stats.values())
        # A fresh install seeds the Welcome and memory notes, so notes_created
        # is not zero. Everything with no data behind it is.
        assert stats["notes_created"] >= 0
        assert stats["notes_modified"] == 0
        assert stats["tasks_completed"] == 0
        assert stats["flashcards_due"] == 0
        assert stats["activity_hours_yesterday"] == 0
        assert stats["pending_changes"] == 0
        assert stats["media_in_progress"] == 0
        # And on the already-greeted reload, which takes the other branch.
        assert client.post("/api/overseer/morning").json()["briefing"]["stats"] == stats


def test_the_morning_prompt_carries_the_recent_activity_it_used_to_discard(
    tmp_path: Path, monkeypatch
):
    """get_digest_data always returned what was created, modified and completed
    recently; the greeting prompt threw all of it away and asked the model to
    judge the day from due dates alone."""
    from app import main as main_module

    monkeypatch.setattr(main_module, "get_settings", lambda: _morning_settings(tmp_path))

    seen: list[str] = []

    class FakeOverseer:
        def __init__(self) -> None:
            self.sessions: dict = {}

        async def chat(self, message, *args, **kwargs) -> dict:
            seen.append(message)
            return {"reply": "Morning."}

    with TestClient(main_module.app) as client:
        client.app.state.overseer = FakeOverseer()
        client.post("/api/notes", json={"path": "Fresh.md", "content": "new today"})
        client.post("/api/overseer/morning")

    assert len(seen) == 1
    payload = json.loads(seen[0][seen[0].index("{") : seen[0].rindex("}") + 1])
    assert "Fresh" in payload["notes_created"]
    assert set(payload) >= {
        "overdue",
        "due_today",
        "events_today",
        "event_reminders",
        "notes_created",
        "notes_modified",
        "tasks_completed",
        "upcoming_events",
        "counts",
    }
    assert set(payload["counts"]) == BRIEFING_STATS
    assert payload["counts"]["notes_created"] == len(payload["notes_created"])
    # It is context, not a checklist — the prompt has to say so, or the model
    # reads a list of note titles as things it must recite.
    assert "NOT a checklist" in seen[0]


def test_morning_greeting_rejects_a_status_word_reply(tmp_path: Path, monkeypatch):
    """OMNIA is told to greet, but sometimes it acknowledges the silent check
    instead ("Reviewed."). The greeting must always begin with "Morning" — a
    non-conforming reply is dropped in favour of the code-built greeting."""
    from app import main as main_module

    monkeypatch.setattr(main_module, "get_settings", lambda: _morning_settings(tmp_path))

    class FakeOverseer:
        def __init__(self) -> None:
            self.sessions: dict = {}

        async def chat(self, *args, **kwargs) -> dict:
            return {"reply": "Reviewed."}

    with TestClient(main_module.app) as client:
        client.app.state.overseer = FakeOverseer()
        r = client.post("/api/overseer/morning").json()
        assert r["greet"] is True
        assert r["message"].startswith("Morning")
        assert "Reviewed" not in r["message"]


def test_greeting_text_returns_the_sanitized_first_message():
    """The header shows the day's GREETING (first assistant message), and a
    stored status word from an older build is shown as a plain "Morning."."""
    from app.api.overseer import _greeting_text

    assert _greeting_text({"messages": [{"role": "assistant", "text": "Reviewed."}]}) == "Morning."
    assert (
        _greeting_text({"messages": [{"role": "assistant", "text": "Morning — 2 overdue tasks."}]})
        == "Morning — 2 overdue tasks."
    )
    # The first assistant message wins, not a later chat reply.
    assert (
        _greeting_text(
            {
                "messages": [
                    {"role": "assistant", "text": "Morning."},
                    {"role": "user", "text": "clean up my tags"},
                    {"role": "assistant", "text": "Staged 3 changes."},
                ]
            }
        )
        == "Morning."
    )
    assert _greeting_text({"messages": []}) is None
    # The greeting stamp wins over the message scan — scheduled runs write
    # their status lines into the thread before the greeting exists.
    assert (
        _greeting_text(
            {
                "greeting": "Morning — quiz tonight.",
                "messages": [{"role": "assistant", "text": "Staged 5 tasks."}],
            }
        )
        == "Morning — quiz tonight."
    )
    assert _greeting_text({"greeting": "Reviewed."}) == "Morning."


def test_scheduled_runs_do_not_swallow_the_greeting(tmp_path: Path, monkeypatch):
    """The 04:00 scheduled runs create the daily thread and write status lines
    into it hours before the first open. The morning dedup keyed on "the thread
    has messages", so on every scheduled day the real greeting was silently
    skipped — the user opened the app to nothing."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app import main as main_module

    monkeypatch.setattr(main_module, "get_settings", lambda: _morning_settings(tmp_path))

    with TestClient(main_module.app) as client:
        state = client.app.state
        now = datetime.now(ZoneInfo(state.timezone))
        thread, created = state.threads.ensure_daily(
            now.date().isoformat(), now.strftime("%A")
        )
        assert created
        state.threads.append_public(
            thread["id"], {"role": "assistant", "text": "Reviewed."}
        )
        state.threads.append_public(
            thread["id"],
            {"role": "assistant", "text": "Staged five School tasks for review."},
        )

        first = client.post("/api/overseer/morning").json()
        assert first["greet"] is True
        assert first["message"].startswith("Morning")

        # And the stamp dedups the reload — same greeting, no regeneration,
        # never the scheduled runs' status lines.
        again = client.post("/api/overseer/morning").json()
        assert again["greet"] is False
        assert again["message"] == first["message"]


# --- templates: path traversal ---------------------------------------------


def test_a_template_name_cannot_escape_the_templates_folder(tmp_path):
    """`render` built its path by interpolation with no validation, so a name
    of "../../secret" read any .md file on the host into a new note."""
    vault = tmp_path / "vault"
    (vault / "templates").mkdir(parents=True)
    (vault / "templates" / "daily.md").write_text("# {{title}}", encoding="utf-8")
    secret = tmp_path / "secret.md"
    secret.write_text("PRIVATE", encoding="utf-8")

    assert tpl.render(vault, "daily", "Monday") == "# Monday"
    assert tpl.render(vault, "../../secret", "x") is None
    assert tpl.render(vault, "../secret", "x") is None
    assert tpl.render(vault, "/etc/hosts", "x") is None
    assert tpl.render(vault, "..", "x") is None


# --- read_json: corrupt files must not silently empty a store ---------------


def test_a_corrupt_store_is_quarantined_rather_than_read_as_empty(tmp_path):
    """Returning the default here destroyed data: the caller saw an empty
    collection and wrote a fresh one straight over the damaged file."""
    path = tmp_path / "events.json"
    path.write_text('{"events": [{"id": "a"', encoding="utf-8")  # truncated write

    with pytest.raises(NoteError):
        read_json(path, {})

    assert not path.exists(), "the unreadable file should have been set aside"
    quarantined = list(tmp_path.glob("events.json.corrupt-*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == '{"events": [{"id": "a"'


def test_a_missing_store_is_still_just_the_default(tmp_path):
    assert read_json(tmp_path / "nope.json", {"a": 1}) == {"a": 1}


def test_scanning_callers_can_skip_a_corrupt_file(tmp_path):
    path = tmp_path / "one.json"
    path.write_text("{oops", encoding="utf-8")
    assert read_json(path, None, skip_corrupt=True) is None


# --- memory: forget must not over-delete ------------------------------------


@pytest.mark.asyncio
async def test_forget_does_not_delete_every_entry_containing_the_substring(acting_state):
    """`forget("e")` used to match — and remove — every bullet in the note."""
    state = acting_state
    await remember(state, "s", {"text": "likes tea"})
    await remember(state, "s", {"text": "runs every morning"})

    with pytest.raises(NoteError) as excinfo:
        await forget(state, "s", {"text": "e"})
    assert "matches 2" in str(excinfo.value.detail)

    body = state.store.read(MEMORY_NOTE)
    assert "likes tea" in body and "runs every morning" in body


@pytest.mark.asyncio
async def test_forget_removes_an_exact_entry(acting_state):
    state = acting_state
    await remember(state, "s", {"text": "likes tea"})
    await remember(state, "s", {"text": "runs every morning"})

    await forget(state, "s", {"text": "likes tea"})

    body = state.store.read(MEMORY_NOTE)
    assert "likes tea" not in body
    assert "runs every morning" in body


@pytest.mark.asyncio
async def test_forget_accepts_an_unambiguous_partial(acting_state):
    state = acting_state
    await remember(state, "s", {"text": "likes tea"})
    await remember(state, "s", {"text": "runs every morning"})

    await forget(state, "s", {"text": "morning"})

    body = state.store.read(MEMORY_NOTE)
    assert "runs every morning" not in body
    assert "likes tea" in body


# --- flashcards: deck lookup must not match in reverse ----------------------


def test_a_short_deck_name_does_not_match_an_unrelated_query(tmp_path):
    """`name in needle` meant deck "AP" answered a lookup for "grapes" — and
    delete_flashcard_deck acted on whatever came back."""
    store = FlashcardStore(tmp_path)
    store.dir.mkdir(parents=True)
    store.save({"id": "ap", "name": "AP", "cards": []})

    assert store.find_by_name("AP")["id"] == "ap"
    assert store.find_by_name("ap")["id"] == "ap"
    assert store.find_by_name("grapes") is None


def test_a_partial_deck_lookup_still_works_but_not_when_ambiguous(tmp_path):
    store = FlashcardStore(tmp_path)
    store.dir.mkdir(parents=True)
    store.save({"id": "micro", "name": "Microeconomics — Midterm", "cards": []})
    assert store.find_by_name("microecon")["id"] == "micro"

    store.save({"id": "micro2", "name": "Microeconomics — Final", "cards": []})
    assert store.find_by_name("microecon") is None, "ambiguous: refuse to guess"


# --- threads: regenerating replaces an exchange instead of duplicating it ----


def _thread_with_exchange(tmp_path):
    store = ThreadStore(tmp_path)
    thread = store.create(title="t", thread_id="abcd1234")
    first = store.append_public(thread["id"], {"role": "user", "text": "one"})
    store.append_public(thread["id"], {"role": "assistant", "text": "answer one"})
    second = store.append_public(thread["id"], {"role": "user", "text": "two"})
    store.append_public(thread["id"], {"role": "assistant", "text": "answer two"})
    store.set_provider_history(
        thread["id"],
        [
            {"role": "user", "content": [{"type": "text", "text": "one"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "answer one"}]},
            {"role": "user", "content": [{"type": "text", "text": "two"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "answer two"}]},
        ],
    )
    return store, thread["id"], first, second


def test_truncating_drops_the_message_and_everything_after_it(tmp_path):
    store, thread_id, _first, second = _thread_with_exchange(tmp_path)

    store.truncate_from(thread_id, second["id"])

    messages = store.get(thread_id)["messages"]
    assert [m["text"] for m in messages] == ["one", "answer one"]


def test_truncating_rewinds_the_provider_history_in_step(tmp_path):
    """Leaving history behind meant the model kept answering a question the
    visible transcript no longer showed."""
    store, thread_id, _first, second = _thread_with_exchange(tmp_path)

    store.truncate_from(thread_id, second["id"])

    history = store.provider_history(thread_id)
    assert [block["content"][0]["text"] for block in history] == ["one", "answer one"]


def test_truncating_from_the_first_message_clears_the_thread(tmp_path):
    store, thread_id, first, _second = _thread_with_exchange(tmp_path)

    store.truncate_from(thread_id, first["id"])

    assert store.get(thread_id)["messages"] == []
    assert store.provider_history(thread_id) == []


def test_truncating_an_unknown_message_is_a_404(tmp_path):
    store, thread_id, _first, _second = _thread_with_exchange(tmp_path)
    with pytest.raises(NoteError):
        store.truncate_from(thread_id, "nope")


# --- streamed text is not fragmented into one block per chunk ---------------


def test_streamed_chunks_coalesce_into_one_block():
    """Each chunk used to become its own history block, so a long reply stored
    hundreds of them — every one repeating the same base64 signature."""
    raw: list[dict] = []
    for chunk in ("Hel", "lo ", "there"):
        _append_text(raw, chunk, None)
    assert raw == [{"type": "text", "text": "Hello there"}]


def test_a_signature_is_adopted_by_the_block_it_merges_into():
    raw: list[dict] = []
    _append_text(raw, "Hel", None)
    _append_text(raw, "lo", "sig-a")
    assert raw == [{"type": "text", "text": "Hello", "thought_signature": "sig-a"}]


def test_differing_signatures_are_never_merged_away():
    """Signatures must round-trip verbatim — Gemini 400s on a replayed call
    whose signature is missing, so merging must never drop one."""
    raw: list[dict] = []
    _append_text(raw, "one", "sig-a")
    _append_text(raw, "two", "sig-b")
    assert [b["thought_signature"] for b in raw] == ["sig-a", "sig-b"]
    assert [b["text"] for b in raw] == ["one", "two"]


def test_matching_signatures_do_merge():
    raw: list[dict] = []
    _append_text(raw, "one", "sig-a")
    _append_text(raw, "two", "sig-a")
    assert raw == [{"type": "text", "text": "onetwo", "thought_signature": "sig-a"}]


# --- provider history lives in a sidecar, not the git-backed transcript -----


def test_history_is_written_beside_the_vault_not_inside_it(tmp_path):
    vault, data = tmp_path / "vault", tmp_path / "data"
    store = ThreadStore(vault, data)
    thread = store.create(title="t", thread_id="side1234")
    store.set_provider_history(
        thread["id"], [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    )

    transcript = json.loads(store._path(thread["id"]).read_text())
    assert "provider_history" not in transcript
    assert store._history_path(thread["id"]).is_file()
    assert data in store._history_path(thread["id"]).parents
    assert store.provider_history(thread["id"])[0]["content"][0]["text"] == "hi"


def test_streaming_updates_do_not_rewrite_the_history(tmp_path):
    """The point of the split: a delta touches the transcript only."""
    store = ThreadStore(tmp_path / "vault", tmp_path / "data")
    thread = store.create(title="t", thread_id="side5678")
    message = store.append_public(thread["id"], {"role": "assistant", "text": ""})
    store.set_provider_history(
        thread["id"], [{"role": "user", "content": [{"type": "text", "text": "x" * 5000}]}]
    )

    history_file = store._history_path(thread["id"])
    before = history_file.stat().st_mtime_ns
    for i in range(20):
        store.update_message(thread["id"], message["id"], text="tok" * i)

    assert history_file.stat().st_mtime_ns == before, "history must not be rewritten"
    assert len(store.get(thread["id"])["messages"][0]["text"]) > 0


def test_a_legacy_inline_history_is_migrated_not_dropped(tmp_path):
    """Threads written before the split keep working, and stop bloating git."""
    vault, data = tmp_path / "vault", tmp_path / "data"
    store = ThreadStore(vault, data)
    thread = store.create(title="t", thread_id="oldstyle")
    # Simulate the old on-disk shape: history inline in the transcript.
    legacy = dict(json.loads(store._path(thread["id"]).read_text()))
    legacy["provider_history"] = [
        {"role": "user", "content": [{"type": "text", "text": "from before"}]}
    ]
    store._path(thread["id"]).write_text(json.dumps(legacy), encoding="utf-8")

    assert store.provider_history(thread["id"])[0]["content"][0]["text"] == "from before"
    on_disk = json.loads(store._path(thread["id"]).read_text())
    assert "provider_history" not in on_disk, "inline copy should be stripped"
    assert store._history_path(thread["id"]).is_file()


def test_deleting_a_thread_removes_its_history(tmp_path):
    store = ThreadStore(tmp_path / "vault", tmp_path / "data")
    thread = store.create(title="t", thread_id="gone1234")
    store.set_provider_history(
        thread["id"], [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    )
    sidecar = store._history_path(thread["id"])
    assert sidecar.is_file()

    store.delete(thread["id"])
    assert not sidecar.exists(), "orphaned history would leak forever"


def test_truncating_keeps_tool_results_with_their_calls(tmp_path):
    """Tool results are also role "user"; counting them as conversation
    boundaries would cut a call away from its result and brick the thread."""
    store = ThreadStore(tmp_path)
    thread = store.create(title="t", thread_id="efgh5678")
    store.append_public(thread["id"], {"role": "user", "text": "one"})
    store.append_public(thread["id"], {"role": "assistant", "text": "answer one"})
    second = store.append_public(thread["id"], {"role": "user", "text": "two"})
    store.set_provider_history(
        thread["id"],
        [
            {"role": "user", "content": [{"type": "text", "text": "one"}]},
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "t1", "name": "x", "input": {}}],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}],
            },
            {"role": "assistant", "content": [{"type": "text", "text": "answer one"}]},
            {"role": "user", "content": [{"type": "text", "text": "two"}]},
        ],
    )

    store.truncate_from(thread["id"], second["id"])

    history = store.provider_history(thread["id"])
    calls = [b for m in history for b in m["content"] if b.get("type") == "tool_use"]
    results = [b for m in history for b in m["content"] if b.get("type") == "tool_result"]
    assert len(calls) == len(results) == 1
    assert json.dumps(history), "history must stay serializable"


# --- sidebar folder order ---------------------------------------------------


def _client(tmp_path: Path, monkeypatch):
    from app import main as main_module

    settings = Settings(
        data_dir=tmp_path / "app-data",
        frontend_dir=Path(__file__).resolve().parents[2] / "frontend" / "build",
        embedding_backend="none",
        autocommit_debounce=0.01,
        auth_password=None,
        anthropic_api_key=None,
        openai_api_key=None,
        google_api_key=None,
        backup_remote=None,
    )
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    return TestClient(main_module.app)


def test_folder_order_round_trips(tmp_path: Path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        assert client.get("/api/settings/folder-order").json() == {"order": []}

        saved = client.put(
            "/api/settings/folder-order", json={"order": ["projects", "ideas"]}
        )
        assert saved.status_code == 200
        assert saved.json() == {"order": ["projects", "ideas"]}
        assert client.get("/api/settings/folder-order").json() == {
            "order": ["projects", "ideas"]
        }


def test_folder_order_drops_duplicates_and_blanks(tmp_path: Path, monkeypatch):
    """The client sends its whole list, so entries for folders that were since
    renamed fall away here rather than accumulating forever."""
    with _client(tmp_path, monkeypatch) as client:
        result = client.put(
            "/api/settings/folder-order",
            json={"order": ["a", "", "a", "/b/", "  ", "c"]},
        )
        assert result.json() == {"order": ["a", "b", "c"]}


def test_folder_order_survives_alongside_other_settings(tmp_path: Path, monkeypatch):
    """It shares config.json with the theme; neither may clobber the other."""
    with _client(tmp_path, monkeypatch) as client:
        client.put("/api/settings/folder-order", json={"order": ["x"]})
        before = client.get("/api/settings/appearance").json()
        client.put("/api/settings/appearance", json=before)
        assert client.get("/api/settings/folder-order").json() == {"order": ["x"]}


# --- templates must not leak frontmatter into the notes they create ---------


CORRUPTED_TEMPLATE = """---
title: '2026-07-23'
created: '2026-07-23T19:23:17Z'
modified: '2026-07-23T19:23:17Z'
---

title: '{{date}}'
created: '{{now}}'
modified: '2026-07-21T18:22:33Z'
---

## Notes
"""


def test_a_template_does_not_carry_frontmatter_into_the_new_note(tmp_path):
    """NoteStore.create writes the real frontmatter, so whatever the template
    carries is redundant — and when it survived it rendered as a heading."""
    vault = tmp_path / "vault"
    (vault / "templates").mkdir(parents=True)
    (vault / "templates" / "daily.md").write_text(
        "---\ntitle: '{{date}}'\n---\n\n## Notes\n", encoding="utf-8"
    )
    assert tpl.render(vault, "daily", "Monday", "2026-07-26") == "## Notes\n"


def test_an_orphaned_metadata_block_is_stripped_too(tmp_path):
    """A template whose own frontmatter stopped being recognized on save ends
    up with a bare title:/created:/modified: run closed by `---`. That is body
    text to every parser, so it was copied into every daily note and shown as a
    heading in Read mode — which is exactly what was reported."""
    vault = tmp_path / "vault"
    (vault / "templates").mkdir(parents=True)
    (vault / "templates" / "daily.md").write_text(CORRUPTED_TEMPLATE, encoding="utf-8")

    rendered = tpl.render(vault, "daily", "x", "2026-07-26")

    assert rendered == "## Notes\n"
    for leaked in ("title:", "created:", "modified:", "---"):
        assert leaked not in rendered


def test_body_content_that_merely_looks_like_metadata_is_kept(tmp_path):
    """`Agenda: things` followed by a horizontal rule is ordinary prose."""
    vault = tmp_path / "vault"
    (vault / "templates").mkdir(parents=True)
    (vault / "templates" / "meeting.md").write_text(
        "Agenda: things\n\n---\n\n## Notes\n", encoding="utf-8"
    )
    rendered = tpl.render(vault, "meeting", "x")
    assert rendered == "Agenda: things\n\n---\n\n## Notes\n"


def test_a_template_with_no_frontmatter_is_unchanged(tmp_path):
    vault = tmp_path / "vault"
    (vault / "templates").mkdir(parents=True)
    (vault / "templates" / "plain.md").write_text("## Notes\n\n## Tasks\n", encoding="utf-8")
    assert tpl.render(vault, "plain", "x") == "## Notes\n\n## Tasks\n"


def test_a_template_date_is_today_in_the_users_timezone(tmp_path):
    """The server runs in UTC, so date.today() gave an evening note in New
    York tomorrow's date. Kiritimati is UTC+14: its day differs from UTC's
    for most of every day."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    vault = tmp_path / "vault"
    (vault / "templates").mkdir(parents=True)
    (vault / "templates" / "t.md").write_text("{{date}}\n", encoding="utf-8")
    expected = datetime.now(ZoneInfo("Pacific/Kiritimati")).date().isoformat()
    assert tpl.render(vault, "t", "x", timezone="Pacific/Kiritimati") == f"{expected}\n"
    # An explicit date still wins.
    assert (
        tpl.render(vault, "t", "x", "2026-07-26", timezone="Pacific/Kiritimati")
        == "2026-07-26\n"
    )


# --- live sync across devices ----------------------------------------------


def test_a_stale_save_can_ask_for_the_newer_content_instead_of_a_copy(
    tmp_path: Path, monkeypatch
):
    """The old behaviour parked every stale save in a `(conflict …).md` note.
    An editor that can three-way merge asks for the newer content instead, so
    two devices editing different lines never litter the vault."""
    with _client(tmp_path, monkeypatch) as client:
        created = client.post("/api/notes", json={"path": "Sync.md"})
        assert created.status_code == 200
        first = client.get("/api/notes/Sync.md").json()

        # Device A saves.
        a = client.put(
            "/api/notes/Sync.md",
            json={
                "content": first["content"] + "\nfrom A\n",
                "base_revision": first["revision"],
                "client_id": "device-a",
            },
        ).json()
        assert a["status"] == "saved"

        # Device B saves against the now-stale revision, asking for a rejection.
        b = client.put(
            "/api/notes/Sync.md",
            json={
                "content": first["content"] + "\nfrom B\n",
                "base_revision": first["revision"],
                "client_id": "device-b",
                "on_conflict": "reject",
            },
        ).json()
        assert b["status"] == "stale"
        assert b["revision"] == a["revision"]
        assert "from A" in b["content"], "the caller needs their version to merge against"

        listed = client.get("/api/notes").json()
        paths = [n["path"] for n in listed["notes"]] if isinstance(listed, dict) else []
        assert not [p for p in paths if "conflict" in p], "no copy should exist"


def test_the_conflict_copy_is_still_the_default(tmp_path: Path, monkeypatch):
    """Any caller that cannot merge must keep the old safety net."""
    with _client(tmp_path, monkeypatch) as client:
        client.post("/api/notes", json={"path": "Legacy.md"})
        first = client.get("/api/notes/Legacy.md").json()
        client.put(
            "/api/notes/Legacy.md",
            json={"content": first["content"] + "\nA\n", "base_revision": first["revision"]},
        )
        second = client.put(
            "/api/notes/Legacy.md",
            json={"content": first["content"] + "\nB\n", "base_revision": first["revision"]},
        ).json()
        assert second["status"] == "conflict_copy"
        assert "conflict" in second["conflict_path"]


def test_saving_announces_the_change_to_other_devices(tmp_path: Path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        bus = client.app.state.live
        queue = bus.subscribe()
        client.post("/api/notes", json={"path": "Announce.md"})
        current = client.get("/api/notes/Announce.md").json()
        client.put(
            "/api/notes/Announce.md",
            json={
                "content": current["content"] + "\nhello\n",
                "base_revision": current["revision"],
                "client_id": "device-a",
            },
        )
        seen = []
        while not queue.empty():
            seen.append(queue.get_nowait())
        notes = [e for e in seen if e["type"] == "note" and e["path"] == "Announce.md"]
        assert notes, f"expected a note event, saw {seen}"
        assert notes[-1]["origin"] == "device-a", "so the sender can ignore its own echo"


def test_a_slow_subscriber_cannot_block_a_save(tmp_path: Path):
    """A wedged reader must cost a stale editor, never the edit itself."""
    from app.live import MAX_PENDING, LiveBus

    bus = LiveBus()
    queue = bus.subscribe()
    for index in range(MAX_PENDING + 25):
        bus.note_changed(f"n{index}.md", "rev")  # must not raise or hang
    assert queue.qsize() == MAX_PENDING
