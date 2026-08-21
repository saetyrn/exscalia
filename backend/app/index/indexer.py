"""Keeps the SQLite metadata index in sync with the vault working tree."""
import logging
from pathlib import Path

import aiosqlite

from .. import media_items, task_boards
from ..vault import fm
from ..vault.notes import NoteStore
from ..vault.internal import TaskTombstones
from . import parse
from .db import content_hash

log = logging.getLogger(__name__)


class Indexer:
    def __init__(
        self,
        db: aiosqlite.Connection,
        store: NoteStore,
        tombstones: TaskTombstones | None = None,
    ):
        self.db = db
        self.store = store
        self.tombstones = tombstones
        # Set by the semantic index (phase 3) to receive change notifications.
        self.on_note_changed = None
        self.on_note_removed = None

    async def index_note(self, rel_path: str, content: str, mtime: float | None = None) -> None:
        meta, body = fm.parse(content)
        title = str(meta.get("title") or Path(rel_path).stem)
        # Recomputed from frontmatter on every save, so adding or removing the
        # marker line in the editor moves the note in or out of board status
        # with no other plumbing.
        board = 1 if task_boards.is_board(meta) else 0
        if mtime is None:
            p = self.store.resolve(rel_path)
            mtime = p.stat().st_mtime if p.exists() else 0.0
        line_offset = parse.frontmatter_line_count(content, body)

        old_tasks = [
            dict(row)
            for row in await self.db.execute_fetchall(
                "SELECT * FROM tasks WHERE path = ?", (rel_path,)
            )
        ]
        parsed_tasks = parse.extract_tasks(body, line_offset)
        for task in parsed_tasks:
            if task.id.startswith("legacy:"):
                task.id = f"legacy:{rel_path}:{task.line}"
        active_ids = {task.id for task in parsed_tasks}
        if self.tombstones:
            self.tombstones.record(
                [task for task in old_tasks if task.get("id") not in active_ids]
            )
            self.tombstones.reactivate(active_ids)

        await self._delete_rows(rel_path)
        await self.db.execute(
            "INSERT INTO notes(path, title, created, modified, mtime, hash, pinned, task_board)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                rel_path,
                title,
                str(meta.get("created") or ""),
                str(meta.get("modified") or ""),
                mtime,
                content_hash(content),
                1 if meta.get("pinned") else 0,
                board,
            ),
        )
        await self.db.execute(
            "INSERT INTO notes_fts(path, title, body) VALUES (?,?,?)", (rel_path, title, body)
        )
        for target in parse.extract_wikilinks(body):
            await self.db.execute(
                "INSERT INTO links(source, target, target_lower) VALUES (?,?,?)",
                (rel_path, target, target.lower()),
            )
        for tag in parse.extract_tags(meta, body):
            await self.db.execute("INSERT INTO tags(path, tag) VALUES (?,?)", (rel_path, tag))
        for task in parsed_tasks:
            # OR IGNORE: a note whose content was copied from another note
            # (old conflict copies) can carry a task id that already exists.
            # The duplicate task is skipped rather than crashing indexing —
            # a single bad note must never take the whole server down.
            await self.db.execute(
                """INSERT OR IGNORE INTO tasks(
                       id, path, line, meta_line, text, status,
                       created_at, started_at, completed_at, reopened_at, due, due_visible,
                       description, board, sort, reminder_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    task.id,
                    rel_path,
                    task.line,
                    task.meta_line,
                    task.text,
                    task.status,
                    task.created_at,
                    task.started_at,
                    task.completed_at,
                    task.reopened_at,
                    task.due,
                    int(task.due_visible),
                    task.description,
                    board,
                    task.sort,
                    task.reminder_at,
                ),
            )
        if media_items.is_media_item(meta):
            fields = media_items.item_fields(meta, Path(rel_path).stem)
            await self.db.execute(
                """INSERT INTO media_items(
                       path, title, alias, media_type, status, creator, year, cover,
                       rating, started, finished, total, modified
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    rel_path,
                    fields["title"],
                    fields["alias"],
                    fields["media_type"],
                    fields["status"],
                    fields["creator"],
                    fields["year"],
                    fields["cover"],
                    fields["rating"],
                    fields["started"],
                    fields["finished"],
                    fields["total"],
                    fields["modified"],
                ),
            )
            for entry in media_items.parse_log(body):
                await self.db.execute(
                    "INSERT INTO media_log(path, line, at, kind, value, text)"
                    " VALUES (?,?,?,?,?,?)",
                    (
                        rel_path,
                        line_offset + entry.line,
                        entry.at,
                        entry.kind,
                        entry.value,
                        entry.text,
                    ),
                )
        await self.db.commit()
        if self.on_note_changed:
            await self.on_note_changed(rel_path, content)

    async def remove_note(self, rel_path: str) -> None:
        if self.tombstones:
            rows = [
                dict(row)
                for row in await self.db.execute_fetchall(
                    "SELECT * FROM tasks WHERE path = ?", (rel_path,)
                )
            ]
            self.tombstones.record(rows)
        await self._delete_rows(rel_path)
        await self.db.commit()
        if self.on_note_removed:
            await self.on_note_removed(rel_path)

    async def rename_note(self, old_rel: str, new_rel: str) -> None:
        await self.remove_note(old_rel)
        content = self.store.read(new_rel)
        await self.index_note(new_rel, content)

    async def _delete_rows(self, rel_path: str) -> None:
        for table in ("notes", "links", "tags", "tasks", "media_items", "media_log"):
            col = "source" if table == "links" else "path"
            await self.db.execute(f"DELETE FROM {table} WHERE {col} = ?", (rel_path,))
        await self.db.execute("DELETE FROM notes_fts WHERE path = ?", (rel_path,))

    async def sync_all(self, force: bool = False) -> int:
        """Reconcile the index with the working tree. Returns notes (re)indexed."""
        on_disk: dict[str, float] = {}
        for info in self.store.list_notes():
            p = self.store.resolve(info.path)
            on_disk[info.path] = p.stat().st_mtime

        known: dict[str, float] = {}
        hashes: dict[str, str] = {}
        async with self.db.execute("SELECT path, mtime, hash FROM notes") as cur:
            async for row in cur:
                known[row["path"]] = row["mtime"]
                hashes[row["path"]] = row["hash"]

        for gone in set(known) - set(on_disk):
            await self.remove_note(gone)

        count = 0
        for rel_path, mtime in on_disk.items():
            if not force and known.get(rel_path) == mtime:
                continue
            try:
                content = self.store.read(rel_path)
            except Exception:
                log.exception("failed reading %s during sync", rel_path)
                continue
            # A fresh mtime over identical bytes is what a git restore or a
            # volume re-clone leaves behind — remember the new mtime, skip
            # the reindex the whole vault would otherwise pay.
            if not force and hashes.get(rel_path) == content_hash(content):
                await self.db.execute(
                    "UPDATE notes SET mtime = ? WHERE path = ?", (mtime, rel_path)
                )
                await self.db.commit()
                continue
            await self.index_note(rel_path, content, mtime)
            count += 1
        if count:
            log.info("indexed %d note(s)", count)
        return count
