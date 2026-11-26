"""Persistent Git-backed OMNIA conversation threads."""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from pathlib import Path

from ..vault.internal import atomic_json, iso_utc, read_json
from ..vault.notes import NoteError, NoteStore
from .history import normalize, repair_history, user_turn_starts
from .provider import without_context_block

log = logging.getLogger(__name__)

# Ceiling for a single conversation's provider history. Past this, the oldest
# exchanges are dropped rather than letting the provider reject the request.
MAX_HISTORY_CHARS = 400_000


def _summary_fresh(summary: dict, thread: dict, message_count: int) -> bool:
    """Whether a stored summary still describes the transcript.

    Summaries written before the count was recorded fall back to the original
    `updated_at` equality rather than being discarded wholesale.
    """
    if "message_count" in summary:
        return summary.get("message_count") == message_count
    return summary.get("source_updated_at") == thread.get("updated_at")


def _text_pieces(turn: dict) -> list[str]:
    content = turn.get("content")
    if isinstance(content, str):
        return [content]
    # A trailing turn context is prompt, not typed text: counted, it made an
    # attachments-only message look like one with words.
    return [
        str(block.get("text") or "")
        for block in without_context_block(content or [])
        if isinstance(block, dict) and block.get("type") == "text"
    ]


def _carries(turn: dict, text: str) -> bool:
    """Whether a provider user turn is the one a public message produced.

    The provider copy can carry more than the typed text: an attachment's
    extracted text in a block of its own, a [system] note added before or after
    it. So a text piece that starts or ends with the public text counts. A bare
    substring does not — a short "ok" would pair with a scheduled prompt that
    happens to say "look". A message with no text pairs with a turn that has
    none (its attachments only).
    """
    pieces = _text_pieces(turn)
    if not text.strip():
        return not any(piece.strip() for piece in pieces)
    return any(piece.startswith(text) or piece.endswith(text) for piece in pieces)


def _first_removed_turn(history: list[dict], starts: list[int], texts: list[str]) -> int | None:
    """Where the provider turn of the first removed public message starts.

    Pairs the removed public messages, last first, with provider user turns
    walking back from the end. Turns with no public counterpart — a scheduled
    run's prompt, the morning check — are stepped over, and a repeated
    "continue" pairs with its own turn rather than the latest one. None when
    the first removed message has no turn to pair with.
    """
    candidates = list(starts)
    found = None
    for text in reversed(texts):
        found = None
        for position in range(len(candidates) - 1, -1, -1):
            if _carries(history[candidates[position]], text):
                found = candidates[position]
                del candidates[position:]
                break
    return found


