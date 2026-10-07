"""OMNIA's "current moment" awareness: the user's selection, view, and open
tabs reach the system prompt so "rewrite this" / "what's on here?" resolve."""
import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.overseer.agent import Overseer
from app.overseer.provider import FakeProvider, ProviderReply


def _run(state, **kwargs):
    provider = FakeProvider([ProviderReply(text="ok", stop_reason="end_turn")])
    overseer = Overseer(provider, state)
    asyncio.run(overseer.chat("do the thing", **kwargs))
    return provider.systems[-1]


def test_selection_view_and_tabs_reach_the_prompt(state):
    system = _run(
        state,
        ui_context={
            "selection": "The quick brown fox jumps.",
            "view": "Calendar",
            "open_tabs": ["Notes", "Calendar", "Media"],
        },
    )
    assert "The quick brown fox jumps." in system
    assert "selected in the editor" in system
    assert "Calendar" in system
    assert "Notes, Calendar, Media" in system


def test_long_selection_is_capped(state):
    system = _run(state, ui_context={"selection": "x" * 5000})
    # Capped at 2000 chars in the prompt block.
    assert "x" * 2000 in system
    assert "x" * 2001 not in system


def test_no_ui_context_adds_no_awareness_block(state):
    system = _run(state)
    assert "selected in the editor" not in system
    assert "currently looking at" not in system


# --- the non-streaming route ------------------------------------------------


class CapturingOverseer:
    """Stands in for the real overseer, recording what the route handed it."""

    def __init__(self):
        self.sessions: dict = {}
        self.kwargs: list[dict] = []

    async def chat(self, message, session_id=None, **kwargs):
        self.kwargs.append(kwargs)
        return {
            "session_id": session_id,
            "reply": "ok",
            "usage": {"input": 9000, "cache_read": 8500},
        }


def _client(tmp_path: Path, monkeypatch) -> TestClient:
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


def test_the_non_streaming_chat_route_forwards_the_current_moment(tmp_path, monkeypatch):
    """The SSE route passed ui_context and selection_range; POST /overseer/chat
    silently dropped both, so any client on the non-streaming fallback asked
    "rewrite this" and OMNIA had no idea what "this" was."""
    with _client(tmp_path, monkeypatch) as client:
        client.app.state.overseer = CapturingOverseer()
        response = client.post(
            "/api/overseer/chat",
            json={
                "message": "tighten this",
                "context_note": "Ideas.md",
                "selection": "The quick brown fox.",
                "view": "Notes",
                "open_tabs": ["Ideas.md", "Calendar"],
                "route": "/note/Ideas.md",
                "selection_range": {"from": 4, "to": 24, "path": "Ideas.md"},
            },
        )

        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["reply"] == "ok"

        passed = client.app.state.overseer.kwargs[-1]
        assert passed["context_note"] == "Ideas.md"
        assert passed["ui_context"] == {
            "selection": "The quick brown fox.",
            "view": "Notes",
            "open_tabs": ["Ideas.md", "Calendar"],
            "route": "/note/Ideas.md",
        }
        # selection_range rides on the persisted user message, not the prompt.
        assert payload["user_message"]["selection_range"] == {
            "from": 4,
            "to": 24,
            "path": "Ideas.md",
        }


def test_the_non_streaming_route_still_works_without_any_of_it(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        client.app.state.overseer = CapturingOverseer()
        response = client.post("/api/overseer/chat", json={"message": "hi"})

        assert response.status_code == 200, response.text
        assert client.app.state.overseer.kwargs[-1]["ui_context"] is None


def test_the_thread_endpoint_returns_per_message_usage(tmp_path, monkeypatch):
    """The token counts are recorded server-side; the client renders them as a
    tooltip, so they have to survive into the thread JSON."""
    with _client(tmp_path, monkeypatch) as client:
        client.app.state.overseer = CapturingOverseer()
        thread_id = client.post("/api/overseer/chat", json={"message": "hi"}).json()[
            "session_id"
        ]

        messages = client.get(f"/api/omnia/threads/{thread_id}").json()["messages"]

        assert all("usage" in message for message in messages)
        assert messages[-1]["usage"] == {"input": 9000, "cache_read": 8500}
