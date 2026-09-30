"""Direct OMNIA actions, confirmations, scoped commits, and Undo."""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import textwrap
from datetime import timedelta
from pathlib import Path
from typing import Any

from ..index.db import content_hash
from ..vault.internal import atomic_json, iso_utc, parse_utc, read_json, utc_now
from ..vault.notes import NoteError, NoteStore
from ..vault.repo import OVERSEER_AUTHOR
from .habits import HABITS_PATH, reload_from_mirror as reload_habits
from .housekeeping import DEFAULT_ENABLED as HOUSEKEEPING_DEFAULT
from .threads import carry_note_threads

log = logging.getLogger(__name__)

# The vault-internal settings file (tool_expansion.CONFIG_PATH).
CONFIG_PATH = f"{NoteStore.INTERNAL_DIR}/config.json"


def _is_note(rel: str) -> bool:
    """A user-visible note — not a file inside the Trash or OMNIA's records."""
    return rel.endswith(".md") and Path(rel).parts[0] != NoteStore.INTERNAL_DIR


def _reload_config_flags(state) -> None:
    """Restore the settings boot mirrors onto app.state (main.py's lifespan)
    from config.json as it now stands.

    A revert rewrites the file, not the live copies: undoing a housekeeping
    toggle left the nightly job running on the undone value until the next
    restart.
    """
    try:
        config = read_json(state.settings.vault_dir / CONFIG_PATH, {})
    except NoteError:
        # Set aside as corrupt. The undo itself landed; keep the live values.
        log.warning("config.json unreadable after an undo", exc_info=True)
        return
    if not isinstance(config, dict):
        config = {}
    state.timezone = config.get("timezone") or state.settings.timezone
    state.omnia_housekeeping = bool(
        config.get("omnia_housekeeping", HOUSEKEEPING_DEFAULT)
    )
    state.omnia_full_auto = bool(config.get("omnia_full_auto", False))
    state.omnia_autocorrect = bool(config.get("omnia_autocorrect", False))


# A confirmation card is the one gate between the model's output and a
# destructive or persistent change, and the prose around it may have been
# steered by whatever the model read. So the card says what it approves from
# the inputs themselves. Bounded, but a value too long for the summary moves
# whole into `details` rather than being cut to what nobody can judge.
SUMMARY_CHARS = 300
DETAIL_LINES = 10
DETAIL_CHARS = 200
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _literal(value: Any) -> str:
    """A find/replace string as the card shows it: line breaks and tabs spelled
    out, so they are not an invisible blank between the quotes."""
    return str(value).replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")


def _spread(label: str, text: str, room: int, *, words: bool = False) -> list[str]:
    """`text` as detail lines under `label`, at most `room` of them; the last
    one ends in an ellipsis when it does not all fit."""
    width = DETAIL_CHARS - len(label)
    if words:
        pieces = textwrap.wrap(text, width) or [""]
    else:
        pieces = [text[i : i + width] for i in range(0, len(text), width)] or [""]
    if len(pieces) > room:
        pieces = pieces[:room]
        pieces[-1] = pieces[-1][: width - 1] + "…"
    return [label + pieces[0], *pieces[1:]]


def _find_replace_card(inputs: dict[str, Any]) -> tuple[str, list[str]]:
    find, replace = _literal(inputs.get("find") or ""), _literal(inputs.get("replace") or "")
    # The executor's own default: case-sensitive unless told otherwise.
    case = "case-sensitive" if bool(inputs.get("case_sensitive", True)) else "case-insensitive"
    if len(find) + len(replace) <= DETAIL_CHARS:
        head = (
            f"Replace “{find}” with “{replace}”" if replace else f"Remove every “{find}”"
        )
        return f"{head} in every note ({case})?", []
    half = DETAIL_LINES // 2
    return f"Find and replace in every note ({case})?", [
        *_spread("Find: ", find, half),
        *_spread("Replace with: ", replace, half),
    ]


