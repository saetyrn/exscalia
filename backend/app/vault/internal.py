"""Git-backed internal application data.

Everything under ``.exscalia`` is deliberately part of the vault backup but
is never exposed as an ordinary note or folder.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .notes import NoteError, NoteStore, check_landing, resolve_under

log = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat().replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def atomic_bytes(path: Path, data: bytes, *, fsync: bool = True) -> None:
    """Replace ``path`` with ``data``: a reader sees the old bytes or the new
    ones, never a truncated mix.

    The temp file sits beside the target (``os.replace`` is atomic only within
    one filesystem), dot-prefixed and named for this write alone, so two
    writers of one path never share it. ``os.open`` rather than
    ``NamedTemporaryFile`` keeps the umask mode earlier writes produced
    instead of 0600, and a file being replaced keeps its own mode. ``fsync``
    makes the bytes durable before the rename, so a crash leaves one version
    or the other, not an empty file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.parent / f".{path.name}.{uuid.uuid4().hex[:12]}.tmp"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            if fsync:
                os.fsync(handle.fileno())
        try:
            # A rewrite in place kept the file's mode; the replacement is a
            # new inode, so carry it over.
            os.chmod(temp, stat.S_IMODE(path.stat().st_mode))
        except FileNotFoundError:
            pass
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def atomic_json(path: Path, value: Any, *, fsync: bool = True) -> None:
    """The shared JSON writer: ``atomic_bytes``, fsynced like every other
    store write. ``fsync=False`` is for a caller rewriting many files in one
    go whose last write is a durable one (Paper's re-stamp of a moved
    notebook's pages)."""
    text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    atomic_bytes(path, text.encode("utf-8"), fsync=fsync)


