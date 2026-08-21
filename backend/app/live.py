"""Change notifications, so a note open on two devices stays in step.

Editing the same note on a laptop and a desktop used to end in a conflict
copy: the second save carried a stale revision, and the server parked those
edits in a separate `… (conflict …).md` note for the user to reconcile by
hand. Broadcasting every write lets the other device merge the change while
it is still trivial to merge.

Deliberately fire-and-forget. A slow or wedged subscriber must never block a
save, so a full queue drops the event rather than applying backpressure — a
missed notification costs a stale editor until the next change, while a
blocked write would cost the edit itself.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger(__name__)

# Enough to absorb a burst (an OMNIA bulk edit touching many notes) without
# letting a disconnected reader pin the events in memory forever.
MAX_PENDING = 64


class LiveBus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=MAX_PENDING)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    @property
    def listeners(self) -> int:
        return len(self._subscribers)

    def publish(self, event: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                log.debug("live subscriber is behind; dropping %s", event.get("type"))

    def note_changed(
        self, path: str, revision: str, origin: str | None = None
    ) -> None:
        """`origin` is the client that made the change, so it can ignore its
        own echo instead of fighting its own save."""
        self.publish(
            {"type": "note", "path": path, "revision": revision, "origin": origin}
        )

    def note_removed(self, path: str, origin: str | None = None) -> None:
        self.publish({"type": "note_removed", "path": path, "origin": origin})
