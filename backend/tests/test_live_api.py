"""The /api/live SSE generator: framing, keepalives, unsubscribe on every exit,
and a revoked session losing its open stream."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.requests import ClientDisconnect

from app.api import auth as auth_module
from app.api import live as live_api
from app.api.live import live_stream
from app.live import LiveBus

READY = "event: ready\ndata: {}\n\n"
KEEPALIVE = "event: ping\ndata: {}\n\n"


def fake_request(state: SimpleNamespace, headers: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        app=SimpleNamespace(state=state),
        headers=headers or {},
        cookies={},
        is_disconnected=AsyncMock(return_value=False),
    )


def test_stream_frames_events_and_unsubscribes_on_close():
    async def run():
        bus = LiveBus()
        body = (await live_stream(fake_request(SimpleNamespace(live=bus)))).body_iterator

        assert await anext(body) == READY
        assert bus.listeners == 1

        bus.note_changed("a.md", "r1")
        frame = await anext(body)
        assert frame.startswith("event: note\ndata: ")
        assert frame.endswith("\n\n")
        assert json.loads(frame[len("event: note\ndata: ") : -2]) == {
            "type": "note",
            "path": "a.md",
            "revision": "r1",
            "origin": None,
        }

        await body.aclose()
        assert bus.listeners == 0

    asyncio.run(run())


def test_quiet_stream_sends_keepalives_and_ends_on_disconnect(monkeypatch):
    monkeypatch.setattr(live_api, "HEARTBEAT_SECONDS", 0.01)

    async def run():
        bus = LiveBus()
        request = fake_request(SimpleNamespace(live=bus))
        body = (await live_stream(request)).body_iterator

        assert await anext(body) == READY
        assert await anext(body) == KEEPALIVE

        request.is_disconnected.return_value = True
        with pytest.raises(StopAsyncIteration):
            await anext(body)
        assert bus.listeners == 0

    asyncio.run(run())


def test_client_gone_before_the_body_starts_leaves_no_subscriber():
    async def run():
        bus = LiveBus()
        response = await live_stream(fake_request(SimpleNamespace(live=bus)))
        assert bus.listeners == 0

        # Starlette sends the response start before it iterates the body; a
        # client already gone fails right there, so the body never starts.
        async def send(_message):
            raise OSError("connection reset")

        scope = {"type": "http", "asgi": {"spec_version": "2.4"}}
        with pytest.raises(ClientDisconnect):
            await response(scope, AsyncMock(), send)
        assert bus.listeners == 0

    asyncio.run(run())


def signed_in(tmp_path: Path, monkeypatch) -> tuple[auth_module.SessionAuth, str]:
    monkeypatch.setattr(auth_module, "_approximate_location", lambda _ip: "Test location")
    auth = auth_module.SessionAuth("alice", "correct horse", "test-secret", tmp_path)
    return auth, auth.create_session("203.0.113.10", "Firefox/130 Windows")


def test_revoked_session_ends_the_stream_at_the_next_heartbeat(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(live_api, "HEARTBEAT_SECONDS", 0.01)
    auth, token = signed_in(tmp_path, monkeypatch)

    async def run():
        bus = LiveBus()
        request = fake_request(
            SimpleNamespace(live=bus, auth=auth),
            headers={"authorization": f"Bearer {token}"},
        )
        body = (await live_stream(request)).body_iterator

        assert await anext(body) == READY
        assert await anext(body) == KEEPALIVE
        bus.note_changed("a.md", "r1")
        assert (await anext(body)).startswith("event: note\n")

        auth.revoke_token(token)
        with pytest.raises(StopAsyncIteration):
            await anext(body)
        assert bus.listeners == 0

    asyncio.run(run())


def test_revoked_session_is_caught_even_when_events_never_pause(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(live_api, "HEARTBEAT_SECONDS", 0.05)
    auth, token = signed_in(tmp_path, monkeypatch)

    async def run():
        bus = LiveBus()
        request = fake_request(
            SimpleNamespace(live=bus, auth=auth),
            headers={"authorization": f"Bearer {token}"},
        )
        body = (await live_stream(request)).body_iterator
        assert await anext(body) == READY

        auth.revoke_token(token)
        bus.note_changed("secret plans.md", "r2")
        await asyncio.sleep(0.1)
        # An event is waiting, so the wait never times out; the path must
        # still not reach the revoked device.
        with pytest.raises(StopAsyncIteration):
            await anext(body)
        assert bus.listeners == 0

    asyncio.run(run())


def test_no_password_means_no_session_checks(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(live_api, "HEARTBEAT_SECONDS", 0.01)
    auth = auth_module.SessionAuth(None, None, "test-secret", tmp_path)

    async def run():
        bus = LiveBus()
        body = (
            await live_stream(fake_request(SimpleNamespace(live=bus, auth=auth)))
        ).body_iterator
        assert await anext(body) == READY
        assert await anext(body) == KEEPALIVE
        assert await anext(body) == KEEPALIVE
        await body.aclose()
        assert bus.listeners == 0

    asyncio.run(run())
