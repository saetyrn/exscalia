"""Music: what was played, when, where and for how long.

One row per play, reported by the speaker when a song stops being the one
playing (it finished, was skipped, or the tab went away). It feeds Recent >
Played (Deploy 4), OMNIA's "what did I play most" (Deploy 5) and the cache's
ranking (Deploy 6, MUSIC.md section 6), which counts a listen as 30 seconds
spent on a song in one play.

Kept in ``data/music/history.sqlite3``, outside git: a row per play would
be a commit per song. Times are UTC; anything that buckets by day converts
with ``tz.py``.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

ENDINGS = ("finished", "skipped", "stopped")
SCHEMA = """
CREATE TABLE IF NOT EXISTS plays (
    id INTEGER PRIMARY KEY,
    song_id TEXT NOT NULL,
    device TEXT NOT NULL,
    started_at TEXT NOT NULL,
    seconds REAL NOT NULL,
    duration REAL,
    ended TEXT NOT NULL,
    repeat INTEGER NOT NULL DEFAULT 0,
    context TEXT
);
CREATE INDEX IF NOT EXISTS plays_song ON plays (song_id, started_at);
CREATE INDEX IF NOT EXISTS plays_started ON plays (started_at);
"""


class History:
    def __init__(self, data_dir: Path) -> None:
        self.path = Path(data_dir) / "music" / "history.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.executescript(SCHEMA)
        self._db.commit()

    def record(
        self,
        song_id: str,
        device: str,
        seconds: float,
        duration: float | None,
        ended: str,
        repeat: bool,
        context: str | None,
        started_at: datetime | None = None,
    ) -> None:
        if ended not in ENDINGS:
            ended = "stopped"
        started = (started_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        with self._lock:
            self._db.execute(
                "INSERT INTO plays (song_id, device, started_at, seconds, duration, ended, repeat, context)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    song_id, device[:120], started.isoformat(timespec="seconds"),
                    max(0.0, min(float(seconds), 86400.0)), duration, ended, int(bool(repeat)), context,
                ),
            )
            self._db.commit()

    def recent(self, limit: int = 100) -> list[dict]:
        """Songs by their latest play, newest first."""
        with self._lock:
            rows = self._db.execute(
                "SELECT song_id, MAX(started_at) AS at, COUNT(*) FROM plays"
                " GROUP BY song_id ORDER BY at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [{"song": r[0], "at": r[1], "plays": r[2]} for r in rows]

    def plays(self, since: str | None = None, song_id: str | None = None) -> list[dict]:
        query = "SELECT song_id, device, started_at, seconds, duration, ended, repeat, context FROM plays"
        where, args = [], []
        if since:
            where.append("started_at >= ?")
            args.append(since)
        if song_id:
            where.append("song_id = ?")
            args.append(song_id)
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY started_at"
        with self._lock:
            rows = self._db.execute(query, args).fetchall()
        keys = ("song", "device", "started_at", "seconds", "duration", "ended", "repeat", "context")
        return [dict(zip(keys, r)) for r in rows]

    def forget(self, song_ids: list[str]) -> None:
        """A song deleted for good takes its history with it."""
        with self._lock:
            self._db.executemany("DELETE FROM plays WHERE song_id = ?", [(s,) for s in song_ids])
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()


def history(state) -> History:
    current = getattr(state, "music_history", None)
    if current is None:
        current = History(state.settings.data_dir)
        state.music_history = current
    return current