def _bulk_tasks_card(inputs: dict[str, Any]) -> tuple[str, list[str]]:
    where = []
    if inputs.get("status"):
        where.append(f"status {inputs['status']}")
    if inputs.get("text"):
        where.append(f"text contains “{inputs['text']}”")
    if inputs.get("path"):
        where.append(f"in “{inputs['path']}”")
    start, end = inputs.get("due_start"), inputs.get("due_end")
    if start and end:
        where.append(f"due {start} to {end}")
    elif start:
        where.append(f"due on or after {start}")
    elif end:
        where.append(f"due on or before {end}")
    if not inputs.get("status"):  # a named status is the whole status filter
        where.append("completed included" if inputs.get("include_completed") else "open only")
    # Every change asked for, as bulk_edit_tasks reads them: it refuses more
    # than one, but the card must not hide a second. They lead, so a long
    # filter can't push a DELETE past the end of the summary.
    changes = []
    if inputs.get("delete"):
        changes.append("DELETE")
    if inputs.get("set_status"):
        changes.append(f"set status to {inputs['set_status']}")
    if "set_due" in inputs:
        due = inputs.get("set_due")
        changes.append(f"set due {due}" if due else "clear the due date")
    if inputs.get("move_to"):
        changes.append(f"move to “{inputs['move_to']}”")
    return f"Bulk edit tasks: {' and '.join(changes) or 'no change'} ({', '.join(where)})?", []


def _merge_card(inputs: dict[str, Any]) -> tuple[str, list[str]]:
    raw = inputs.get("sources") or []
    sources = [str(p) for p in (raw if isinstance(raw, list) else [raw]) if p]
    target = inputs.get("target") or ""
    gone = {1: "it goes", 2: "both go"}.get(len(sources), f"all {len(sources)} go")
    names = ", ".join(f"“{p}”" for p in sources)
    summary = f"Merge {names} into “{target}” ({gone} to Trash)?"
    if len(summary) <= SUMMARY_CHARS:
        return summary, []
    return (
        f"Merge {len(sources)} notes into “{target}” ({gone} to Trash)?",
        _spread("", names, DETAIL_LINES, words=True),
    )


def _cadence(inputs: dict[str, Any]) -> str:
    frequency = str(inputs.get("frequency") or "").lower()
    try:
        step = int(inputs.get("interval") or 1)
    except (TypeError, ValueError):
        step = inputs.get("interval")
    if frequency == "daily":
        every = "every day" if step == 1 else f"every {step} days"
    elif frequency == "weekly":
        weekday = inputs.get("weekday")
        try:
            day = WEEKDAYS[int(weekday or 0)] if 0 <= int(weekday or 0) < 7 else None
        except (TypeError, ValueError):
            day = None
        day = day or f"weekday {weekday}"
        every = f"every {day}" if step == 1 else f"every {step} weeks on {day}"
    else:
        every = f"“{inputs.get('frequency')}”"
    return f"{every} at {inputs.get('at') or '09:00'}"


def _schedule_card(inputs: dict[str, Any]) -> tuple[str, list[str]]:
    # A schedule replays its instruction every time it fires, so the card
    # carries all of it — as the executor will store it.
    instruction = " ".join(str(inputs.get("instruction") or "").split())
    cadence = _cadence(inputs)
    if len(instruction) <= DETAIL_CHARS:
        return f"Schedule OMNIA to run {cadence}: “{instruction}”?", []
    return (
        f"Schedule OMNIA to run {cadence} with this instruction?",
        _spread("", instruction, DETAIL_LINES, words=True),
    )


def _bulk_tag_card(inputs: dict[str, Any]) -> tuple[str, list[str]]:
    tag = str(inputs.get("tag") or "").lstrip("#").strip()
    operation = str(inputs.get("operation") or "add").lower()
    folder = str(inputs.get("folder") or "").strip("/")
    has_tag = str(inputs.get("has_tag") or "").lstrip("#").strip()
    scope = []
    if folder:
        scope.append(f"under “{folder}/”")
    if has_tag:
        scope.append(f"tagged #{has_tag}")
    where = " and ".join(scope) or "(no folder or tag given)"
    if operation == "add":
        return f"Add #{tag} to every note {where}?", []
    if operation == "remove":
        return f"Remove #{tag} from every note {where}?", []
    return f"Bulk tag #{tag} “{operation}” on every note {where}?", []


def _bulk_actions_card(inputs: dict[str, Any]) -> tuple[str, list[str]]:
    operations = inputs.get("operations") or []
    lines = []
    for operation in operations:
        if not isinstance(operation, dict):
            continue
        summary, details = confirmation_card(
            str(operation.get("tool") or ""), operation.get("inputs") or {}
        )
        lines.append(": ".join([summary.removesuffix("?"), *details[:1]]))
    return f"Apply {len(operations)} OMNIA changes as separate undoable actions?", lines


