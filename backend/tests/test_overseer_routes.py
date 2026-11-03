"""The OMNIA routes where the user's yes, no or stop lands, and the AI-key
settings route.

Approve is where a confirmation card turns into destructive execution, and the
config route holds the provider keys; none of them had a test that went through
HTTP. The state is the offline test state from conftest, served by the real
overseer router.
"""
import asyncio
import logging
import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.api import overseer as overseer_api
from app.config import Settings
from app.overseer import tool_expansion
from app.overseer.agent import Overseer
from app.overseer.provider import FakeProvider
from app.overseer.runtime_config import load_ai_config, provider_info, save_ai_config
from app.overseer.threads import ThreadStore
from app.task_service import create_task
from app.vault.notes import NoteError

TZ = "America/New_York"


def make_client(state) -> TestClient:
    app = FastAPI()
    app.state = state

    @app.exception_handler(NoteError)
    async def handler(_request, exc: NoteError):
        return JSONResponse(status_code=exc.status, content={"detail": exc.detail})

    app.include_router(overseer_api.router)
    return TestClient(app)


@pytest.fixture
def routes(acting_state, tmp_path: Path):
    state = acting_state
    state.threads = ThreadStore(state.settings.vault_dir, tmp_path)
    # Signed-out single user: every request resolves to auth session None,
    # which is what the confirmations below are requested under.
    state.auth = SimpleNamespace(
        session_for_token=lambda *a, **k: None,
        token_from_request=lambda request: None,
    )
    state.ai_config = {}
    state.alerts = None
    state.overseer = None
    state.timezone = TZ
    # Mirror the fields of config.Settings that the config route reads.
    state.settings = SimpleNamespace(
        vault_dir=state.settings.vault_dir,
        data_dir=state.settings.data_dir,
        ai_provider="anthropic",
        anthropic_api_key=None,
        openai_api_key=None,
        google_api_key=None,
        anthropic_model="claude-opus-4-8",
        openai_model="gpt-5.6-sol",
        google_model="gemini-3.6-flash",
    )
    return state, make_client(state)


def card_message(state, thread_id: str, card: dict) -> dict:
    """The assistant message a confirmation card rides on, as the agent loop
    persists it."""
    return state.threads.append_public(
        thread_id,
        {"role": "assistant", "text": "", "confirmations": [{**card, "status": "pending"}]},
    )


def approve(client, token: str, thread_id: str):
    return client.post(
        f"/api/omnia/confirmations/{token}/approve", json={"thread_id": thread_id}
    )


# --- approve / cancel / stop -------------------------------------------------


def test_a_bulk_approval_that_fails_midway_keeps_what_it_applied_undoable(routes):
    """The loop consumes the token first, then runs each operation. When one
    fails, the earlier ones have landed: the 409 has to say so, they have to
    stay undoable, and the card must not be approvable a second time."""
    state, client = routes
    thread_id = state.threads.create()["id"]
    card = state.actions.request_confirmation(
        thread_id,
        "bulk_actions",
        {
            "operations": [
                {"tool": "create_note", "inputs": {"path": "Bulk first.md", "content": "applied"}},
                {
                    "tool": "delete_note",
                    "inputs": {"path": "Never existed.md", "base_revision": "0"},
                },
            ]
        },
    )
    card_message(state, thread_id, card)

    failed = approve(client, card["token"], thread_id)

    assert failed.status_code == 409, failed.text
    assert "1 earlier action(s) were applied and remain undoable" in failed.json()["detail"]
    assert state.store.exists("Bulk first.md")

    actions = client.get("/api/omnia/actions").json()["actions"]
    assert [(a["tool"], a["undoable"]) for a in actions] == [("create_note", True)]

    # Consumed before the loop ran, so a retry cannot run op 1 twice.
    assert approve(client, card["token"], thread_id).status_code == 404

    message = state.threads.get(thread_id)["messages"][-1]
    assert [c["status"] for c in message["confirmations"]] == ["failed"]
    assert [a["id"] for a in message["actions"]] == [actions[0]["id"]]


