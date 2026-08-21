"""Git-backed internal application data.

Everything under ``.exscalia`` is deliberately part of the vault backup but
is never exposed as an ordinary note or folder.
"""

from __future__ import annotations

import json
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .notes import NoteError, NoteStore


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat().replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)


def read_json(path: Path, default: Any, *, skip_corrupt: bool = False) -> Any:
    """Read a JSON store. A missing file is normal; a corrupt one is not.

    Returning the default for unreadable content destroyed data: a truncated
    write left ``events.json`` unparseable, ``EventStore._events()`` saw an
    empty list, and the next ``add()`` persisted a single-event file over the
    whole calendar. The corrupt file is quarantined so it can be recovered by
    hand, and the failure is raised rather than swallowed.

    ``skip_corrupt`` is for callers scanning a directory, where one bad file
    should not take out the entire listing.
    """
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        quarantine = path.with_suffix(
            path.suffix + f".corrupt-{utc_now().strftime('%Y%m%dT%H%M%S')}"
        )
        try:
            path.rename(quarantine)
        except OSError:  # pragma: no cover - best effort
            quarantine = path
        if skip_corrupt:
            return default
        raise NoteError(
            500,
            f"{path.name} is unreadable and was set aside as {quarantine.name}. "
            "Nothing was overwritten.",
        ) from exc


@dataclass
class TrashItem:
    id: str
    kind: str
    original_path: str
    deleted_at: str
    expires_at: str
    note_count: int

    def public(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "original_path": self.original_path,
            "deleted_at": self.deleted_at,
            "expires_at": self.expires_at,
            "note_count": self.note_count,
        }


