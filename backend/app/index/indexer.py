"""Keeps the SQLite metadata index in sync with the vault working tree."""
import asyncio
import logging
from pathlib import Path

import aiosqlite

from .. import media_items, task_boards
from ..vault import fm
from ..vault.notes import NoteStore
from ..vault.internal import TaskTombstones
from . import parse
from .db import content_hash, write_transaction
from .links import LinkResolver, names_note

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
        # Serialises every multi-statement write on the app's one aiosqlite
        # connection (see db.write_transaction). PaperIndex and PendingStore
        # are handed this same lock at boot.
        self.write_lock = asyncio.Lock()
        # Set by the semantic index (phase 3) to receive change notifications.
        self.on_note_changed = None
        self.on_note_removed = None

    async def index_note(
        self,
        rel_path: str,
        content: str,
        mtime: float | None = None,
        *,
        resolve_links: bool = True,
    ) -> None:
        """(Re)index one note. `resolve_links=False` leaves its links'
        resolved_path NULL for a later _reresolve_all — sync_all's bulk mode."""
        if mtime is None:
            mtime = self._disk_mtime(rel_path)
        async with write_transaction(self.db, self.write_lock):
            await self._index_locked(rel_path, content, mtime, resolve_links=resolve_links)
        # Outside the lock: the hook only queues the embedding, and nothing
        # that waits on the model may hold up the next save.
        if self.on_note_changed:
            await self.on_note_changed(rel_path, content)

    async def remove_note(self, rel_path: str, *, resolve_links: bool = True) -> None:
        async with write_transaction(self.db, self.write_lock):
            await self._remove_locked(rel_path, resolve_links=resolve_links)
        if self.on_note_removed:
            await self.on_note_removed(rel_path)

    async def rename_note(self, old_rel: str, new_rel: str) -> None:
        # One transaction: a reader of the committed index sees the note at
        # its old path or its new one, never at neither.
        content = self.store.read(new_rel)
        mtime = self._disk_mtime(new_rel)
        async with write_transaction(self.db, self.write_lock):
            await self._remove_locked(old_rel)
            await self._index_locked(new_rel, content, mtime)
        if self.on_note_removed:
            await self.on_note_removed(old_rel)
        if self.on_note_changed:
            await self.on_note_changed(new_rel, content)

    def _disk_mtime(self, rel_path: str) -> float:
        p = self.store.resolve(rel_path)
        return p.stat().st_mtime if p.exists() else 0.0

    async def _index_locked(
        self,
        rel_path: str,
        content: str,
        mtime: float,
        *,
        resolve_links: bool = True,
    ) -> None:
        """The rows for one note. Caller holds write_lock and commits."""
        meta, body = fm.parse(content)
        title = str(meta.get("title") or Path(rel_path).stem)
        # Recomputed from frontmatter on every save, so adding or removing the
        # marker line in the editor moves the note in or out of board status
        # with no other plumbing.
        board = 1 if task_boards.is_board(meta) else 0
        line_offset = parse.frontmatter_line_count(content, body)

        parsed_tasks = parse.extract_tasks(body, line_offset)
        for task in parsed_tasks:
            if task.id.startswith("legacy:"):
                task.id = f"legacy:{rel_path}:{task.line}"
        # Everything derived before the first DELETE: a note that fails to
        # parse never gets as far as touching its old rows.
        targets = parse.extract_wikilinks(body)
        tags = [(rel_path, tag) for tag in parse.extract_tags(meta, body)]
        tasks = [
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
            )
            for task in parsed_tasks
        ]
        media = None
        media_log = []
        if media_items.is_media_item(meta):
            fields = media_items.item_fields(meta, Path(rel_path).stem)
            media = (
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
            )
            media_log = [
                (
                    rel_path,
                    line_offset + entry.line,
                    entry.at,
                    entry.kind,
                    entry.value,
                    entry.text,
                )
                for entry in media_items.parse_log(body)
            ]

        old_tasks = [
            dict(row)
            for row in await self.db.execute_fetchall(
                "SELECT * FROM tasks WHERE path = ?", (rel_path,)
            )
        ]
        active_ids = {task.id for task in parsed_tasks}
        if self.tombstones:
            self.tombstones.record(
                [task for task in old_tasks if task.get("id") not in active_ids]
            )
            self.tombstones.reactivate(active_ids)

        existed = bool(
            await self.db.execute_fetchall("SELECT 1 FROM notes WHERE path = ?", (rel_path,))
        )
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
        # The resolver reads the notes table, so it is built after this note's
        # own row lands (a new note must be able to resolve a link to itself).
        resolver = (
            await self._link_resolver()
            if resolve_links and (targets or not existed)
            else None
        )
        # executemany: one thread hop per table rather than one per row, so a
        # 300-task note holds the lock for a handful of awaits.
        if targets:
            await self.db.executemany(
                "INSERT INTO links(source, target, target_lower, resolved_path)"
                " VALUES (?,?,?,?)",
                [
                    (
                        rel_path,
                        target,
                        target.lower(),
                        resolver.resolve(target) if resolver else None,
                    )
                    for target in targets
                ],
            )
        if resolver and not existed:
            # A new note can take over links other notes already hold: a
            # dangling [[Plan]] starts to resolve, or a shallower Plan.md
            # wins the tie from a deeper one.
            rows = await self.db.execute_fetchall("SELECT DISTINCT target FROM links")
            await self._reresolve(
                resolver, [r["target"] for r in rows if names_note(r["target"], rel_path)]
            )
        if tags:
            await self.db.executemany("INSERT INTO tags(path, tag) VALUES (?,?)", tags)
        if tasks:
            # OR IGNORE: a note whose content was copied from another note
            # (old conflict copies) can carry a task id that already exists.
            # The duplicate task is skipped rather than crashing indexing —
            # a single bad note must never take the whole server down.
            await self.db.executemany(
                """INSERT OR IGNORE INTO tasks(
                       id, path, line, meta_line, text, status,
                       created_at, started_at, completed_at, reopened_at, due, due_visible,
                       description, board, sort, reminder_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                tasks,
            )
        if media is not None:
            await self.db.execute(
                """INSERT INTO media_items(
                       path, title, alias, media_type, status, creator, year, cover,
                       rating, started, finished, total, modified
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                media,
            )
        if media_log:
            await self.db.executemany(
                "INSERT INTO media_log(path, line, at, kind, value, text)"
                " VALUES (?,?,?,?,?,?)",
                media_log,
            )

    async def _remove_locked(self, rel_path: str, *, resolve_links: bool = True) -> None:
        """Drop one note's rows. Caller holds write_lock and commits."""
        if self.tombstones:
            rows = [
                dict(row)
                for row in await self.db.execute_fetchall(
                    "SELECT * FROM tasks WHERE path = ?", (rel_path,)
                )
            ]
            self.tombstones.record(rows)
        await self._delete_rows(rel_path)
        if resolve_links:
            # Links that opened this note now open a same-name note, or nothing.
            rows = await self.db.execute_fetchall(
                "SELECT DISTINCT target FROM links WHERE resolved_path = ?", (rel_path,)
            )
            if rows:
                await self._reresolve(
                    await self._link_resolver(), [r["target"] for r in rows]
                )

    async def _link_resolver(self) -> LinkResolver:
        rows = await self.db.execute_fetchall("SELECT path FROM notes")
        return LinkResolver(r["path"] for r in rows)

    async def _reresolve(self, resolver: LinkResolver, targets) -> None:
        """Recompute resolved_path for every link row with one of `targets`.
        Resolution depends on the target text and the note list only, so rows
        are addressed by target, whichever note holds them."""
        for target in targets:
            resolved = resolver.resolve(target)
            await self.db.execute(
                "UPDATE links SET resolved_path = ? WHERE target = ? AND resolved_path IS NOT ?",
                (resolved, target, resolved),
            )

    async def _reresolve_all(self) -> None:
        """One pass over every link: fills the column after the migration and
        after a bulk sync, and heals any row that drifted."""
        async with write_transaction(self.db, self.write_lock):
            resolver = await self._link_resolver()
            rows = await self.db.execute_fetchall(
                "SELECT DISTINCT target, resolved_path FROM links"
            )
            stale = {
                r["target"] for r in rows if resolver.resolve(r["target"]) != r["resolved_path"]
            }
            if stale:
                await self._reresolve(resolver, sorted(stale))

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

        # Bulk mode: links are resolved once, against the final note list, by
        # _reresolve_all below rather than after every single note.
        for gone in set(known) - set(on_disk):
            await self.remove_note(gone, resolve_links=False)

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
                async with write_transaction(self.db, self.write_lock):
                    await self.db.execute(
                        "UPDATE notes SET mtime = ? WHERE path = ?", (mtime, rel_path)
                    )
                continue
            # One note that fails to index is skipped, never the whole sync:
            # boot awaits this before serving, and under `restart:
            # unless-stopped` an exception here is a crash loop. index_note
            # has already rolled its rows back, so the note keeps whatever
            # the index held for it before.
            try:
                await self.index_note(rel_path, content, mtime, resolve_links=False)
            except Exception:
                log.exception("failed indexing %s during sync", rel_path)
                continue
            count += 1
        await self._reresolve_all()
        if count:
            log.info("indexed %d note(s)", count)
        return count
