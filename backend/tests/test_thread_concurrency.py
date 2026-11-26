"""Two generations on one thread.

A user turn, a scheduled run and the morning check can all land on today's
daily thread. Only the user turn took the thread's generation lock, and every
caller read its history back out of the overseer's shared sessions dict, so
whichever run seeded that dict last decided what got saved and the other
exchange vanished from provider history while the transcript still showed it.
Run tracking had one slot per thread, so a queued second request hid the
generating one from Stop and from is_running.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.api import overseer as overseer_api
from app.api.overseer import _run_thread_chat
from app.config import Settings
from app.overseer.agent import MAX_SESSIONS, Overseer
from app.overseer.provider import FakeProvider, ProviderReply, ToolCall
from app.overseer.threads import ThreadStore
from app.scheduler import _run_one, schedule_tz
from app.vault.notes import NoteError

TID = "thread-1234"


# --- run tracking ------------------------------------------------------------


async def _settle() -> None:
    for _ in range(3):
        await asyncio.sleep(0)


def test_stop_cancels_the_generating_turn_and_the_queued_one(tmp_path: Path):
    """The queued request registered second and took the only slot, so Stop
    cancelled it and the turn actually generating carried on."""
    threads = ThreadStore(tmp_path / "vault", tmp_path)

    async def scenario():
        release = asyncio.Event()
        generating = asyncio.create_task(release.wait())
        queued = asyncio.create_task(release.wait())
        try:
            threads.register_run(TID, generating)
            threads.register_run(TID, queued)
            await _settle()
            assert threads.is_running(TID)

            assert threads.stop_run(TID) is True
            await _settle()
            assert generating.cancelled()
            assert queued.cancelled()
            assert not threads.is_running(TID)
            assert threads.stop_run(TID) is False
        finally:
            release.set()
            await asyncio.gather(generating, queued, return_exceptions=True)

    asyncio.run(scenario())


def test_clearing_one_run_leaves_the_other_tracked(tmp_path: Path):
    """The queued request's stream closing first must not make the thread look
    idle: truncate and housekeeping gate on is_running."""
    threads = ThreadStore(tmp_path / "vault", tmp_path)

    async def scenario():
        release = asyncio.Event()
        generating = asyncio.create_task(release.wait())
        queued = asyncio.create_task(release.wait())
        try:
            threads.register_run(TID, generating)
            threads.register_run(TID, queued)
            queued.cancel()
            await _settle()
            # Finished but not yet cleared (its stream finalizer is deferred).
            assert threads.is_running(TID)

            threads.clear_run(TID, queued)
            assert threads.is_running(TID)
            assert threads.stop_run(TID) is True
            await _settle()
            assert generating.cancelled()

            threads.clear_run(TID, generating)
            assert not threads.is_running(TID)
            assert TID not in threads._runs
            # A stale or unknown clear is a no-op.
            threads.clear_run(TID, generating)
            threads.clear_run("thread-none", generating)
        finally:
            release.set()
            await asyncio.gather(generating, queued, return_exceptions=True)

    asyncio.run(scenario())


# --- shared history on the daily thread ---------------------------------------


class YieldingProvider(FakeProvider):
    """FakeProvider never awaits, so two turns could never interleave inside a
    model call. This one yields to the loop first, and records whether the
    thread's generation lock was held each time it was called."""

    def __init__(self, script, threads, thread_id: str, on_call=None):
        super().__init__(script)
        self.threads = threads
        self.thread_id = thread_id
        self.on_call = on_call
        self.locked: list[bool] = []

    async def chat(self, system, messages, tools, on_text=None, allow_tools=True, context=None):
        self.locked.append(self.threads.generation_lock(self.thread_id).locked())
        if self.on_call is not None:
            self.on_call()
        await _settle()
        return await super().chat(
            system, messages, tools, on_text=on_text, allow_tools=allow_tools, context=context
        )


def reply(text: str) -> ProviderReply:
    return ProviderReply(text=text, stop_reason="end_turn")


def turns(history: list[dict]) -> list[tuple[str, str]]:
    out = []
    for message in history:
        content = message.get("content")
        if isinstance(content, list):
            content = " ".join(
                str(block.get("text") or "") for block in content if isinstance(block, dict)
            )
        out.append((message.get("role"), str(content)))
    return out


def daily_thread(state) -> str:
    now = datetime.now(schedule_tz(state))
    thread, _ = state.threads.ensure_daily(now.date().isoformat(), now.strftime("%A, %B %-d"))
    return thread["id"]


@pytest.fixture
def daily(acting_state, tmp_path: Path):
    state = acting_state
    state.threads = ThreadStore(state.settings.vault_dir, tmp_path)
    return state, daily_thread(state)


SCHEDULE = {"id": "sched-1", "instruction": "summarize tasks"}


