"""Background work the lifespan runs: the reminder tick, the live stream's
stop on shutdown, and the stop-signal hook that makes that stop prompt."""

import asyncio
import signal
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import alerts as alerts_module
from app import main as main_module
from app import push_service
from app.alerts import AlertQueue
from app.api import live as live_api
from app.live import LiveBus

ALERT = {
    "key": "e1|2030-01-01T09:00|15",
    "title": "Lecture",
    "starts_at": "2030-01-01T09:00",
    "minutes_before": 15,
    "overdue": False,
}


def _tick_state(tmp_path) -> SimpleNamespace:
    """The slice of app.state alerts.reminder_tick touches."""
    return SimpleNamespace(
        alerts=AlertQueue(tmp_path),
        events=SimpleNamespace(mark_surfaced=lambda pairs, day: None),
        settings=SimpleNamespace(data_dir=tmp_path),
        timezone="UTC",
    )


def _events_due(state, now=None, consume=True):
    return [ALERT], []


def test_event_reminders_are_queued_even_when_the_task_scan_fails(tmp_path, monkeypatch):
    """Event reminders are queued before the task scan runs, so a scan failing
    after them (db locked, quarantined ledger) must not drop them, and the
    tick itself keeps going for the next source."""

    async def broken_scan(state, now=None, mark=True):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(alerts_module, "due_reminders", _events_due)
    monkeypatch.setattr(alerts_module, "task_due_reminders", broken_scan)
    state = _tick_state(tmp_path)

    asyncio.run(main_module.reminder_tick(state, set()))

    assert [a["key"] for a in state.alerts.peek()] == [ALERT["key"]]


def test_reminder_tick_does_not_wait_for_push_delivery(tmp_path, monkeypatch):
    async def no_tasks(state, now=None, mark=True):
        return [], {}

    async def slow_deliver(state, alerts):
        await asyncio.sleep(1)

    monkeypatch.setattr(alerts_module, "due_reminders", _events_due)
    monkeypatch.setattr(alerts_module, "task_due_reminders", no_tasks)
    monkeypatch.setattr(push_service, "deliver", slow_deliver)
    state = _tick_state(tmp_path)

    async def run():
        inflight: set[asyncio.Task] = set()
        started = time.monotonic()
        await main_module.reminder_tick(state, inflight)
        elapsed = time.monotonic() - started
        held = len(inflight)
        for task in list(inflight):
            task.cancel()
        await asyncio.gather(*inflight, return_exceptions=True)
        return elapsed, held, len(inflight)

    elapsed, held, left = asyncio.run(run())

    assert elapsed < 0.5
    assert held == 1  # a reference is kept while the push runs…
    assert left == 0  # …and released when it ends
    assert [a["key"] for a in state.alerts.peek()] == [ALERT["key"]]


def _request(state, disconnects):
    return SimpleNamespace(
        app=SimpleNamespace(state=state),
        is_disconnected=AsyncMock(side_effect=disconnects),
    )


def test_live_stream_ends_when_the_server_starts_shutting_down():
    """uvicorn waits for every open response before shutting down; a stream
    that ignored the stop sat there until the graceful timeout cancelled it."""

    async def run():
        bus = LiveBus()
        stopping = asyncio.Event()
        state = SimpleNamespace(live=bus, shutting_down=stopping)
        request = _request(state, lambda: False)
        response = await live_api.live_stream(request)
        frames: list[str] = []

        async def read():
            async for frame in response.body_iterator:
                frames.append(frame)

        async def next_frame(count):
            while len(frames) < count:
                await asyncio.sleep(0.005)

        reader = asyncio.create_task(read())
        await asyncio.wait_for(next_frame(1), 1)
        bus.note_changed("a.md", "r1")
        await asyncio.wait_for(next_frame(2), 1)
        stopping.set()
        await asyncio.wait_for(reader, 0.5)  # the heartbeat is 20s away
        return frames, bus.listeners

    frames, listeners = asyncio.run(run())

    assert frames[0].startswith("event: ready")
    assert frames[1].startswith("event: note") and '"a.md"' in frames[1]
    assert len(frames) == 2
    assert listeners == 0


def test_live_stream_without_a_shutdown_event_keeps_alive_until_disconnect(monkeypatch):
    """Bare FastAPI apps (the other API tests) have no shutting_down event."""
    monkeypatch.setattr(live_api, "HEARTBEAT_SECONDS", 0.01)

    async def run():
        bus = LiveBus()
        request = _request(SimpleNamespace(live=bus), [False, False, True])
        response = await live_api.live_stream(request)
        frames = [frame async for frame in response.body_iterator]
        return frames, bus.listeners

    frames, listeners = asyncio.run(asyncio.wait_for(run(), 2))

    assert frames[0].startswith("event: ready")
    assert frames[1:] == ["event: ping\ndata: {}\n\n", "event: ping\ndata: {}\n\n"]
    assert listeners == 0


def test_stop_signal_sets_shutting_down_and_still_reaches_the_server():
    received = []
    saved = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    # Stands in for uvicorn's handle_exit, installed before lifespan startup.
    signal.signal(signal.SIGTERM, lambda signum, frame: received.append(signum))
    try:

        async def run():
            stopping = asyncio.Event()
            main_module.set_on_stop_signal(stopping)
            signal.raise_signal(signal.SIGTERM)
            await asyncio.wait_for(stopping.wait(), 1)

        asyncio.run(run())
    finally:
        for sig, handler in saved.items():
            signal.signal(sig, handler)

    assert received == [signal.SIGTERM]