def _words(value: Any) -> str:
    return " ".join(str(value or "").split())


def _quoted(
    head: str, text: str, tail: str = "", room: int = DETAIL_LINES
) -> tuple[str, list[str]]:
    """`head “text”tail?`, or the text in up to `room` detail lines when it is
    too long to quote in the summary."""
    if len(text) <= DETAIL_CHARS:
        return f"{head} “{text}”{tail}?", []
    return f"{head}{tail}?", _spread("", text, room, words=True)


# Cards for the direct calls a turn that read unvetted content has confirmed
# first (tools.taint_reason). The user is judging whether that content chose
# the words, so the card shows them.


def _remember_card(inputs: dict[str, Any]) -> tuple[str, list[str]]:
    category = _words(inputs.get("category"))
    return _quoted("Remember", _words(inputs.get("text")), f" ({category})" if category else "")


def _forget_card(inputs: dict[str, Any]) -> tuple[str, list[str]]:
    return _quoted("Forget", _words(inputs.get("text")))


def _notify_card(inputs: dict[str, Any]) -> tuple[str, list[str]]:
    summary, details = _quoted(
        "Send a notification", _words(inputs.get("title")), room=DETAIL_LINES // 2
    )
    body = _words(inputs.get("body"))
    if body:
        details += _spread("Body: ", body, DETAIL_LINES - len(details), words=True)
    return summary, details


def _reminder_card(inputs: dict[str, Any]) -> tuple[str, list[str]]:
    when = _words(inputs.get("when"))
    summary, details = _quoted(
        "Set a reminder",
        _words(inputs.get("text")),
        f" for {when}" if when else "",
        DETAIL_LINES - 3,
    )
    offsets = inputs.get("reminders")
    if isinstance(offsets, list) and offsets:
        details.append(f"Alerts: {', '.join(str(m) for m in offsets)} minutes before")
    recurrence = inputs.get("recurrence")
    if isinstance(recurrence, dict) and recurrence.get("frequency"):
        details.append(f"Repeats {_words(recurrence['frequency'])}")
    if inputs.get("note_path"):
        details.append(f"Linked to “{inputs['note_path']}”")
    return summary, details


def _followup_card(inputs: dict[str, Any]) -> tuple[str, list[str]]:
    if inputs.get("when"):
        when = f"on {_words(inputs['when'])}"
    elif inputs.get("in_days") not in (None, ""):
        when = f"in {inputs['in_days']} day(s)"
    else:
        when = "at the default time"
    return f"Remind you to follow up on “{inputs.get('path') or ''}” {when}?", []


def _task_reminder_card(inputs: dict[str, Any]) -> tuple[str, list[str]]:
    task = f"task “{inputs.get('task_id') or ''}”"
    if inputs.get("remind_at"):
        return f"Set a reminder on {task} for {_words(inputs['remind_at'])}?", []
    if inputs.get("days_before") not in (None, ""):
        at = f" at {_words(inputs['at_time'])}" if inputs.get("at_time") else ""
        return f"Set a reminder on {task} {inputs['days_before']} day(s) before it is due{at}?", []
    return f"Set a reminder on {task}?", []


def _habit_card(inputs: dict[str, Any]) -> tuple[str, list[str]]:
    key = inputs.get("key") or ""
    event = str(inputs.get("event") or "").lower()
    if event == "set":
        return _quoted(f"Set habit “{key}” to", _words(inputs.get("value")))
    if event == "delete":
        return f"Delete habit “{key}”?", []
    return f"Update habit “{key}” ({event or 'no event'})?", []


def _move_folder_card(inputs: dict[str, Any]) -> tuple[str, list[str]]:
    return f"Move folder “{inputs.get('path') or ''}” to “{inputs.get('new_path') or ''}”?", []


