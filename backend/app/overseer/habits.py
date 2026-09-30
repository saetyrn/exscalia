"""The habit store's durable half, plus how a habit is ranked as it ages.

The habits table lives in the SQLite index, which the app documents as a
rebuildable cache over the vault — except nothing could rebuild it. The answers
OMNIA learned by asking the user exist in no note and no markdown file, so a
lost index silently lost the whole learning layer, and neither git history nor
the off-site backup held a copy.

Every write is therefore mirrored to a small JSON file inside `.exscalia`, which
is version-controlled, autocommitted and backed up like everything else there.
An empty table on boot restores from it, which makes the docstring true.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..vault.internal import atomic_json, iso_utc, read_json, utc_now
from ..vault.notes import NoteError, NoteStore

log = logging.getLogger(__name__)

HABITS_PATH = f"{NoteStore.INTERNAL_DIR}/omnia/habits.json"
VERSION = 1
COLUMNS = ("key", "value", "confidence", "source", "last_seen", "times_confirmed")

# Ranking. A fact recorded once and never seen again should not outrank one
# confirmed last week forever, but it should not vanish either — so confidence
# decays toward a floor rather than to zero.
HALF_LIFE_DAYS = 180
RECENCY_FLOOR = 0.2

# Nightly prune. Only ever both: barely believed AND untouched for a year.
PRUNE_CONFIDENCE = 0.1
PRUNE_AFTER_DAYS = 365

# What one habit may hold. The top forty ride verbatim in the system prompt of
# every later turn, so a key is one short token and a value one short line —
# prose belongs in the memory note, whose entries are capped too.
KEY_CHARS = 80
VALUE_CHARS = 200


def valid_key(key: str) -> bool:
    return 0 < len(key) <= KEY_CHARS and not any(ch.isspace() for ch in key)


def valid_value(value: str) -> bool:
    return len(value) <= VALUE_CHARS and len(value.splitlines()) <= 1


def _parse(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def recency_factor(last_seen, now: datetime | None = None) -> float:
    """Halves every HALF_LIFE_DAYS, floored. An unparseable or missing
    `last_seen` is treated as ancient — it is what a pre-dating row looks
    like — but the floor keeps it in the running."""
    seen = _parse(last_seen)
    if seen is None:
        return RECENCY_FLOOR
    days = max(0.0, ((now or utc_now()) - seen).total_seconds() / 86400)
    return max(RECENCY_FLOOR, 0.5 ** (days / HALF_LIFE_DAYS))


def habit_rank(confidence, last_seen, now: datetime | None = None) -> float:
    try:
        value = float(confidence or 0)
    except (TypeError, ValueError):
        value = 0.0
    return value * recency_factor(last_seen, now)


def mirror_path(state) -> Path:
    return state.settings.vault_dir / HABITS_PATH


async def mirror(state) -> None:
    """Write the whole table out, keyed by habit key.

    A full dump rather than a delta: the table is tens of rows, and a dump is
    the only shape that cannot drift from the database it mirrors. Never
    raises — losing the mirror must not fail the habit write it follows.
    """
    try:
        rows = await state.db.execute_fetchall(
            f"SELECT {', '.join(COLUMNS)} FROM habits"
        )
        atomic_json(
            mirror_path(state),
            {
                "version": VERSION,
                "habits": {
                    str(row["key"]): {
                        key: row[key] for key in COLUMNS if key != "key"
                    }
                    for row in rows
                },
            },
        )
    except Exception:
        log.warning("habit mirror write failed", exc_info=True)


def stored(state) -> dict[str, dict]:
    try:
        value = read_json(mirror_path(state), None)
    except NoteError:
        # read_json has already set the unreadable file aside; a mirror is a
        # backup, so an unreadable one means "nothing to restore", never a boot
        # failure.
        return {}
    if not isinstance(value, dict) or not isinstance(value.get("habits"), dict):
        return {}
    return {k: v for k, v in value["habits"].items() if isinstance(v, dict)}


async def restore_if_empty(state) -> int:
    """Refill an empty habits table from the mirror. Returns rows restored.

    Only when the table is empty: a table with rows in it is the live one, and
    an older mirror must never overwrite it.
    """
    rows = await state.db.execute_fetchall("SELECT 1 FROM habits LIMIT 1")
    if rows:
        return 0
    saved = stored(state)
    if not saved:
        return 0
    return await _insert(state, saved)


async def reload_from_mirror(state) -> int:
    """Make the table match the mirror as it now stands. Returns rows loaded.

    For an Undo: reverting a habit write rolls back habits.json with the rest
    of the commit, and the table has to follow the file — left alone, the next
    habit write would mirror the undone row straight back. A missing file is
    an empty store (undoing the first habit ever set removes it); an
    unreadable one keeps the table as it is.
    """
    try:
        value = read_json(mirror_path(state), {"habits": {}})
    except NoteError:
        log.warning("habit mirror unreadable after an undo", exc_info=True)
        return 0
    if not isinstance(value, dict) or not isinstance(value.get("habits"), dict):
        log.warning("habit mirror has no habits table; keeping the live one")
        return 0
    await state.db.execute("DELETE FROM habits")
    return await _insert(
        state, {k: v for k, v in value["habits"].items() if isinstance(v, dict)}
    )


async def _insert(state, saved: dict[str, dict]) -> int:
    restored = 0
    for key, row in saved.items():
        try:
            await state.db.execute(
                "INSERT OR REPLACE INTO habits"
                "(key, value, confidence, source, last_seen, times_confirmed)"
                " VALUES (?,?,?,?,?,?)",
                (
                    key,
                    str(row.get("value") or ""),
                    float(row.get("confidence") or 0.0),
                    row.get("source"),
                    row.get("last_seen"),
                    int(row.get("times_confirmed") or 0),
                ),
            )
            restored += 1
        except Exception:
            log.warning("could not restore habit %s", key, exc_info=True)
    await state.db.commit()
    return restored


async def prune(state, now: datetime | None = None) -> int:
    """Drop habits that are both barely believed and long unseen."""
    cutoff = iso_utc((now or utc_now()) - timedelta(days=PRUNE_AFTER_DAYS))
    cur = await state.db.execute(
        "DELETE FROM habits WHERE confidence < ? AND last_seen IS NOT NULL"
        " AND last_seen < ?",
        (PRUNE_CONFIDENCE, cutoff),
    )
    await state.db.commit()
    removed = int(cur.rowcount or 0)
    if removed:
        await mirror(state)
    return removed