def test_a_scheduled_run_waits_for_the_user_turn_and_both_are_kept(daily):
    state, tid = daily
    provider = YieldingProvider(
        [reply("Answer to hi"), reply("Scheduled summary")], state.threads, tid
    )
    overseer = Overseer(provider, state)
    state.overseer = overseer

    async def scenario():
        await asyncio.gather(
            _run_thread_chat(state, tid, "hi", None),
            _run_one(state, overseer, SCHEDULE),
        )

    asyncio.run(scenario())

    assert provider.locked == [True, True]
    history = turns(state.threads.provider_history(tid))
    assert [role for role, _ in history] == ["user", "assistant", "user", "assistant"]
    assert history[0] == ("user", "hi")
    assert history[1] == ("assistant", "Answer to hi")
    assert history[2][1].startswith("(Scheduled run")
    assert history[3] == ("assistant", "Scheduled summary")
    public = [(m["role"], m["text"]) for m in state.threads.get(tid)["messages"]]
    assert public == [
        ("user", "hi"),
        ("assistant", "Answer to hi"),
        ("assistant", "Scheduled summary"),
    ]


def crowd_out(overseer: Overseer, tid: str):
    """What twenty other chats inside one turn do to the session cache: the
    in-flight session is least recently used, so it is evicted."""

    def run() -> None:
        for index in range(MAX_SESSIONS):
            overseer.sessions[f"other-{index}"] = []
        while len(overseer.sessions) > MAX_SESSIONS:
            overseer.sessions.pop(next(iter(overseer.sessions)))
        assert tid not in overseer.sessions

    return run


@pytest.mark.parametrize("caller", ["user turn", "scheduled run"])
def test_an_evicted_session_still_persists_the_whole_turn(daily, caller):
    """Reading `sessions.get(tid) or []` after chat() saved an empty history
    once the in-flight session had been evicted."""
    state, tid = daily
    state.threads.set_provider_history(
        tid, [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "ok"}]
    )
    overseer = Overseer(YieldingProvider([reply("Done")], state.threads, tid), state)
    overseer.provider.on_call = crowd_out(overseer, tid)
    state.overseer = overseer

    if caller == "user turn":
        asyncio.run(_run_thread_chat(state, tid, "hi", None))
    else:
        asyncio.run(_run_one(state, overseer, SCHEDULE))

    history = turns(state.threads.provider_history(tid))
    assert [role for role, _ in history] == ["user", "assistant", "user", "assistant"]
    assert history[:2] == [("user", "earlier"), ("assistant", "ok")]
    assert history[3] == ("assistant", "Done")


def test_truncate_waits_out_a_run_that_is_not_registered(daily):
    """Scheduled runs and the morning check hold the lock without a registered
    task; a truncate under one was overwritten by its untruncated history."""
    state, tid = daily
    state.overseer = None
    asked = state.threads.append_public(tid, {"role": "user", "text": "hi"})
    app = FastAPI()
    app.state = state

    @app.exception_handler(NoteError)
    async def handler(_request, exc: NoteError):
        return JSONResponse(status_code=exc.status, content={"detail": exc.detail})

    app.include_router(overseer_api.router)
    url = f"/api/omnia/threads/{tid}/messages/{asked['id']}"

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            async with state.threads.generation_lock(tid):
                blocked = await client.delete(url)
            allowed = await client.delete(url)
        return blocked, allowed

    blocked, allowed = asyncio.run(scenario())

    assert blocked.status_code == 409, blocked.text
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["messages"] == []


# --- the morning check-in ------------------------------------------------------


def _settings(tmp_path: Path) -> Settings:
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


def test_the_morning_check_holds_the_lock_and_persists_its_own_history(
    tmp_path: Path, monkeypatch
):
    from app import main as main_module

    monkeypatch.setattr(main_module, "get_settings", lambda: _settings(tmp_path))

    with TestClient(main_module.app) as client:
        state = client.app.state
        tid = "daily-" + datetime.now(ZoneInfo(state.timezone)).date().isoformat()
        provider = YieldingProvider([reply("Morning.")], state.threads, tid)
        state.overseer = Overseer(provider, state)
        provider.on_call = crowd_out(state.overseer, tid)

        body = client.post("/api/overseer/morning").json()
        history = turns(state.threads.provider_history(tid))

    assert body["thread_id"] == tid
    assert body["message"] == "Morning."
    assert provider.locked == [True]
    assert [role for role, _ in history] == ["user", "assistant"]
    assert history[0][1].startswith("(Internal morning check-in")
    assert history[1] == ("assistant", "Morning.")


def test_narration_before_a_tool_call_does_not_cost_the_morning_greeting(
    tmp_path: Path, monkeypatch
):
    """An unstreamed turn's reply joins every round's text, so a check that
    narrated before reading a note no longer began with "Morning" and was
    thrown away for the canned greeting. The guard reads the closing round."""
    from app import main as main_module

    monkeypatch.setattr(main_module, "get_settings", lambda: _settings(tmp_path))
    narration = "Checking yesterday's note."
    script = [
        ProviderReply(
            text=narration,
            tool_calls=[ToolCall(id="t1", name="read_structure", input={})],
            stop_reason="tool_use",
            raw_content=[
                {"type": "text", "text": narration},
                {"type": "tool_use", "id": "t1", "name": "read_structure", "input": {}},
            ],
        ),
        reply("Morning — the essay draft is due today."),
    ]

    with TestClient(main_module.app) as client:
        state = client.app.state
        state.overseer = Overseer(FakeProvider(script), state)
        body = client.post("/api/overseer/morning").json()

    assert body["message"] == "Morning — the essay draft is due today."