def _move_notes_card(inputs: dict[str, Any]) -> tuple[str, list[str]]:
    raw = inputs.get("paths") or []
    paths = [str(p) for p in (raw if isinstance(raw, list) else [raw]) if p]
    folder = str(inputs.get("folder") or "").strip("/")
    where = f"“{folder}/”" if folder else "the vault root"
    names = ", ".join(f"“{p}”" for p in paths)
    summary = f"Move {names} to {where}?"
    if len(summary) <= SUMMARY_CHARS:
        return summary, []
    return f"Move {len(paths)} notes to {where}?", _spread("", names, DETAIL_LINES, words=True)


def _schedule_runs_card(inputs: dict[str, Any]) -> tuple[str, list[str]]:
    operation = str(inputs.get("operation") or "list").lower()
    return f"{operation.capitalize()} scheduled run “{inputs.get('schedule_id') or ''}”?", []


def _written_text(inputs: dict[str, Any]) -> list[str]:
    """What a write would put in the note, as detail lines: a card for a
    direct write to the memory note (every later turn reads it) shows it."""
    for key, label in (
        ("content", "Content: "),
        ("new_content", "Content: "),
        ("text", "Text: "),
        ("new_string", "New text: "),
    ):
        text = _words(inputs.get(key))
        if text:
            return _spread(label, text, DETAIL_LINES - 1, words=True)
    edits = inputs.get("edits")
    if isinstance(edits, list):
        return [
            f"“{_words(edit.get('old_string'))}” → “{_words(edit.get('new_string'))}”"
            for edit in edits
            if isinstance(edit, dict)
        ]
    return []


def _target_card(tool: str, inputs: dict[str, Any]) -> tuple[str, list[str]]:
    # Anything that identifies the target. Without deck/tag/name here, a
    # vault-wide tag rename and a deck deletion both rendered as a bare
    # "Rename tag?" / "Delete flashcard deck?" — approved blind.
    target = next(
        (
            str(inputs[key])
            for key in (
                "path",
                "task_id",
                "entry_id",
                "event_id",
                "item_id",
                "deck",
                "name",
                "from_tag",
                "tag",
            )
            if inputs.get(key)
        ),
        "",
    )
    label = tool.replace("_", " ").strip().capitalize()
    detail = f" “{target}”" if target else ""
    # Where a move or copy lands, which may be the very note the card guards.
    destination = inputs.get("new_path") or inputs.get("target_path")
    if destination and destination != target:
        detail += f" → “{destination}”"
    if tool == "rename_tag" and inputs.get("to_tag"):
        detail = (
            f" “{inputs.get('from_tag')}” → “{inputs['to_tag']}” across every note"
        )
    return f"{label}{detail}?", _written_text(inputs)


_CARDS = {
    "bulk_actions": _bulk_actions_card,
    "bulk_edit_tasks": _bulk_tasks_card,
    "bulk_tag": _bulk_tag_card,
    "find_replace_across_vault": _find_replace_card,
    "merge_notes": _merge_card,
    "schedule_agent_run": _schedule_card,
    "remember": _remember_card,
    "forget": _forget_card,
    "notify_user": _notify_card,
    "create_reminder": _reminder_card,
    "set_note_followup": _followup_card,
    "set_task_reminder": _task_reminder_card,
    "update_habit": _habit_card,
    "move_folder": _move_folder_card,
    "move_notes": _move_notes_card,
    "manage_scheduled_runs": _schedule_runs_card,
}


def confirmation_card(tool: str, inputs: dict[str, Any]) -> tuple[str, list[str]]:
    """A confirmation card's one-line summary and the detail lines under it."""
    if not isinstance(inputs, dict):
        inputs = {}
    describe = _CARDS.get(tool)
    summary, details = describe(inputs) if describe else _target_card(tool, inputs)
    if len(details) > DETAIL_LINES:
        details = [*details[: DETAIL_LINES - 1], f"and {len(details) - DETAIL_LINES + 1} more"]
    return (
        _clip(summary, SUMMARY_CHARS),
        [_clip(line, DETAIL_CHARS) for line in details],
    )


