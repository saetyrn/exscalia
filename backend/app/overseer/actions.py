"""Direct OMNIA actions, confirmations, scoped commits, and Undo."""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
from datetime import timedelta
from pathlib import Path
from typing import Any

from ..index.db import content_hash
from ..vault.internal import atomic_json, iso_utc, parse_utc, read_json, utc_now
from ..vault.notes import NoteError, NoteStore
from ..vault.repo import OVERSEER_AUTHOR

log = logging.getLogger(__name__)


class ActionCoordinator:
    CONFIRMATION_LIFETIME = timedelta(minutes=10)

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
    ) -> dict[str, Any]:
        self._purge_confirmations()
        token = secrets.token_hex(16)
        expires = utc_now() + self.CONFIRMATION_LIFETIME
        summary = self._confirmation_summary(tool, inputs)
        item = {
            "token": token,
            "thread_id": thread_id,
            "auth_session_id": auth_session_id,
            "tool": tool,
            "inputs": inputs,
            "summary": summary,
            "created_at": iso_utc(),
            "expires_at": iso_utc(expires),
        }
        self._confirmations[token] = item
        self._save_confirmations()
        return {
            key: item[key]
            for key in ("token", "tool", "summary", "created_at", "expires_at")
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
    ) -> None:
        self._purge_confirmations()
        item = self._confirmations.get(token)
        if item is None:
            return
        if item["thread_id"] != thread_id:
            raise NoteError(403, "That confirmation belongs to another OMNIA thread")
        if item.get("auth_session_id") != auth_session_id:
            raise NoteError(403, "That confirmation belongs to another signed-in device")
        del self._confirmations[token]
        self._save_confirmations()

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

    @staticmethod
    def _confirmation_summary(tool: str, inputs: dict[str, Any]) -> str:
        if tool == "bulk_actions":
            count = len(inputs.get("operations") or [])
            return f"Apply {count} OMNIA changes as separate undoable actions?"
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
        if tool == "rename_tag" and inputs.get("to_tag"):
            detail = (
                f" “{inputs.get('from_tag')}” → “{inputs['to_tag']}” across every note"
            )
        return f"{label}{detail}?"

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
        self.announce_paths(paths)
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
        self.announce_paths(paths)
        return action

    def pending_confirmations(self) -> list[dict[str, Any]]:
        """Confirmation cards still awaiting the user, projected to safe fields
        (never the raw tool `inputs`, which can hold note bodies)."""
        self._purge_confirmations()
        return [
            {key: item.get(key) for key in ("token", "tool", "summary", "created_at", "expires_at")}
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
            undo_sha = await self.state.vault.revert_commit(
                action["commit"],
                f"Undo OMNIA action: {action['summary']}",
                OVERSEER_AUTHOR,
            )
        except ValueError as exc:
            raise NoteError(409, str(exc)) from exc

        action["undone_at"] = iso_utc()
        action["undo_commit"] = undo_sha
        action["undoable"] = False
        atomic_json(self._action_path(action_id), action)
        await self._refresh_after_undo(
            action.get("refresh") or [], action.get("paths") or []
        )
        self.announce_paths(action.get("paths") or [])
        self.state.autocommit.touch()
        return {
            "ok": True,
            "action_id": action_id,
            "summary": f"Undid: {action['summary']}",
            "commit": undo_sha,
            "refresh": action.get("refresh") or [],
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

    async def _reindex(self, paths: list[str]) -> None:
        """Re-read each path after a revert; a missing one was undone away."""
        for rel in paths:
            try:
                content = self.state.store.read(rel)
            except NoteError:
                await self.state.indexer.remove_note(rel)
            else:
                await self.state.indexer.index_note(rel, content)

    async def _refresh_after_undo(self, refresh: list[str], paths: list[str]) -> None:
        if any(
            item in refresh
            for item in ("notes", "tasks", "calendar", "search")
        ):
            # A revert touches exactly the paths the action recorded, so
            # reindex those rather than resyncing the whole vault — undoing a
            # one-line edit used to re-read and re-embed every note.
            notes = [p for p in paths if p.endswith(".md")]
            if notes:
                await self._reindex(notes)
            else:
                await self.state.indexer.sync_all(force=True)
        if "appearance" in refresh or "settings" in refresh:
            from ..vault.internal import read_json

            config = read_json(
                self.state.settings.vault_dir
                / NoteStore.INTERNAL_DIR
                / "config.json",
                {},
            )
            if isinstance(config, dict) and config.get("timezone"):
                self.state.timezone = config["timezone"]