def atomic_secret_json(path: Path, value: Any) -> None:
    """Write a secrets file (API keys, the VAPID private key) so ``path`` is
    either the old content or the complete new content, and 0600 from birth.

    ``write_text`` truncates first: a full disk left a 392-byte ai.json as 60
    bytes of invalid JSON, and the next Settings save wrote one field over
    every provider key. A chmod after the write also left a 0644 window.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(value, indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def set_aside(path: Path, tag: str = "corrupt") -> Path:
    """Rename an unreadable store to ``<name>.<tag>-<UTC ts>`` so it can be
    recovered by hand; returns the new path (``path`` itself if the rename
    failed). An earlier set-aside from the same second is never replaced."""
    stamp = utc_now().strftime("%Y%m%dT%H%M%S")
    quarantine = path.with_name(f"{path.name}.{tag}-{stamp}")
    n = 1
    while quarantine.exists():
        quarantine = path.with_name(f"{path.name}.{tag}-{stamp}.{n}")
        n += 1
    try:
        path.rename(quarantine)
    except OSError:  # pragma: no cover - best effort
        return path
    return quarantine


def quarantine_pending(path: Path) -> Path | None:
    """The newest ``<name>.corrupt-*`` / ``<name>.unreadable-*`` sibling of
    ``path``, if any. While one exists the store it came from must not be
    written: the set-aside copy is the only record of what was there."""
    found = [
        p
        for tag in ("corrupt", "unreadable")
        for p in path.parent.glob(f"{path.name}.{tag}-*")
    ]
    if not found:
        return None
    return max(found, key=lambda p: p.name.rsplit("-", 1)[-1])


def refuse_if_set_aside(path: Path) -> None:
    """Raise instead of writing a store whose last content was quarantined."""
    q = quarantine_pending(path)
    if q is not None:
        raise NoteError(
            500,
            f"{path.name} was set aside as {q.name} after it became unreadable; "
            "nothing was overwritten. Inspect or remove it, then save again.",
        )


def refuse_if_unreadable(path: Path) -> None:
    """Raise instead of replacing a store that exists but cannot be read now.

    A permission or I/O error is not corruption, so the loaders leave the file
    in place and read it as {}; whatever a caller built on that {} would bury
    the real content."""
    try:
        path.read_bytes()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise NoteError(
            500,
            f"{path.name} cannot be read ({exc.strerror or exc}); nothing was "
            "overwritten. Fix its permissions, then save again.",
        ) from exc


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


def read_versioned(
    path: Path, default: Any, *, version: int, name: str | None = None
) -> Any:
    """``read_json`` for a store about to be rewritten.

    Stores rewrite their whole file, so after a rollback an older image
    writing over a file stamped by a newer one would silently drop whatever
    that version added. Refuse instead. Readers that only display may keep
    using ``read_json``.
    """
    value = read_json(path, default)
    stored = value.get("version") if isinstance(value, dict) else None
    if isinstance(stored, int) and not isinstance(stored, bool) and stored > version:
        raise NoteError(
            409,
            f"{name or path.name} was written by a newer app version "
            f"(v{stored}); refusing to modify it",
        )
    return value


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

    def manifest(self, item_id: str) -> dict:
        """An item's manifest, for a caller that must check before restoring."""
        return self._manifest(item_id)

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
        rel_path = self.store.normalize_folder(rel_path)
        self.store.refuse_protected_folder(rel_path, "deleted")
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

    # Paper notebooks and pages are package directories, not notes, so they
    # get their own kinds: the payload is moved wholesale and nothing in it is
    # ever counted or previewed as markdown.
    PAPER_KINDS = ("notebook", "page")

    def resolve_paper(self, rel_path: str) -> Path:
        """Validate a vault-relative path under ``paper/`` for trash moves."""
        p = Path(rel_path)
        if p.is_absolute() or any(
            part in ("..", ".git", NoteStore.INTERNAL_DIR) for part in p.parts
        ):
            raise NoteError(400, "Invalid paper path")
        if not p.parts or p.parts[0] != NoteStore.PAPER_DIR:
            raise NoteError(400, "Paper items must live under paper/")
        abs_path = resolve_under(self.vault_dir, rel_path, "Invalid paper path")
        if not abs_path.is_relative_to((self.vault_dir / NoteStore.PAPER_DIR).resolve()):
            raise NoteError(400, "Invalid paper path")
        return abs_path

    def trash_paper(
        self, kind: str, rel_path: str, extra: dict | None = None
    ) -> dict:
        """Move a notebook or page directory into the Trash.

        ``extra`` carries what a restore needs to put a page back where it was
        ({notebook, notebook_id, page_id, position}); the notebook's own
        manifest is not rewritten here, since the caller owns it.
        """
        if kind not in self.PAPER_KINDS:
            raise NoteError(400, f"Unknown paper trash kind: {kind}")
        source = self.resolve_paper(rel_path)
        if not source.is_dir():
            raise NoteError(404, f"Not found: {rel_path}")
        item_id, manifest = self._new_manifest(kind, rel_path, 0)
        manifest.update(extra or {})
        entry = self._entry(item_id)
        destination = entry / "payload" / rel_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.rename(destination)
        atomic_json(entry / "manifest.json", manifest)
        return manifest

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
        return sorted(
            items, key=lambda item: str(item.get("deleted_at") or ""), reverse=True
        )

    def preview(self, item_id: str) -> dict:
        manifest = self._manifest(item_id)
        payload = self._entry(item_id) / "payload" / manifest["original_path"]
        if manifest["kind"] == "note":
            content = payload.read_text(encoding="utf-8") if payload.is_file() else ""
            return {**manifest, "content": content, "paths": [manifest["original_path"]]}
        if manifest["kind"] in self.PAPER_KINDS:
            # Ink and renders have no textual preview; the client shows the
            # notebook title and, for a page, its own stored render.
            return {**manifest, "content": None, "paths": []}
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
        elif manifest["kind"] in self.PAPER_KINDS:
            destination = self.resolve_paper(target)
        else:
            destination = self.store.resolve_folder(target)
        if destination.exists():
            raise NoteError(409, f"Restore destination already exists: {target}")
        if manifest["kind"] == "note":
            check_landing(target)
        elif manifest["kind"] not in self.PAPER_KINDS:
            for note in sorted(source.rglob("*.md")):
                check_landing((Path(target) / note.relative_to(source)).as_posix())
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.rename(destination)
        if manifest["kind"] == "note":
            restored_notes = [target]
        elif manifest["kind"] in self.PAPER_KINDS:
            restored_notes = []
        else:
            restored_notes = [
                str(p.relative_to(self.vault_dir))
                for p in sorted(destination.rglob("*.md"))
            ]
        shutil.rmtree(self._entry(item_id), ignore_errors=True)
        if (
            manifest["kind"] == "note"
            and target != original
            and self.store.title_follows_path(target)
        ):
            # Restored under a new name (the Trash page's 409 fallback, OMNIA's
            # target_path): the title follows the filename as NoteStore.rename
            # makes it, so the index rows callers build from the file do too.
            try:
                content = self.store.read(target)
                aligned = self.store.align_title(target, content)
            except NoteError:
                # Unloadable frontmatter, or bytes that are not UTF-8. The
                # restore stands; the first save realigns the title.
                pass
            else:
                if aligned != content:
                    self.store.write(target, aligned)
        restored = {
            "id": item_id,
            "kind": manifest["kind"],
            "path": target,
            "notes": restored_notes,
        }
        for key in ("notebook", "notebook_id", "page_id", "position"):
            if key in manifest:
                restored[key] = manifest[key]
        return restored

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
            # One hand-edited entry (a null or naive expires_at, an id that
            # is not its directory) or one failed rmtree used to escape here,
            # ending the maintenance loop for good and, at boot, taking the
            # whole app down with it. Skip it and expire the rest.
            try:
                if parse_utc(item["expires_at"]) <= now:
                    removed.append(self.purge(item["id"]))
            except Exception:
                log.warning("skipping trash entry %s", item.get("id"), exc_info=True)
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