class ActionCoordinator:
    CONFIRMATION_LIFETIME = timedelta(minutes=10)
    # What became of a card or an Undo click, held for the thread's next turn.
    OUTCOMES_PER_THREAD = 20
    OUTCOME_LIFETIME = timedelta(hours=24)

    def __init__(self, state):
        self.state = state
        self.root = (
            state.settings.vault_dir
            / NoteStore.INTERNAL_DIR
            / "omnia"
            / "actions"
        )
        self._lock = asyncio.Lock()
        self.mutation_lock = asyncio.Lock()
        # Kept in data_dir, NOT the vault: these carry tool inputs (note bodies
        # among them) and the vault is git-committed. Persisted so a restart —
        # every deploy is one — doesn't leave a live-looking Approve button that
        # 404s when clicked.
        self._confirmations_path = state.settings.data_dir / "omnia-confirmations.json"
        self._confirmations: dict[str, dict[str, Any]] = self._load_confirmations()
        # Beside it, for the same reason: an approval clicked just before a
        # deploy must still reach the model on the next turn.
        self._outcomes_path = state.settings.data_dir / "omnia-outcomes.json"
        self._outcomes: dict[str, list[dict[str, str]]] = self._load_outcomes()

    def _load_confirmations(self) -> dict[str, dict[str, Any]]:
        stored = read_json(self._confirmations_path, {})
        if not isinstance(stored, dict):
            return {}
        alive = {}
        for token, item in stored.items():
            try:
                if parse_utc(item["expires_at"]) > utc_now():
                    alive[token] = item
            except (KeyError, TypeError, ValueError):
                continue
        return alive

    def _save_confirmations(self) -> None:
        try:
            atomic_json(self._confirmations_path, self._confirmations)
        except OSError:
            # Losing persistence is survivable; the in-memory copy still works.
            log.warning("could not persist OMNIA confirmations", exc_info=True)

    def _load_outcomes(self) -> dict[str, list[dict[str, str]]]:
        try:
            stored = read_json(self._outcomes_path, {})
        except NoteError:
            # Set aside as corrupt; a lost note is survivable.
            log.warning("OMNIA outcomes unreadable", exc_info=True)
            return {}
        if not isinstance(stored, dict):
            return {}
        return {
            thread_id: [
                entry
                for entry in entries
                if isinstance(entry, dict) and isinstance(entry.get("text"), str)
            ]
            for thread_id, entries in stored.items()
            if isinstance(entries, list)
        }

    def _fresh(self, entry: dict[str, str]) -> bool:
        try:
            return parse_utc(entry["created_at"]) > utc_now() - self.OUTCOME_LIFETIME
        except (AttributeError, KeyError, TypeError, ValueError):
            return False

    def _save_outcomes(self) -> None:
        try:
            atomic_json(self._outcomes_path, self._outcomes)
        except OSError:
            log.warning("could not persist OMNIA outcomes", exc_info=True)

    def note_outcome(self, thread_id: str, text: str) -> None:
        """Queue what happened to a card or an action for the thread's next
        turn. The model only ever saw "confirmation required"; approving,
        cancelling or undoing touched the transcript, never its history."""
        # Expired entries go from every thread, so ones never visited again
        # don't pile up in the file.
        self._outcomes = {
            tid: kept
            for tid, entries in self._outcomes.items()
            if (kept := [entry for entry in entries if self._fresh(entry)])
        }
        queue = self._outcomes.setdefault(thread_id, [])
        queue.append({"text": text, "created_at": iso_utc()})
        del queue[: -self.OUTCOMES_PER_THREAD]
        self._save_outcomes()

    def drain_outcomes(self, thread_id: str) -> list[str]:
        """Take the thread's queued outcomes, oldest first, less any older
        than OUTCOME_LIFETIME."""
        queue = self._outcomes.pop(thread_id, None)
        if queue is None:
            return []
        self._save_outcomes()
        return [entry["text"] for entry in queue if self._fresh(entry)]

    def _action_path(self, action_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{24}", action_id):
            raise NoteError(400, "Invalid OMNIA action")
        return self.root / f"{action_id}.json"

    def request_confirmation(
        self,
        thread_id: str,
        tool: str,
        inputs: dict[str, Any],
        auth_session_id: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """`reason` is the one line saying why a card that is normally not
        asked for appeared (tools.taint_reason)."""
        self._purge_confirmations()
        token = secrets.token_hex(16)
        expires = utc_now() + self.CONFIRMATION_LIFETIME
        summary, details = confirmation_card(tool, inputs)
        item = {
            "token": token,
            "thread_id": thread_id,
            "auth_session_id": auth_session_id,
            "tool": tool,
            "inputs": inputs,
            "summary": summary,
            "details": details,
            "created_at": iso_utc(),
            "expires_at": iso_utc(expires),
        }
        if reason:
            item["reason"] = reason
        self._confirmations[token] = item
        self._save_confirmations()
        return {
            key: item[key]
            for key in ("token", "tool", "summary", "details", "reason", "created_at", "expires_at")
            if key in item
        }

    def consume_confirmation(
        self,
        token: str,
        thread_id: str,
        auth_session_id: str | None = None,
    ) -> dict[str, Any]:
        self._purge_confirmations()
        item = self._confirmations.get(token)
        if item is None:
            raise NoteError(404, "That confirmation expired or no longer exists")
        if item["thread_id"] != thread_id:
            raise NoteError(403, "That confirmation belongs to another OMNIA thread")
        if item.get("auth_session_id") != auth_session_id:
            raise NoteError(403, "That confirmation belongs to another signed-in device")
        self._confirmations.pop(token, None)
        self._save_confirmations()
        return item

    def cancel_confirmation(
        self,
        token: str,
        thread_id: str,
        auth_session_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Drop a card; returns it, or None when it had already gone."""
        self._purge_confirmations()
        item = self._confirmations.get(token)
        if item is None:
            return None
        if item["thread_id"] != thread_id:
            raise NoteError(403, "That confirmation belongs to another OMNIA thread")
        if item.get("auth_session_id") != auth_session_id:
            raise NoteError(403, "That confirmation belongs to another signed-in device")
        del self._confirmations[token]
        self._save_confirmations()
        return item

    def _purge_confirmations(self) -> None:
        now = utc_now()
        removed = False
        for token, item in list(self._confirmations.items()):
            try:
                expired = parse_utc(item["expires_at"]) <= now
            except (KeyError, TypeError, ValueError):
                expired = True
            if expired:
                self._confirmations.pop(token, None)
                removed = True
        if removed:
            self._save_confirmations()

    async def record(
        self,
        *,
        thread_id: str,
        tool: str,
        summary: str,
        paths: list[str],
        refresh: list[str],
        result: dict[str, Any],
        undoable: bool = True,
        navigate: str | None = None,
        close_tabs: dict | None = None,
    ) -> dict[str, Any]:
        """Create a scoped OMNIA commit and persist action metadata."""
        async with self._lock:
            sha = await self.state.vault.commit_paths(
                f"OMNIA: {summary}", paths, author=OVERSEER_AUTHOR
            )
            action_id = secrets.token_hex(12)
            action = {
                "id": action_id,
                "thread_id": thread_id,
                "tool": tool,
                "summary": summary,
                "paths": sorted(set(paths)),
                "refresh": sorted(set(refresh)),
                "commit": sha,
                "created_at": iso_utc(),
                "undoable": bool(undoable and sha),
                "undone_at": None,
                "undo_commit": None,
                # Optional in-app destination the chat should open after this
                # action — how "pull up my microecon flashcards" lands you in
                # the study screen.
                "navigate": navigate,
                # Optional directive to close open tabs ({match?, others?}).
                "close_tabs": close_tabs,
            }
            atomic_json(self._action_path(action_id), action)
            self.state.autocommit.touch()
        await self.announce_commit(sha, paths)
        return {
            **result,
            "_action": {
                key: action[key]
                for key in (
                    "id",
                    "tool",
                    "summary",
                    "paths",
                    "refresh",
                    "commit",
                    "created_at",
                    "undoable",
                    "navigate",
                    "close_tabs",
                )
            },
        }

    async def record_existing(
        self,
        *,
        thread_id: str,
        tool: str,
        summary: str,
        paths: list[str],
        refresh: list[str],
        commit: str,
        rationale: str | None = None,
    ) -> dict[str, Any]:
        """Record an action for work that already made its own commit — used by
        the review tray, whose approvals commit before this is called."""
        async with self._lock:
            action_id = secrets.token_hex(12)
            action = {
                "id": action_id,
                "thread_id": thread_id,
                "tool": tool,
                "summary": summary,
                "paths": sorted(set(paths)),
                "refresh": sorted(set(refresh)),
                "commit": commit,
                "created_at": iso_utc(),
                "undoable": True,
                "undone_at": None,
                "undo_commit": None,
                "navigate": None,
                "rationale": rationale,
            }
            atomic_json(self._action_path(action_id), action)
            self.state.autocommit.touch()
        await self.announce_commit(commit, paths)
        return action

    def pending_confirmations(self) -> list[dict[str, Any]]:
        """Confirmation cards still awaiting the user, projected to safe fields
        (never the raw tool `inputs`, which can hold note bodies)."""
        self._purge_confirmations()
        return [
            {
                **{key: item.get(key) for key in ("token", "tool", "summary", "created_at", "expires_at")},
                # Cards stored before details existed have none.
                "details": item.get("details") or [],
                **({"reason": item["reason"]} if item.get("reason") else {}),
            }
            for item in self._confirmations.values()
        ]

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        """Newest-first action history. The data was always written to disk;
        there was simply no way to read it back."""
        if not self.root.is_dir():
            return []
        actions = []
        for path in self.root.glob("*.json"):
            value = read_json(path, None, skip_corrupt=True)
            if isinstance(value, dict) and value.get("id"):
                actions.append(value)
        actions.sort(key=lambda a: str(a.get("created_at") or ""), reverse=True)
        return actions[: max(1, min(limit, 500))]

    def purge_old(self, keep: int = 500) -> int:
        """Action records accumulated forever. Keep the recent ones."""
        if not self.root.is_dir():
            return 0
        files = sorted(
            self.root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
        )
        removed = 0
        for path in files[keep:]:
            path.unlink(missing_ok=True)
            removed += 1
        return removed

    def get(self, action_id: str) -> dict[str, Any]:
        value = read_json(self._action_path(action_id), None)
        if not isinstance(value, dict):
            raise NoteError(404, "OMNIA action not found")
        return value

    async def undo(self, action_id: str, thread_id: str) -> dict[str, Any]:
        action = self.get(action_id)
        if action.get("thread_id") != thread_id:
            raise NoteError(403, "That action belongs to another OMNIA thread")
        if not action.get("undoable") or not action.get("commit"):
            raise NoteError(409, "That action cannot be undone")
        if action.get("undone_at"):
            raise NoteError(409, "That action has already been undone")

        try:
            undo_sha, skipped = await self.state.vault.revert_commit(
                action["commit"],
                f"Undo OMNIA action: {action['summary']}",
                OVERSEER_AUTHOR,
                paths=action.get("paths") or [],
            )
        except ValueError as exc:
            raise NoteError(409, str(exc)) from exc

        action["undone_at"] = iso_utc()
        action["undo_commit"] = undo_sha
        action["undoable"] = False
        atomic_json(self._action_path(action_id), action)
        paths = action.get("paths") or []
        # The revert's own diff, not the recorded paths: git revert follows
        # renames, so a note renamed since the action is reverted under its
        # new name; an undone folder move comes back as moves, an undone
        # delete as the notes reappearing.
        try:
            changes = await self.state.vault.changed_paths(undo_sha)
        except Exception:
            # The revert already landed; refresh from the recorded paths.
            log.warning("could not read undo commit %s", undo_sha, exc_info=True)
            changes = []
        await self._refresh_after_undo(paths, changes)
        if changes:
            self.announce_changes(changes)
        else:
            self.announce_paths(paths)
        self.state.autocommit.touch()
        summary = f"Undid: {action['summary']}"
        if skipped:
            # A partial undo: notes edited since the action keep those edits.
            kept = ", ".join(skipped[:3])
            if len(skipped) > 3:
                kept += f" and {len(skipped) - 3} more"
            summary += f" — kept newer edits in {kept}"
        return {
            "ok": True,
            "action_id": action_id,
            "summary": summary,
            "commit": undo_sha,
            "refresh": action.get("refresh") or [],
            "skipped": skipped,
        }

    def announce_paths(self, paths: list[str]) -> None:
        """Push OMNIA's note writes to open editors.

        Without this the agent could rewrite the note under your cursor and the
        editor would only find out on the next save — as a conflict.
        """
        bus = getattr(self.state, "live", None)
        if bus is None:
            return
        for rel in paths:
            if not rel.endswith(".md"):
                continue
            try:
                bus.note_changed(rel, content_hash(self.state.store.read(rel)))
            except NoteError:
                bus.note_removed(rel)

    async def announce_commit(
        self, sha: str | None, paths: list[str], *, within_paths: bool = False
    ) -> None:
        """Announce what a commit changed, then every recorded note it does
        not cover; with no commit (nothing to stage), the recorded paths.

        The union matters: the autosave commit can land mid-tool, while a
        multi-note tool awaits between writes, and the notes it took are
        missing from OMNIA's own commit. `within_paths` keeps only the
        commit's changes to the recorded paths, for a commit_all that swept
        unrelated user edits in with them (approve-all)."""
        if sha:
            try:
                changes = await self.state.vault.changed_paths(sha)
            except Exception:
                # The action already landed; a notification must not fail it.
                log.warning("could not read OMNIA commit %s for live", sha, exc_info=True)
            else:
                if within_paths:
                    wanted = set(paths)
                    changes = [c for c in changes if c[1] in wanted or c[2] in wanted]
                self.announce_changes(changes)
                covered = {p for _status, path, new in changes for p in (path, new) if p}
                self.announce_paths(
                    [p for p in dict.fromkeys(paths) if _is_note(p) and p not in covered]
                )
                return
        self.announce_paths(paths)

    def announce_changes(self, changes: list[tuple[str, str, str | None]]) -> None:
        """Push one commit's note changes to open editors, moves as moves.

        Read from the commit rather than the action's recorded paths: a folder
        move or delete records folder names, which name no note, so nothing
        was announced and open editors kept saving to paths that were gone.
        """
        # The notes' OMNIA conversations move with them. A client carries its
        # cached pointer only for a move it sees live; this is the binding
        # every other device (asleep, offline, elsewhere) looks up.
        carry_note_threads(
            self.state,
            [
                (path, new_path)
                for status, path, new_path in changes
                if status[:1] == "R" and new_path and _is_note(path) and _is_note(new_path)
            ],
        )
        bus = getattr(self.state, "live", None)
        if bus is None:
            return
        for status, path, new_path in changes:
            kind = status[:1]
            if kind == "R" and new_path:
                if _is_note(path) and _is_note(new_path):
                    try:
                        revision = content_hash(self.state.store.read(new_path))
                    except NoteError:
                        bus.note_removed(path)
                    else:
                        bus.note_moved(path, new_path, revision)
                        # The removal + appearance pair still follows, as the
                        # note API's renames send it: a client that predates
                        # note_moved (an installed desktop build) drops that
                        # event and would otherwise keep the note at the old
                        # path.
                        bus.note_removed(path)
                        bus.note_changed(new_path, revision)
                    continue
                # Into the Trash is a deletion; back out of it, an arrival.
                if _is_note(path):
                    bus.note_removed(path)
                if _is_note(new_path):
                    self._announce_note(bus, new_path)
                continue
            if kind == "C" and new_path:
                path = new_path
            if not _is_note(path):
                continue
            if kind == "D":
                bus.note_removed(path)
            else:
                self._announce_note(bus, path)

    def _announce_note(self, bus, rel: str) -> None:
        try:
            bus.note_changed(rel, content_hash(self.state.store.read(rel)))
        except NoteError:
            bus.note_removed(rel)

    async def _reindex(self, paths: list[str]) -> None:
        """Re-read each path after a revert; a missing one was undone away."""
        for rel in paths:
            try:
                content = self.state.store.read(rel)
            except NoteError:
                await self.state.indexer.remove_note(rel)
            else:
                await self.state.indexer.index_note(rel, content)

    async def _refresh_after_undo(
        self, paths: list[str], changes: list[tuple[str, str, str | None]]
    ) -> None:
        # Reindex exactly the notes the revert changed, both sides of a move
        # so the old row goes too. A revert that touched no note (an event, a
        # schedule, a setting, a folder's .gitkeep) leaves the index alone:
        # undoing one used to force-reindex and re-embed every note.
        touched = [p for _status, path, new in changes for p in (path, new) if p]
        notes = [p for p in (touched or paths) if _is_note(p)]
        await self._reindex(list(dict.fromkeys(notes)))
        # Whatever the refresh tags say: set_omnia_housekeeping records none.
        if CONFIG_PATH in paths or CONFIG_PATH in touched:
            _reload_config_flags(self.state)
        # The habits table follows its reverted mirror, or the next habit
        # write would mirror the undone row straight back.
        if HABITS_PATH in paths or HABITS_PATH in touched:
            await reload_habits(self.state)