def test_an_approved_tool_that_stops_part_way_puts_its_undo_on_the_card(routes, monkeypatch):
    """bulk_edit_tasks always confirms. When it stops on row 2 the row it
    changed is recorded as one partial action, and that action has to reach
    the card, or the user's only way back is History."""
    state, client = routes
    for n in range(3):
        asyncio.run(create_task(state, text=f"review {n}", path="Tasks Inbox.md"))
    real, calls = tool_expansion.update_task_record, []

    async def flaky(state, task_id, **changes):
        calls.append(task_id)
        if len(calls) == 2:
            raise NoteError(409, "Task changed; refresh and try again")
        return await real(state, task_id, **changes)

    monkeypatch.setattr(tool_expansion, "update_task_record", flaky)
    thread_id = state.threads.create()["id"]
    card = state.actions.request_confirmation(
        thread_id, "bulk_edit_tasks", {"text": "review", "set_status": "done"}
    )
    card_message(state, thread_id, card)

    failed = approve(client, card["token"], thread_id)

    assert failed.status_code == 409, failed.text
    assert failed.json()["detail"].startswith("Applied 1 of 3")
    actions = client.get("/api/omnia/actions").json()["actions"]
    assert [(a["summary"], a["undoable"]) for a in actions] == [
        ("Bulk set status to done on 1 task (partial: 1 of 3)", True)
    ]
    message = state.threads.get(thread_id)["messages"][-1]
    assert [c["status"] for c in message["confirmations"]] == ["failed"]
    assert [a["id"] for a in message["actions"]] == [actions[0]["id"]]


def test_a_cancelled_card_cannot_be_approved_afterwards(routes):
    state, client = routes
    state.store.create("Keep me.md", "still here")
    thread_id = state.threads.create()["id"]
    card = state.actions.request_confirmation(
        thread_id, "delete_note", {"path": "Keep me.md"}
    )
    card_message(state, thread_id, card)

    cancelled = client.post(
        f"/api/omnia/confirmations/{card['token']}/cancel", json={"thread_id": thread_id}
    )

    assert cancelled.status_code == 200
    assert cancelled.json() == {"ok": True}
    assert approve(client, card["token"], thread_id).status_code == 404
    assert state.store.exists("Keep me.md")
    message = state.threads.get(thread_id)["messages"][-1]
    assert [c["status"] for c in message["confirmations"]] == ["cancelled"]


def test_stop_on_an_idle_thread_reports_nothing_stopped(routes):
    state, client = routes
    thread_id = state.threads.create()["id"]

    response = client.post(f"/api/omnia/threads/{thread_id}/stop")

    assert response.status_code == 200
    assert response.json() == {"stopped": False}


# --- turn failures -------------------------------------------------------------


class BrokenProvider(FakeProvider):
    """A code bug below the route: the exception text alone says nothing."""

    async def chat(self, *args, **kwargs):
        raise KeyError("id")


def test_a_failed_turn_is_logged_with_the_ref_the_thread_shows(routes, caplog):
    """The thread kept only str(exc) — "'id'" — with no traceback anywhere. The
    log line and the persisted error share a ref so one leads to the other."""
    state, client = routes
    state.overseer = Overseer(BrokenProvider([]), state)
    thread_id = state.threads.create()["id"]

    with caplog.at_level(logging.ERROR, logger=overseer_api.__name__):
        response = client.post(
            "/api/overseer/chat", json={"message": "hello", "session_id": thread_id}
        )

    assert response.status_code == 200
    assert response.json()["error"] == "provider_error"
    records = [
        record
        for record in caplog.records
        if record.getMessage().startswith("OMNIA turn failed")
    ]
    assert len(records) == 1
    assert records[0].exc_info is not None  # the traceback, not just the text
    logged = records[0].getMessage()
    assert f"thread={thread_id}" in logged
    ref = re.search(r"ref=([0-9a-f]{8})$", logged).group(1)

    stored = state.threads.get(thread_id)["messages"][-1]
    assert stored["status"] == "error"
    assert stored["text"] == f"OMNIA hit an error (ref {ref}): 'id'"


