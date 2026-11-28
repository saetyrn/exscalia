"""Music: the one player session every device follows (MUSIC.md section 4).

The server holds what is playing: the queue, where in it, the position,
shuffle and repeat, and which device is the speaker. Every change raises
``version``. A client changes the session by sending the whole new state with
the version it started from; a stale version is refused with the current
state, which the client adopts. Commands therefore set state and never
toggle, so a retried or crossed command cannot flip something twice.

The speaker is one tab of one browser (``<device>:<tab>``): the tab that
last pressed play. Its heartbeat carries the position every few seconds
without raising the version (nobody needs a live event for that); the
others show the position by counting on from ``position_at``. A speaker
that stops beating (closed laptop, killed tab) is treated as paused after
``STALE_SECONDS``. ``seek`` rises on an explicit jump, so the speaker can
tell a jump from the drift of its own heartbeats.

Kept in ``data/music/session.json``, outside git: it changes every few
seconds and is nobody's history. Changes are announced on the LiveBus as a
``music`` event with path ``music/session``.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from .music import MusicError
from .vault.internal import atomic_json, read_json

SESSION_PATH = "music/session"
STALE_SECONDS = 45
MAX_QUEUE = 5000
REPEATS = ("off", "all", "one")

# What a client may set. version, position_at and heartbeat_at are the
# server's.
FIELDS = (
    "queue", "original", "index", "context", "shuffle", "repeat",
    "playing", "position", "seek", "speaker", "speaker_name",
)


def _empty() -> dict:
    return {
        "version": 0,
        "queue": [],
        "original": [],
        "index": -1,
        "context": None,
        "shuffle": False,
        "repeat": "off",
        "playing": False,
        "position": 0.0,
        "position_at": 0.0,
        "seek": 0,
        "speaker": None,
        "speaker_name": None,
        "heartbeat_at": 0.0,
    }


class Session:
    def __init__(self, data_dir: Path) -> None:
        self.path = Path(data_dir) / "music" / "session.json"
        self.lock = asyncio.Lock()
        state = read_json(self.path, None, skip_corrupt=True) if self.path.exists() else None
        self.state = _empty()
        if isinstance(state, dict):
            for key in self.state:
                if key in state:
                    self.state[key] = state[key]
        # Nothing is playing across a restart: no speaker has beaten since.
        self.state["playing"] = False

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(self.path, self.state, fsync=False)

    def view(self) -> dict:
        out = dict(self.state)
        out["now"] = time.time()
        return out


def session(state) -> Session:
    current = getattr(state, "music_session", None)
    if current is None:
        current = Session(state.settings.data_dir)
        state.music_session = current
    return current


def _announce(state, sess: Session) -> None:
    state.live.publish(
        {"type": "music", "path": SESSION_PATH, "revision": sess.state["version"]}
    )


def _clean(value: dict, songs: dict) -> dict:
    """Validate a proposed state field by field."""
    out: dict[str, Any] = {}
    for key in FIELDS:
        if key not in value:
            continue
        item = value[key]
        if key in ("queue", "original"):
            if not isinstance(item, list) or len(item) > MAX_QUEUE:
                raise MusicError(400, f"{key} must be a list of songs")
            out[key] = [s for s in item if isinstance(s, str) and s in songs]
        elif key == "index":
            out[key] = int(item) if isinstance(item, (int, float)) else -1
        elif key in ("shuffle", "playing"):
            out[key] = bool(item)
        elif key == "repeat":
            if item not in REPEATS:
                raise MusicError(400, "repeat is off, all or one")
            out[key] = item
        elif key == "position":
            out[key] = max(0.0, float(item or 0))
        elif key == "seek":
            out[key] = int(item or 0)
        else:  # context, speaker, speaker_name
            out[key] = None if item in (None, "") else str(item)[:200]
    return out


def _fix_index(st: dict, proposed: dict) -> None:
    """Songs that are gone were dropped from the queue; keep the index on the
    song it pointed at."""
    raw = proposed.get("queue")
    if raw is None:
        return
    wanted = raw[proposed["index"]] if 0 <= proposed.get("index", -1) < len(raw) else None
    st["index"] = st["queue"].index(wanted) if wanted in st["queue"] else min(st["index"], len(st["queue"]) - 1)


def expire(state) -> bool:
    """A speaker that stopped beating is paused. True when that changed."""
    sess = session(state)
    st = sess.state
    if st["playing"] and time.time() - st["heartbeat_at"] > STALE_SECONDS:
        st["position"] = _position_now(st)
        st["playing"] = False
        st["position_at"] = time.time()
        st["version"] += 1
        sess.save()
        _announce(state, sess)
        return True
    return False


def _position_now(st: dict) -> float:
    if not st["playing"]:
        return st["position"]
    elapsed = max(0.0, min(time.time(), st["heartbeat_at"] + STALE_SECONDS) - st["position_at"])
    return st["position"] + elapsed


def get(state) -> dict:
    expire(state)
    return session(state).view()


async def update(state, base: int | None, proposed: dict, songs: dict) -> dict:
    """Apply a new state if ``base`` is the current version (None skips the
    check: the server's own changes, OMNIA in Deploy 5)."""
    sess = session(state)
    async with sess.lock:
        st = sess.state
        if base is not None and base != st["version"]:
            raise StaleSession(sess.view())
        raw_queue = proposed.get("queue")
        clean = _clean(proposed, songs)
        st.update(clean)
        if raw_queue is not None:
            _fix_index(st, {"queue": raw_queue, "index": proposed.get("index", st["index"])})
        if not st["queue"]:
            st["index"] = -1
            st["playing"] = False
        st["index"] = max(-1, min(st["index"], len(st["queue"]) - 1))
        now = time.time()
        if "position" in clean or "playing" in clean:
            st["position_at"] = now
        if st["playing"]:
            st["heartbeat_at"] = now
        st["version"] += 1
        sess.save()
        _announce(state, sess)
        return sess.view()


async def heartbeat(state, speaker: str, position: float, playing: bool, final: bool = False) -> dict:
    """The speaker reporting its position. Quiet (no version, no event)
    unless it says playback stopped, which every device must see."""
    sess = session(state)
    async with sess.lock:
        st = sess.state
        if st["speaker"] != speaker:
            return sess.view()
        now = time.time()
        st["position"] = max(0.0, float(position or 0))
        st["position_at"] = now
        st["heartbeat_at"] = now
        if (final or not playing) and st["playing"]:
            st["playing"] = False
            st["version"] += 1
            sess.save()
            _announce(state, sess)
        else:
            sess.save()
        return sess.view()


class StaleSession(MusicError):
    def __init__(self, current: dict) -> None:
        super().__init__(409, "The player changed meanwhile")
        self.current = current