class ThreadStore:
    """Transcripts in the vault; raw provider history in a sidecar beside it.

    They used to share one file, which cost twice over. The visible transcript
    is small and worth versioning, but `provider_history` — tool results up to
    30k chars, base64 thinking signatures — was 65–72% of every thread file, so
    the git-backed vault meant to hold the user's notes was mostly chat
    machinery, and *every* streamed token rewrote all of it.

    Splitting them means a vault restore recovers the readable conversation but
    not the raw tool-call history. That is the intended trade: the history is a
    provider implementation detail, not the user's writing.
    """

    def __init__(self, vault_dir: Path, data_dir: Path | None = None, index=None):
        self.root = vault_dir / NoteStore.INTERNAL_DIR / "omnia" / "threads"
        # Beside the threads, not among them: list() reads every *.json there.
        self.note_threads_path = (
            vault_dir / NoteStore.INTERNAL_DIR / "omnia" / "note-threads.json"
        )
        # Outside the vault, so it is neither committed nor pushed off-site.
        self.history_root = (data_dir or vault_dir) / "omnia" / "history"
        # The conversation search cache (index.thread_index.ThreadIndex), wired
        # in by main.py. Every write to a transcript goes through this class, so
        # this is the one choke point that keeps the cache honest — background
        # flows (the scheduler, the morning check) land here too.
        self.index = index
        self._lock = asyncio.Lock()
        self._generation_locks: dict[str, asyncio.Lock] = {}
        # In-flight generations, so Stop works server-side rather than relying
        # on the originating client aborting its own fetch. A list per thread:
        # a request is registered before it takes the generation lock, so a
        # second tab's queued turn sits here beside the one generating.
        self._runs: dict[str, list[asyncio.Task]] = {}

    @staticmethod
    def _valid(thread_id: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]{4,80}", thread_id):
            raise NoteError(400, "Invalid OMNIA thread")
        return thread_id

    def _path(self, thread_id: str) -> Path:
        return self.root / f"{self._valid(thread_id)}.json"

    def _history_path(self, thread_id: str) -> Path:
        return self.history_root / f"{self._valid(thread_id)}.json"

    def _read(self, thread_id: str) -> dict:
        value = read_json(self._path(thread_id), None)
        if not isinstance(value, dict):
            raise NoteError(404, "OMNIA thread not found")
        value.setdefault("messages", [])
        # Threads written before the split still carry history inline. Move it
        # out on first read rather than dropping it, and strip the inline copy
        # so the vault stops versioning it from here on.
        if "provider_history" in value:
            legacy = value.pop("provider_history") or []
            if legacy and not self._history_path(thread_id).exists():
                atomic_json(self._history_path(thread_id), legacy)
            atomic_json(self._path(thread_id), value)
        return value

    def _write(self, thread: dict) -> dict:
        thread["updated_at"] = iso_utc()
        atomic_json(self._path(thread["id"]), thread)
        return thread

    # -- search cache --------------------------------------------------------
    # Never let a cache write break the conversation that produced it: a missed
    # row costs one message its searchability until the next boot heals it.

    def _cache(self, method: str, *args) -> None:
        if self.index is None:
            return
        try:
            getattr(self.index, method)(*args)
        except Exception:
            log.warning("conversation index %s failed", method, exc_info=True)

    def list(self, include_messages: bool = False) -> list[dict]:
        """Thread summaries, newest first.

        `include_messages` returns the transcript alongside each summary so
        callers that need to look inside (conversation search) don't re-parse
        every file a second time via get().
        """
        if not self.root.exists():
            return []
        threads = []
        for path in self.root.glob("*.json"):
            value = read_json(path, None, skip_corrupt=True)
            if not isinstance(value, dict):
                continue
            messages = value.get("messages") or []
            preview = ""
            for message in reversed(messages):
                if message.get("text"):
                    preview = str(message["text"]).replace("\n", " ")[:120]
                    break
            # A housekeeping summary describes the whole conversation, which is
            # more use than its last line — but only while it still describes
            # this transcript. Freshness is the message count it was written
            # at: a reply changes it, so the preview goes live again, while a
            # restamp of `updated_at` by anything else (a rename, a status
            # repair) leaves the summary standing. Keyed on `updated_at`, those
            # writes silently threw the summary away.
            summary = value.get("summary")
            if (
                isinstance(summary, dict)
                and summary.get("text")
                and _summary_fresh(summary, value, len(messages))
            ):
                preview = str(summary["text"])[:120]
            threads.append(
                {
                    key: value.get(key)
                    for key in (
                        "id",
                        "title",
                        "kind",
                        "date",
                        "created_at",
                        "updated_at",
                        # What the nightly pass has already judged. In the
                        # listing row so housekeeping can pick candidates
                        # without re-reading every thread file.
                        "housekept",
                    )
                }
                | {"message_count": len(messages), "preview": preview}
                | ({"messages": messages} if include_messages else {})
            )
        return sorted(
            threads,
            key=lambda item: str(item.get("updated_at") or ""),
            reverse=True,
        )

    def get(self, thread_id: str) -> dict:
        return self._read(thread_id)

    def set_summary(self, thread_id: str, text: str) -> dict:
        """Attach a housekeeping summary describing the conversation.

        Deliberately not `_write`: that restamps `updated_at`, which would both
        make the summary instantly stale by its own freshness rule — the
        feature would silently do nothing — and shove the thread to the top of
        a sidebar that sorts on `updated_at`. Describing a conversation is not
        a change to it.
        """
        thread = self._read(thread_id)
        thread["summary"] = {
            "text": str(text)[:80],
            # The transcript it describes, by length. `source_updated_at` is
            # still written for anything reading older files, but freshness is
            # judged on the count — see _summary_fresh.
            "message_count": len(thread.get("messages") or []),
            "source_updated_at": thread.get("updated_at"),
            "created_at": iso_utc(),
        }
        atomic_json(self._path(thread_id), thread)
        return thread

    def set_housekept(self, thread_id: str, message_count: int) -> dict:
        """Record that the nightly pass has judged this conversation as it
        stands, so it is not re-sent to the model until something is said in it.

        Same reasoning as `set_summary`: written with `atomic_json` rather than
        `_write`, because being judged is not a change to the conversation and
        must not restamp `updated_at` (which reorders the sidebar and would
        invalidate the summary written moments earlier).
        """
        thread = self._read(thread_id)
        thread["housekept"] = {"at": iso_utc(), "message_count": int(message_count)}
        atomic_json(self._path(thread_id), thread)
        return thread

    def create(
        self,
        *,
        title: str = "New chat",
        kind: str = "named",
        date: str | None = None,
        thread_id: str | None = None,
    ) -> dict:
        thread_id = thread_id or uuid.uuid4().hex[:16]
        path = self._path(thread_id)
        if path.exists():
            return self._read(thread_id)
        now = iso_utc()
        return self._write(
            {
                "id": thread_id,
                "title": title.strip()[:120] or "New chat",
                "kind": kind,
                "date": date,
                "created_at": now,
                "updated_at": now,
                "messages": [],
            }
        )

    def ensure_daily(self, day: str, title: str) -> tuple[dict, bool]:
        thread_id = f"daily-{day}"
        path = self._path(thread_id)
        if path.exists():
            return self._read(thread_id), False
        return self.create(
            title=title,
            kind="daily",
            date=day,
            thread_id=thread_id,
        ), True

    def rename(self, thread_id: str, title: str) -> dict:
        thread = self._read(thread_id)
        title = title.strip()
        if not title:
            raise NoteError(400, "Thread title cannot be empty")
        thread["title"] = title[:120]
        return self._write(thread)

    def set_greeting(self, thread_id: str, text: str) -> dict:
        """Stamp the day's greeting on a daily thread. The morning check dedups
        on this stamp — not on the thread having messages, which scheduled runs
        also write."""
        thread = self._read(thread_id)
        thread["greeting"] = str(text)
        return self._write(thread)

    def delete(self, thread_id: str) -> None:
        path = self._path(thread_id)
        if not path.exists():
            raise NoteError(404, "OMNIA thread not found")
        path.unlink()
        self._history_path(thread_id).unlink(missing_ok=True)
        self._cache("drop_thread", thread_id)
        bindings = self._note_threads()
        kept = {note: bound for note, bound in bindings.items() if bound != thread_id}
        if kept != bindings:
            self._save_note_threads(kept)

    # -- note bindings -------------------------------------------------------
    # Which thread is a note's own drawer conversation, by note path. Clients
    # kept this pointer in their own localStorage only, so a move they did not
    # see live (on another device, or OMNIA's while the laptop slept) orphaned
    # the conversation, and each other device started a thread of its own.
    # One small map rather than a field on every thread: a drawer's lookup
    # reads one file, not every transcript. A deleted note keeps its entry, so
    # restoring it from the Trash brings the conversation back with it.

    def _note_threads(self) -> dict[str, str]:
        try:
            value = read_json(self.note_threads_path, {})
        except NoteError:
            # read_json set the unreadable file aside. A binding is a
            # convenience; no rename or drawer may fail over one.
            return {}
        if not isinstance(value, dict):
            return {}
        return {str(note): bound for note, bound in value.items() if isinstance(bound, str)}

    def _save_note_threads(self, bindings: dict[str, str]) -> None:
        atomic_json(self.note_threads_path, dict(sorted(bindings.items())))

    def _exists(self, thread_id: str) -> bool:
        try:
            return self._path(thread_id).exists()
        except NoteError:
            return False

    def for_note(self, note_path: str) -> str | None:
        """The thread bound to `note_path`, while it still exists."""
        thread_id = self._note_threads().get(note_path)
        return thread_id if thread_id and self._exists(thread_id) else None

    def bind_note(self, note_path: str, thread_id: str) -> str | None:
        """Bind a note to `thread_id` unless a live thread already holds it.
        Returns the thread the note is bound to afterwards: the first device
        to bind wins, and the others adopt its conversation. None when the
        thread belongs to another note: a pointer left at a path the note has
        since moved away from must not follow a new note that took the path."""
        self._read(thread_id)  # 404 for an unknown thread
        bound = self.for_note(note_path)
        if bound:
            return bound
        bindings = self._note_threads()
        if thread_id in bindings.values():
            return None
        bindings[note_path] = thread_id
        self._save_note_threads(bindings)
        return thread_id

    def create_for_note(self, note_path: str, title: str) -> dict:
        """The note's thread, made and bound now if it has none yet."""
        bound = self.for_note(note_path)
        if bound:
            return self._read(bound)
        thread = self.create(title=title)
        self.bind_note(note_path, thread["id"])
        return thread

    def notes_moved(self, moves) -> bool:
        """Carry bindings along with renamed or moved notes, all at once (a
        folder move is one step, and an undo can swap two paths). A move's
        destination did not exist before it, so a binding left there by a
        deleted note goes: the note moved in must not inherit its
        conversation. Returns whether anything changed."""
        moves = [(old, new) for old, new in moves if old != new]
        if not moves:
            return False
        bindings = self._note_threads()
        before = dict(bindings)
        carried = {new: bindings[old] for old, new in moves if old in bindings}
        for old, new in moves:
            bindings.pop(old, None)
            bindings.pop(new, None)
        bindings.update(carried)
        if bindings == before:
            return False
        self._save_note_threads(bindings)
        return True

    def append_public(self, thread_id: str, message: dict) -> dict:
        thread = self._read(thread_id)
        message = {
            "id": message.get("id") or uuid.uuid4().hex[:16],
            "role": message["role"],
            "text": str(message.get("text") or ""),
            "created_at": message.get("created_at") or iso_utc(),
            "status": message.get("status") or "complete",
            "context_note": message.get("context_note"),
            # Editor selection offsets, so selection-editing tools act on the
            # exact range (present only on user messages that had a live editor).
            "selection_range": message.get("selection_range"),
            "trace": message.get("trace") or [],
            "graphics": message.get("graphics") or [],
            "actions": message.get("actions") or [],
            "confirmations": message.get("confirmations") or [],
            # Attachment paths, so images stay visible when a thread reloads.
            "images": message.get("images") or [],
            # Token counts for the turn, filled in by update_message when the
            # reply completes. Seeded here so the key is always present in the
            # JSON the thread endpoints return, rather than appearing only on
            # messages that happened to finish.
            "usage": message.get("usage") or {},
        }
        thread["messages"].append(message)
        if (
            message["role"] == "user"
            and thread.get("kind") == "named"
            and thread.get("title") == "New chat"
        ):
            words = message["text"].split()
            thread["title"] = (
                " ".join(words[:8])[:72]
                or ("Image" if message["images"] else "")
                or "New chat"
            )
        self._write(thread)
        self._cache("index_message", thread_id, message)
        return message

    def update_message(self, thread_id: str, message_id: str, **changes) -> dict:
        thread = self._read(thread_id)
        for message in thread["messages"]:
            if message.get("id") == message_id:
                message.update(changes)
                self._write(thread)
                # Streaming flushes land four times a second with a growing
                # partial nothing searches for. The final update — complete,
                # error or interrupted, here or in reconcile_incomplete —
                # indexes the reply once.
                if "text" in changes and changes.get("status") != "streaming":
                    self._cache("index_message", thread_id, message)
                return message
        raise NoteError(404, "OMNIA message not found")

    def truncate_from(self, thread_id: str, message_id: str) -> dict:
        """Drop `message_id` and everything after it.

        Regenerating an answer used to slice the client's array only, so the
        server still held the original exchange — reloading the thread brought
        the old question and answer straight back, followed by the duplicate.
        The provider history has to move in step, or the model keeps answering
        a question the transcript no longer shows.
        """
        thread = self._read(thread_id)
        messages = thread["messages"]
        index = next(
            (i for i, m in enumerate(messages) if m.get("id") == message_id),
            None,
        )
        if index is None:
            raise NoteError(404, "OMNIA message not found")

        removed_texts = [
            str(m.get("text") or "") for m in messages[index:] if m.get("role") == "user"
        ]
        thread["messages"] = messages[:index]

        history = self._raw_history(thread_id)
        if removed_texts and history:
            # Cut where the first removed question sits in the provider history.
            # Counting user turns back from the end assumed they line up with
            # public user messages one to one, but scheduled runs and the
            # morning check add user turns with no public message, so a turn
            # after the regenerated one left the discarded exchange behind.
            starts = user_turn_starts(history)
            if starts:
                cut = _first_removed_turn(history, starts, removed_texts)
                if cut is None:
                    # Summarized away or otherwise unrecognisable: count back.
                    keep_turns = max(len(starts) - len(removed_texts), 0)
                    cut = starts[keep_turns] if keep_turns < len(starts) else len(history)
                repaired, _ = repair_history(history[:cut])
                atomic_json(self._history_path(thread_id), repaired)

        written = self._write(thread)
        # Whole-thread, not per-message: the dropped exchange has to leave the
        # search cache too, or a regenerated answer stays findable by the text
        # it no longer has.
        self._cache("index_thread", written)
        return written

    def reconcile_incomplete(self) -> int:
        """Mark generations left open by a server stop as interrupted."""
        changed = 0
        if not self.root.exists():
            return changed
        for path in self.root.glob("*.json"):
            thread = read_json(path, None, skip_corrupt=True)
            if not isinstance(thread, dict):
                continue
            repaired = []
            for message in thread.get("messages") or []:
                if message.get("status") == "streaming":
                    message["status"] = "interrupted"
                    if not message.get("text"):
                        message["text"] = "Response interrupted by a server restart."
                    repaired.append(message)
                    changed += 1
            if repaired:
                self._write(thread)
                # Streaming flushes are never indexed, and a reply cut off
                # here never got the final update that would have.
                for message in repaired:
                    self._cache("index_message", thread["id"], message)
        return changed

    def set_provider_history(self, thread_id: str, history: list[dict]) -> None:
        """Persist provider history, repaired and trimmed.

        This is the single choke point for every write — including the ones on
        the cancellation and error paths, which are exactly the ones that used
        to persist an unmatched tool call and brick the thread permanently.
        """
        normalized, notes = normalize(history, MAX_HISTORY_CHARS)
        if notes:
            log.info("thread %s history repaired: %s", thread_id, "; ".join(notes))
        # Sidecar only: the transcript file is untouched, so streaming no longer
        # rewrites tens of kilobytes of unchanged history per token.
        atomic_json(self._history_path(thread_id), normalized)

    def _raw_history(self, thread_id: str) -> list[dict]:
        """Stored history, migrating a legacy inline copy first."""
        self._read(thread_id)
        stored = read_json(self._history_path(thread_id), [])
        return stored if isinstance(stored, list) else []

    def provider_history(self, thread_id: str) -> list[dict]:
        """History ready to send. Repairs threads broken before this existed."""
        normalized, notes = normalize(self._raw_history(thread_id), MAX_HISTORY_CHARS)
        if notes:
            log.info("thread %s history repaired on load: %s", thread_id, "; ".join(notes))
        return normalized

    def generation_lock(self, thread_id: str) -> asyncio.Lock:
        return self._generation_locks.setdefault(thread_id, asyncio.Lock())

    def register_run(self, thread_id: str, task: asyncio.Task) -> None:
        """Track the in-flight generation so it can be stopped from anywhere —
        another tab, another device — not just by the client that started it."""
        self._runs.setdefault(thread_id, []).append(task)

    def clear_run(self, thread_id: str, task: asyncio.Task) -> None:
        # Only this task: the slot used to be shared, so a queued turn's
        # registration hid the generating one from Stop and is_running.
        tasks = self._runs.get(thread_id)
        if not tasks:
            return
        tasks[:] = [t for t in tasks if t is not task]
        if not tasks:
            self._runs.pop(thread_id, None)

    def stop_run(self, thread_id: str) -> bool:
        live = [t for t in self._runs.get(thread_id) or [] if not t.done()]
        for task in live:
            task.cancel()
        return bool(live)

    def is_running(self, thread_id: str) -> bool:
        return any(not t.done() for t in self._runs.get(thread_id) or [])


def carry_note_threads(state, moves) -> None:
    """notes_moved for callers holding app state, which a bare test state may
    lack a thread store on."""
    threads = getattr(state, "threads", None)
    if threads is not None and moves:
        threads.notes_moved(moves)
