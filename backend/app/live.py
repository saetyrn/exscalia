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

# Enough to absorb a burst (an OMNIA bulk edit touching many notes, a folder
# rename sending three events per note) without letting a disconnected reader
# pin the events in memory forever. At 64 a rename of 22 notes already lost
# the tail, and an editor open on one kept saving to its old path.
MAX_PENDING = 1024


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

    def paper_changed(
        self, path: str, revision: int, origin: str | None = None
    ) -> None:
        """A Paper page or notebook changed. ``path`` is the vault-relative
        page directory (or notebook package), the key every Paper surface uses;
        ``revision`` is the page's server revision so a listener can tell an
        echo of its own save from someone else's edit."""
        self.publish(
            {"type": "paper", "path": path, "revision": revision, "origin": origin}
        )

    def note_removed(self, path: str, origin: str | None = None) -> None:
        self.publish({"type": "note_removed", "path": path, "origin": origin})

    def note_moved(
        self,
        old_path: str,
        new_path: str,
        revision: str,
        origin: str | None = None,
    ) -> None:
        """A rename or move, as one event. Sent ahead of the note_removed(old)
        + note(new) pair that older clients still rely on: read as that pair
        alone, an open editor took the rename for a deletion and went home
        while its next autosave landed on a path that no longer existed."""
        self.publish(
            {
                "type": "note_moved",
                "path": old_path,
                "new_path": new_path,
                "revision": revision,
                "origin": origin,
            }
        )
