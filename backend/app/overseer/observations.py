"""Staged observations: what a background pass noticed, waiting to be asked.

A proactive pass runs on read and proposal tools only, so until now it could
notice a pattern and had nowhere to put it — the morning check could tell the
user something, but nothing it learned survived the turn. Writing habits or
memory from an unattended pass is the other extreme: OMNIA would be editing its
model of the user with nobody in the room.

This is the middle. `note_observation` stages ONE suggestion; the morning
briefing surfaces it; the user confirms it (and only then does it become a habit
or a memory entry) or dismisses it. The limits are hard rules in code, not
prompt wording, because a prompt is a request and this must be a guarantee:

* at most one pending observation per day,
* pending entries expire after a week rather than queueing up,
* a habit key the user has dismissed is not raised again for three months.

The file lives in the vault, so the tombstones and the pending queue are
git-backed like everything else OMNIA remembers.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from ..vault.internal import atomic_json, iso_utc, parse_utc, read_json, utc_now
from ..vault.notes import NoteError, NoteStore
from . import habits

OBSERVATIONS_PATH = f"{NoteStore.INTERNAL_DIR}/omnia/observations.json"
VERSION = 1

KINDS = ("habit", "memory")
MAX_TEXT = 300
EXPIRE_DAYS = 7
DISMISS_BLOCK_DAYS = 90
# Settled entries are kept as a short record of what was asked and answered.
KEEP_SETTLED = 50


def path(state) -> Path:
    return state.settings.vault_dir / OBSERVATIONS_PATH


def load(state) -> dict:
    try:
        value = read_json(path(state), None)
    except NoteError:
        # read_json set the unreadable file aside already. This is a suggestion
        # queue, so start a fresh one rather than failing the pass that writes.
        value = None
    if not isinstance(value, dict):
        value = {}
    return {
        "version": VERSION,
        "observations": [
            item for item in value.get("observations") or [] if isinstance(item, dict)
        ],
        "dismissed": [
            item for item in value.get("dismissed") or [] if isinstance(item, dict)
        ],
    }


def save(state, data: dict) -> None:
    atomic_json(path(state), data)


def _age_days(value, now=None) -> float | None:
    try:
        return ((now or utc_now()) - parse_utc(str(value))).total_seconds() / 86400
    except (TypeError, ValueError):
        return None


def prune(data: dict, now=None) -> dict:
    """Expire stale pending entries and cap the settled history.

    An observation nobody answered within a week is not worth asking about any
    more; leaving it pending would mean a question from another month surfacing
    as if it were today's.
    """
    kept = []
    settled = []
    for item in data["observations"]:
        if item.get("status") == "pending":
            age = _age_days(item.get("created_at"), now)
            if age is not None and age > EXPIRE_DAYS:
                continue
            kept.append(item)
        else:
            settled.append(item)
    data["observations"] = kept + settled[-KEEP_SETTLED:]
    return data


def dismissed_recently(data: dict, habit_key: str, now=None) -> bool:
    if not habit_key:
        return False
    for item in data["dismissed"]:
        if item.get("habit_key") != habit_key:
            continue
        age = _age_days(item.get("dismissed_at"), now)
        if age is None or age <= DISMISS_BLOCK_DAYS:
            return True
    return False


def pending_today(data: dict, today: str) -> dict | None:
    """The pending observation staged today, if there is one."""
    for item in data["observations"]:
        if item.get("status") == "pending" and str(item.get("created_at", ""))[:10] == today:
            return item
    return None


def surfaced(data: dict) -> dict | None:
    """The one pending observation to show the user: the newest.

    At most one can be staged per day and they expire in a week, so this is
    normally today's; when the user has left older ones unanswered, the newest
    is the one still worth asking about.
    """
    pending = [item for item in data["observations"] if item.get("status") == "pending"]
    if not pending:
        return None
    return sorted(pending, key=lambda item: str(item.get("created_at") or ""))[-1]


def public(item: dict | None) -> dict | None:
    """The exact shape the morning response carries. `habit_value` rides
    along because confirming stores it, not the sentence: a card that showed
    only the text could be approving something else entirely."""
    if not item:
        return None
    return {
        "id": item.get("id"),
        "text": item.get("text"),
        "kind": item.get("kind"),
        "habit_key": item.get("habit_key"),
        "habit_value": item.get("habit_value"),
        "created_at": item.get("created_at"),
    }


def record(
    state,
    *,
    text: str,
    kind: str,
    habit_key: str | None = None,
    habit_value: str | None = None,
    now=None,
) -> tuple[dict | None, str]:
    """Stage one observation. Returns (entry, note); entry is None when a rule
    refused it — refusals are a note back to the model, never an error, because
    hitting the daily cap is the system working."""
    text = " ".join(str(text or "").split())[:MAX_TEXT]
    if not text:
        raise NoteError(400, "Provide the observation text")
    if kind not in KINDS:
        raise NoteError(400, f"kind must be one of: {', '.join(KINDS)}")
    # Held to update_habit's limits here, where the model can still fix them:
    # confirming stores these as they are, and a pair it refused would take
    # the user's yes and store nothing.
    habit_value = " ".join(str(habit_value or "").split())[: habits.VALUE_CHARS].rstrip()
    if habit_key and not habits.valid_key(habit_key):
        raise NoteError(
            400,
            f"habit_key must be one dotted token of at most {habits.KEY_CHARS} "
            "characters, like 'daily_note.logs_hours'",
        )

    now = now or utc_now()
    data = prune(load(state), now)
    today = iso_utc(now)[:10]

    if pending_today(data, today) is not None:
        return None, (
            "An observation is already waiting for the user today — at most one "
            "is surfaced per day. Keep this one for another time."
        )
    if habit_key and dismissed_recently(data, habit_key, now):
        return None, (
            f"The user declined '{habit_key}' within the last "
            f"{DISMISS_BLOCK_DAYS} days. Do not raise it again."
        )

    entry = {
        "id": uuid.uuid4().hex[:12],
        "text": text,
        "kind": kind,
        "habit_key": habit_key or None,
        "habit_value": habit_value or None,
        "created_at": iso_utc(now),
        "status": "pending",
    }
    data["observations"].append(entry)
    save(state, data)
    return entry, "Staged for the user to confirm or dismiss."


def _find(data: dict, observation_id: str) -> dict:
    for item in data["observations"]:
        if item.get("id") == observation_id and item.get("status") == "pending":
            return item
    raise NoteError(404, "That observation is no longer waiting")


def get_pending(state, observation_id: str, now=None) -> dict:
    """The pending observation with this id, or a 404. Separate from `settle`
    so a confirmation can apply its effect BEFORE the entry is consumed — a
    failure there would otherwise swallow the user's answer."""
    return _find(prune(load(state), now), observation_id)


def settle(state, observation_id: str, status: str, now=None) -> dict:
    """Mark a pending observation confirmed or dismissed.

    Dismissing a habit observation also leaves a tombstone, which is what stops
    the same suggestion coming back every week.
    """
    now = now or utc_now()
    data = prune(load(state), now)
    item = _find(data, observation_id)
    item["status"] = status
    item["settled_at"] = iso_utc(now)
    if status == "dismissed" and item.get("habit_key"):
        data["dismissed"] = [
            row for row in data["dismissed"] if row.get("habit_key") != item["habit_key"]
        ] + [{"habit_key": item["habit_key"], "dismissed_at": iso_utc(now)}]
    save(state, data)
    return item
