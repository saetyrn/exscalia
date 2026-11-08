"""What happens to a conversation's opening when the context window fills.

Trimming is permanent and silent: the dropped exchanges are not written back
anywhere, so a long thread forgot how it started and the model went on
answering follow-ups having lost the premise. The hook replaces what is about
to go with a paragraph — and, because it costs a model call, it has to give up
quietly whenever it can't.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from app.overseer import history
from app.overseer.provider import FakeProvider, ProviderReply
from app.overseer.usage import UsageLedger

LIMIT = 40_000


def build_state(tmp_path: Path, *replies) -> SimpleNamespace:
    provider = FakeProvider(list(replies))
    return SimpleNamespace(
        settings=SimpleNamespace(
            data_dir=tmp_path,
            vault_dir=tmp_path / "vault",
            ai_provider="anthropic",
            anthropic_api_key=None,
            openai_api_key=None,
            google_api_key=None,
            anthropic_model="claude-opus-4-8",
            openai_model="gpt-5.6-sol",
            google_model="gemini-3.6-flash",
        ),
        overseer=SimpleNamespace(provider=provider, sessions={}),
        usage=UsageLedger(tmp_path / "vault"),
    )


def long_history(exchanges: int = 12) -> list[dict]:
    messages: list[dict] = []
    for index in range(exchanges):
        messages.append({"role": "user", "content": f"question {index} " + "x" * 4000})
        messages.append({"role": "assistant", "content": f"answer {index}"})
    return messages


def summarized(text="They set up the project and agreed on the folder layout."):
    return ProviderReply(text=text, stop_reason="end_turn")


def run(state, messages, limit=LIMIT):
    return asyncio.run(history.summarize_before_trim(state, messages, limit))


def test_the_dropped_opening_comes_back_as_a_paragraph(tmp_path):
    state = build_state(tmp_path, summarized())
    messages = long_history()

    out = run(state, messages)

    assert out[0]["content"].startswith(history.SUMMARY_PREFIX)
    assert "folder layout" in out[0]["content"]
    # The recent exchange is untouched, and the whole thing now fits — so the
    # trim that follows in set_provider_history has nothing left to drop.
    assert out[-1]["content"] == "answer 11"
    assert history.history_chars(out) <= LIMIT
    trimmed, notes = history.trim_history(out, LIMIT)
    assert notes == []
    # What it summarized is what was going to be lost.
    assert "question 0" in state.overseer.provider.seen[-1][0]["content"]


def test_it_is_metered_as_its_own_caller(tmp_path):
    state = build_state(tmp_path, summarized())
    state.overseer.provider.script[0].usage = {"input": 900, "output": 40}

    run(state, long_history())

    assert state.usage.month_summary()["by_caller"]["history"]["calls"] == 1


def test_a_history_that_fits_is_handed_straight_back(tmp_path):
    state = build_state(tmp_path, summarized())
    messages = long_history(2)

    assert run(state, messages) is messages
    assert state.overseer.provider.seen == []  # not a token spent


def test_a_paused_budget_falls_back_to_the_old_lossy_trim(tmp_path):
    state = build_state(tmp_path, summarized())
    state.usage.allow_background = lambda budget: False
    messages = long_history()

    assert run(state, messages) is messages
    assert state.overseer.provider.seen == []


def test_a_failed_call_never_costs_the_turn(tmp_path):
    class Broken(FakeProvider):
        async def chat(self, system, messages, tools, on_text=None):
            raise RuntimeError("provider down")

    state = build_state(tmp_path)
    state.overseer.provider = Broken([])
    messages = long_history()

    out = run(state, messages)

    assert out is messages  # unchanged; the existing trim takes over
    trimmed, notes = history.trim_history(out, LIMIT)
    assert notes and history.history_chars(trimmed) <= LIMIT


def test_an_empty_reply_is_not_prepended(tmp_path):
    state = build_state(tmp_path, summarized(""))
    messages = long_history()

    assert run(state, messages) is messages


def test_the_summary_is_capped(tmp_path):
    state = build_state(tmp_path, summarized("word " * 2000))

    out = run(state, long_history())

    assert len(out[0]["content"]) <= history.MAX_SUMMARY_CHARS + len(
        history.SUMMARY_PREFIX
    ) + 1


def test_a_real_turn_summarizes_before_the_history_is_persisted(tmp_path, monkeypatch):
    """The wiring, at the one call site that has an event loop and a finished
    turn. Without it the hook is dead code and the trim stays lossy."""
    from fastapi.testclient import TestClient

    from app import main as main_module
    from app.config import Settings
    from app.overseer.threads import MAX_HISTORY_CHARS

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

    oversized = long_history(exchanges=(MAX_HISTORY_CHARS // 4000) + 4)

    class Overgrown:
        """An overseer whose turn leaves a history past the ceiling."""

        def __init__(self):
            self.sessions: dict = {}
            self.provider = FakeProvider([summarized()])

        async def chat(self, message, session_id=None, **kwargs):
            # Like the real one: appended to the list the caller seeded, which
            # is what the caller persists.
            self.sessions[session_id].extend(oversized)
            return {"reply": "done", "trace": [], "graphics": [], "actions": []}

    with TestClient(main_module.app) as client:
        client.app.state.overseer = Overgrown()
        thread_id = client.post("/api/omnia/threads").json()["id"]
        client.post("/api/overseer/chat", json={"message": "hi", "session_id": thread_id})

        stored = client.app.state.threads.provider_history(thread_id)

    assert stored[0]["content"].startswith(history.SUMMARY_PREFIX)
    assert history.history_chars(stored) <= MAX_HISTORY_CHARS


def test_tool_calls_are_described_rather_than_pasted(tmp_path):
    """The transcript the summarizer sees has to be bounded: a raw history is
    mostly 30k-char tool results."""
    messages = [
        {"role": "user", "content": "read my notes"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "looking"},
                {"type": "tool_use", "id": "t1", "name": "read_notes", "input": {}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "y" * 50_000}
            ],
        },
    ]

    text = history.transcript(messages)

    assert "called read_notes" in text
    assert len(text) < 2000
