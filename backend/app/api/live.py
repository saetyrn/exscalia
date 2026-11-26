"""SSE stream of vault changes for open editors."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

router = APIRouter(prefix="/api", tags=["live"])

# Proxies and phone radios drop a connection that says nothing, and a socket
# left half-open by a sleeping laptop looks exactly like a quiet one. The
# heartbeat is a named event (not an SSE comment, which EventSource never
# surfaces) so clients can time out a stream that stopped talking. Clients
# that predate it ignore an event name they do not listen for.
HEARTBEAT_SECONDS = 20.0


@router.get("/live")
async def live_stream(request: Request):
    bus = request.app.state.live
    # The server waits for every open response before it shuts down, and this
    # one never ends on its own; the lifespan sets this on the stop signal.
    # Bare apps (tests) have none and wait on an event that never fires.
    stopping = getattr(request.app.state, "shutting_down", None) or asyncio.Event()
    # The middleware checked the session once, at connect. Revoking it (or
    # changing the password) must also end a stream that is already open,
    # so the token is re-checked about once a heartbeat.
    auth = getattr(request.app.state, "auth", None)
    if auth is not None and not auth.required:
        auth = None
    token = auth.token_from_request(request) if auth is not None else None

    async def events():
        loop = asyncio.get_running_loop()
        recheck_at = loop.time() + HEARTBEAT_SECONDS
        stop = asyncio.ensure_future(stopping.wait())
        try:
            # Subscribed here, not in the handler: a client gone before the
            # body starts never runs this generator, so its finally would
            # never unsubscribe.
            queue = bus.subscribe()
            # Tell the client it is connected before anything happens, so it can
            # distinguish "connected and quiet" from "never connected".
            yield "event: ready\ndata: {}\n\n"
            while not stop.done():
                if await request.is_disconnected():
                    break
                getter = asyncio.ensure_future(queue.get())
                try:
                    await asyncio.wait(
                        {getter, stop},
                        timeout=HEARTBEAT_SECONDS,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    getter.cancel()  # a no-op once it has an event
                timed_out = not getter.done() and not stop.done()
                # A busy vault never times out, so the check also runs on a clock.
                if auth is not None and (timed_out or loop.time() >= recheck_at):
                    recheck_at = loop.time() + HEARTBEAT_SECONDS
                    if auth.session_for_token(token, touch=False) is None:
                        break
                if getter.done():
                    event = getter.result()
                    payload = json.dumps(event, ensure_ascii=False)
                    yield f"event: {event.get('type', 'message')}\ndata: {payload}\n\n"
                elif not stop.done():
                    yield "event: ping\ndata: {}\n\n"
        finally:
            stop.cancel()
            bus.unsubscribe(queue)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