def test_a_failed_morning_model_call_is_logged_before_the_fallback(
    tmp_path: Path, monkeypatch, caplog
):
    """A revoked key turned every morning into the code-built greeting with no
    trace of why."""
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
    calls: list[str] = []

    class FailingOverseer:
        def __init__(self) -> None:
            self.sessions: dict = {}

        async def chat(self, message, *args, **kwargs) -> dict:
            calls.append(message)
            raise RuntimeError("provider rejected the key")

    with TestClient(main_module.app) as client:
        client.app.state.overseer = FailingOverseer()
        with caplog.at_level(logging.WARNING, logger=overseer_api.__name__):
            body = client.post("/api/overseer/morning").json()
        thread = client.get(f"/api/omnia/threads/{body['thread_id']}").json()

    assert len(calls) == 1
    assert body["greet"] is True
    assert body["message"].startswith("Morning")
    assert thread["messages"][-1]["text"] == body["message"]
    warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING
        and "morning check model call failed" in record.getMessage()
    ]
    assert len(warnings) == 1
    assert "provider rejected the key" in str(warnings[0].exc_info[1])


# --- AI configuration ----------------------------------------------------------


def ai_file(state) -> Path:
    return state.settings.data_dir / "ai.json"


def seed_config(state, config: dict) -> None:
    save_ai_config(state.settings.data_dir, config)
    state.ai_config = load_ai_config(state.settings.data_dir)


def test_a_rejected_config_save_changes_neither_memory_nor_disk(routes):
    """The key was written into the live config before the model and reasoning
    checks ran, so a 400 left a key in memory that the file never got."""
    state, client = routes
    seed_config(
        state,
        {
            "provider": "anthropic",
            "keys": {"anthropic": "sk-ant-old-key-1111"},
            "models": {"anthropic": "claude-opus-4-8"},
        },
    )
    on_disk = ai_file(state).read_bytes()

    bad_model = client.post(
        "/api/overseer/config",
        json={"provider": "anthropic", "api_key": "sk-ant-new-key-2222", "model": "no-such-model"},
    )
    bad_reasoning = client.post(
        "/api/overseer/config", json={"clear_key": True, "reasoning": "extreme"}
    )

    assert bad_model.status_code == 400
    assert bad_reasoning.status_code == 400
    assert state.ai_config["keys"] == {"anthropic": "sk-ant-old-key-1111"}
    assert state.ai_config["models"] == {"anthropic": "claude-opus-4-8"}
    assert ai_file(state).read_bytes() == on_disk
    shown = client.get("/api/overseer/config").json()
    assert shown["keys"]["anthropic"]["hint"] == "…1111"


def test_a_config_save_builds_on_the_file_not_a_boot_copy(routes):
    """Boot keeps {} when ai.json cannot be read then; a later save built on
    that copy wrote one field over every provider key."""
    state, client = routes
    seed_config(
        state,
        {
            "provider": "anthropic",
            "keys": {"anthropic": "sk-ant-old-key-1111", "openai": "sk-openai-2222"},
            "models": {"anthropic": "claude-opus-4-8"},
        },
    )
    state.ai_config = {}
    model = provider_info("anthropic")["models"][1]["id"]

    saved = client.post("/api/overseer/config", json={"model": model})

    assert saved.status_code == 200, saved.text
    stored = load_ai_config(state.settings.data_dir)
    assert stored["keys"] == {"anthropic": "sk-ant-old-key-1111", "openai": "sk-openai-2222"}
    assert stored["models"] == {"anthropic": model}
    assert state.ai_config == stored


def test_a_saved_key_is_only_ever_shown_as_its_last_four(routes):
    state, client = routes
    key = "sk-ant-api03-a-very-secret-value-WXYZ"
    model = provider_info("anthropic")["models"][1]["id"]

    saved = client.post(
        "/api/overseer/config",
        json={"provider": "anthropic", "api_key": key, "model": model, "reasoning": "high"},
    )
    shown = client.get("/api/overseer/config")

    assert saved.status_code == 200, saved.text
    for response in (saved, shown):
        assert key not in response.text
        assert key[:-4] not in response.text
        status = response.json()["keys"]["anthropic"]
        assert status == {"set": True, "hint": "…WXYZ", "source": "ui"}
        assert response.json()["model"] == model
        assert response.json()["reasoning"] == "high"
    assert shown.json()["available"] is True
    stored = load_ai_config(state.settings.data_dir)
    assert stored["keys"] == {"anthropic": key}
    assert stored["models"] == {"anthropic": model}
    assert os.stat(ai_file(state)).st_mode & 0o777 == 0o600
