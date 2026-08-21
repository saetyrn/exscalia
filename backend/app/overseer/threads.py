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

log = logging.getLogger(__name__)

# Ceiling for a single conversation's provider history. Past this, the oldest
# exchanges are dropped rather than letting the provider reject the request.
MAX_HISTORY_CHARS = 400_000


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

    def __init__(self, vault_dir: Path, data_dir: Path | None = None):
        self.root = vault_dir / NoteStore.INTERNAL_DIR / "omnia" / "threads"
        # Outside the vault, so it is neither committed nor pushed off-site.
        self.history_root = (data_dir or vault_dir) / "omnia" / "history"
        self._lock = asyncio.Lock()
        self._generation_locks: dict[str, asyncio.Lock] = {}
        # In-flight generations, so Stop works server-side rather than relying
        # on the originating client aborting its own fetch.
        self._runs: dict[str, asyncio.Task] = {}

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
            # this transcript. Every mutator restamps `updated_at`, so exact
            # equality means anything said since silently reverts to the live
            # preview until the next nightly pass catches up.
            summary = value.get("summary")
            if (
                isinstance(summary, dict)
                and summary.get("text")
                and summary.get("source_updated_at") == value.get("updated_at")
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
            "source_updated_at": thread.get("updated_at"),
            "created_at": iso_utc(),
        }
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

    def delete(self, thread_id: str) -> None:
        path = self._path(thread_id)
        if not path.exists():
            raise NoteError(404, "OMNIA thread not found")
        path.unlink()
        self._history_path(thread_id).unlink(missing_ok=True)

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
        return message

    def update_message(self, thread_id: str, message_id: str, **changes) -> dict:
        thread = self._read(thread_id)
        for message in thread["messages"]:
            if message.get("id") == message_id:
                message.update(changes)
                self._write(thread)
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

        removed_user_turns = sum(1 for m in messages[index:] if m.get("role") == "user")
        thread["messages"] = messages[:index]

        history = self._raw_history(thread_id)
        if removed_user_turns and history:
            # Public user messages line up with real user turns in the provider
            # history, so cut back that many of them from the end.
            starts = user_turn_starts(history)
            if starts:
                keep_turns = max(len(starts) - removed_user_turns, 0)
                cut = starts[keep_turns] if keep_turns < len(starts) else len(history)
                repaired, _ = repair_history(history[:cut])
                atomic_json(self._history_path(thread_id), repaired)

        return self._write(thread)

    def reconcile_incomplete(self) -> int:
        """Mark generations left open by a server stop as interrupted."""
        changed = 0
        if not self.root.exists():
            return changed
        for path in self.root.glob("*.json"):
            thread = read_json(path, None, skip_corrupt=True)
            if not isinstance(thread, dict):
                continue
            dirty = False
            for message in thread.get("messages") or []:
                if message.get("status") == "streaming":
                    message["status"] = "interrupted"
                    if not message.get("text"):
                        message["text"] = "Response interrupted by a server restart."
                    dirty = True
                    changed += 1
            if dirty:
                self._write(thread)
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
        self._runs[thread_id] = task

    def clear_run(self, thread_id: str, task: asyncio.Task) -> None:
        if self._runs.get(thread_id) is task:
            self._runs.pop(thread_id, None)

    def stop_run(self, thread_id: str) -> bool:
        task = self._runs.get(thread_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    def is_running(self, thread_id: str) -> bool:
        task = self._runs.get(thread_id)
        return task is not None and not task.done()