class TrashStore:
    RETENTION_DAYS = 30

    def __init__(self, vault_dir: Path, store: NoteStore):
        self.vault_dir = vault_dir
        self.store = store
        self.root = vault_dir / NoteStore.INTERNAL_DIR / "trash"

    def _entry(self, item_id: str) -> Path:
        if not item_id or any(c not in "0123456789abcdef" for c in item_id):
            raise NoteError(400, "Invalid Trash item")
        return self.root / item_id

    def _manifest(self, item_id: str) -> dict:
        manifest = read_json(self._entry(item_id) / "manifest.json", None)
        if not isinstance(manifest, dict):
            raise NoteError(404, "Trash item not found")
        return manifest

    def _new_manifest(self, kind: str, path: str, note_count: int) -> tuple[str, dict]:
        item_id = uuid.uuid4().hex
        deleted = utc_now()
        return item_id, {
            "id": item_id,
            "kind": kind,
            "original_path": path,
            "deleted_at": iso_utc(deleted),
            "expires_at": iso_utc(deleted + timedelta(days=self.RETENTION_DAYS)),
            "note_count": note_count,
        }

    def trash_note(self, rel_path: str) -> dict:
        source = self.store.resolve(rel_path)
        if not source.is_file():
            raise NoteError(404, f"Note not found: {rel_path}")
        item_id, manifest = self._new_manifest("note", rel_path, 1)
        entry = self._entry(item_id)
        destination = entry / "payload" / rel_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.rename(destination)
        atomic_json(entry / "manifest.json", manifest)
        return manifest

    def trash_folder(self, rel_path: str) -> tuple[dict, list[str]]:
        source = self.store.resolve_folder(rel_path)
        if not source.is_dir():
            raise NoteError(404, f"Folder not found: {rel_path}")
        notes = self.store._contained_notes(source)
        item_id, manifest = self._new_manifest("folder", rel_path, len(notes))
        entry = self._entry(item_id)
        destination = entry / "payload" / rel_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.rename(destination)
        atomic_json(entry / "manifest.json", manifest)
        return manifest, notes

    def list(self) -> list[dict]:
        items: list[dict] = []
        if not self.root.exists():
            return items
        for entry in self.root.iterdir():
            if not entry.is_dir():
                continue
            manifest = read_json(entry / "manifest.json", None, skip_corrupt=True)
            if isinstance(manifest, dict):
                items.append(manifest)
        return sorted(items, key=lambda item: item.get("deleted_at", ""), reverse=True)

    def preview(self, item_id: str) -> dict:
        manifest = self._manifest(item_id)
        payload = self._entry(item_id) / "payload" / manifest["original_path"]
        if manifest["kind"] == "note":
            content = payload.read_text(encoding="utf-8") if payload.is_file() else ""
            return {**manifest, "content": content, "paths": [manifest["original_path"]]}
        paths = [
            str(path.relative_to(self._entry(item_id) / "payload"))
            for path in sorted(payload.rglob("*.md"))
        ]
        return {**manifest, "content": None, "paths": paths}

    def restore(self, item_id: str, target: str | None = None) -> dict:
        manifest = self._manifest(item_id)
        original = manifest["original_path"]
        target = (target or original).strip("/")
        source = self._entry(item_id) / "payload" / original
        if manifest["kind"] == "note":
            destination = self.store.resolve(target)
        else:
            destination = self.store.resolve_folder(target)
        if destination.exists():
            raise NoteError(409, f"Restore destination already exists: {target}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.rename(destination)
        restored_notes = (
            [target]
            if manifest["kind"] == "note"
            else [
                str(p.relative_to(self.vault_dir))
                for p in sorted(destination.rglob("*.md"))
            ]
        )
        shutil.rmtree(self._entry(item_id), ignore_errors=True)
        return {
            "id": item_id,
            "kind": manifest["kind"],
            "path": target,
            "notes": restored_notes,
        }

    def purge(self, item_id: str) -> dict:
        manifest = self._manifest(item_id)
        shutil.rmtree(self._entry(item_id))
        return manifest

    def empty(self) -> int:
        count = len(self.list())
        if self.root.exists():
            shutil.rmtree(self.root)
        return count

    def purge_expired(self, now: datetime | None = None) -> list[dict]:
        now = now or utc_now()
        removed: list[dict] = []
        for item in self.list():
            try:
                if parse_utc(item["expires_at"]) <= now:
                    removed.append(self.purge(item["id"]))
            except (KeyError, ValueError):
                continue
        return removed


class TaskTombstones:
    """Small Git-backed ledger for milestones of task lines that disappear."""

    # Only ever grew before; cap at the newest deletions — a timeline showing
    # the last two thousand is indistinguishable from one showing all of them.
    MAX_TOMBSTONES = 2000

    def __init__(self, vault_dir: Path):
        self.path = vault_dir / NoteStore.INTERNAL_DIR / "task-tombstones.json"

    def all(self) -> dict[str, dict]:
        value = read_json(self.path, {})
        return value if isinstance(value, dict) else {}

    def record(self, rows: list[dict]) -> None:
        if not rows:
            return
        current = self.all()
        changed = False
        for row in rows:
            task_id = row.get("id")
            if not task_id:
                continue
            current[task_id] = {
                key: row.get(key)
                for key in (
                    "id",
                    "path",
                    "text",
                    "status",
                    "created_at",
                    "started_at",
                    "completed_at",
                    "reopened_at",
                    "due",
                    # Carried so the calendar can keep a deleted board task's
                    # events hidden the same way a live one's are.
                    "board",
                )
            }
            current[task_id]["deleted_at"] = iso_utc()
            changed = True
        if changed:
            if len(current) > self.MAX_TOMBSTONES:
                newest = sorted(
                    current.items(),
                    key=lambda item: str(item[1].get("deleted_at") or ""),
                    reverse=True,
                )[: self.MAX_TOMBSTONES]
                current = dict(newest)
            atomic_json(self.path, current)

    def reactivate(self, task_ids: set[str]) -> None:
        if not task_ids:
            return
        current = self.all()
        changed = False
        for task_id in task_ids:
            if task_id in current:
                del current[task_id]
                changed = True
        if changed:
            atomic_json(self.path, current)
