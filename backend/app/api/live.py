"""SSE stream of vault changes for open editors."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

router = APIRouter(prefix="/api", tags=["live"])

# Proxies and phone radios drop a connection that says nothing. A comment line
# is ignored by EventSource but keeps the socket alive.
HEARTBEAT_SECONDS = 20.0


@router.get("/live")
async def live_stream(request: Request):
    bus = request.app.state.live
    queue = bus.subscribe()

    async def events():
        try:
            # Tell the client it is connected before anything happens, so it can
            # distinguish "connected and quiet" from "never connected".
            yield "event: ready\ndata: {}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                payload = json.dumps(event, ensure_ascii=False)
                yield f"event: {event.get('type', 'message')}\ndata: {payload}\n\n"
        finally:
            bus.unsubscribe(queue)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
