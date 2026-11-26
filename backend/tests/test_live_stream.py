"""The /api/live wire format. Clients time out a stream that stopped talking,
so the idle heartbeat has to be an event they can see — an SSE comment is
swallowed by EventSource and never reaches the page.

The stream never ends, which TestClient cannot read (it buffers the whole
body), so these drive the endpoint's body iterator directly."""
import asyncio
from types import SimpleNamespace

from app.api import live as live_api
from app.live import LiveBus


class FakeRequest:
    def __init__(self, bus):
        self.app = SimpleNamespace(state=SimpleNamespace(live=bus))

    async def is_disconnected(self):
        return False


async def read_frames(bus, count, before=None):
    response = await live_api.live_stream(FakeRequest(bus))
    assert response.media_type == "text/event-stream"
    body = response.body_iterator
    frames = []
    try:
        async for chunk in body:
            frames.append(chunk)
            if len(frames) == 1 and before:
                before()
            if len(frames) == count:
                break
    finally:
        await body.aclose()
    return frames


def test_idle_stream_sends_named_ping_after_ready(monkeypatch):
    monkeypatch.setattr(live_api, "HEARTBEAT_SECONDS", 0.05)
    bus = LiveBus()

    frames = asyncio.run(read_frames(bus, 3))

    assert frames[0] == "event: ready\ndata: {}\n\n"
    assert frames[1] == "event: ping\ndata: {}\n\n"
    assert frames[2] == "event: ping\ndata: {}\n\n"
    assert bus.listeners == 0


def test_published_change_arrives_as_its_own_event(monkeypatch):
    monkeypatch.setattr(live_api, "HEARTBEAT_SECONDS", 5.0)
    bus = LiveBus()

    frames = asyncio.run(
        read_frames(bus, 2, before=lambda: bus.note_changed("a.md", "r1", origin="c1"))
    )

    assert frames[1].startswith("event: note\ndata: ")
    assert '"path": "a.md"' in frames[1]
    assert '"origin": "c1"' in frames[1]
