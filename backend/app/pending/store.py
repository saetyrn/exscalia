"""Staged AI changes — the safety layer.

The overseer never writes to the vault working tree. Every write it proposes
becomes a row here. The user reviews each as a diff and approves (applied +
committed with the Overseer git author) or discards (dropped). Approved
deletes move into the same recoverable Trash as direct user deletions.

The overseer sees its own staged state: `effective_content` / `effective_paths`
overlay pending changes (in id order) on top of the working tree, so chained
operations (create then edit, organize across files) compose naturally.
"""
import logging
from dataclasses import dataclass

import aiosqlite

from ..index import parse as note_parse
from ..index.db import content_hash
from ..index.indexer import Indexer
from ..vault import fm
from ..vault.notes import NoteError, NoteStore
from ..vault.internal import TrashStore
from ..vault.repo import OVERSEER_AUTHOR, VaultRepo

log = logging.getLogger(__name__)

OPS = ("create", "edit", "delete", "move")


@dataclass
class PendingChange:
    id: int
    session_id: str
    op: str
    path: str
    new_path: str | None
    base_hash: str | None
    new_content: str | None
    rationale: str | None
    created_at: str


def _row_to_change(row) -> PendingChange:
    return PendingChange(
        id=row["id"],
        session_id=row["session_id"],
        op=row["op"],
        path=row["path"],
        new_path=row["new_path"],
        base_hash=row["base_hash"],
        new_content=row["new_content"],
        rationale=row["rationale"],
        created_at=row["created_at"],
    )


class PendingStore:
    def __init__(
        self,
        db: aiosqlite.Connection,
        store: NoteStore,
        vault: VaultRepo,
        indexer: Indexer,
        trash: TrashStore | None = None,
    ):
        self.db = db
        self.store = store
        self.vault = vault
        # Set during startup wiring; lets approvals record an undoable action.
        self.actions = None
        self.indexer = indexer
        self.trash = trash

    # -- staging -------------------------------------------------------------

    async def stage(
        self,
        session_id: str,
        op: str,
        path: str,
        new_path: str | None = None,
        new_content: str | None = None,
        rationale: str | None = None,
    ) -> int:
        if op not in OPS:
            raise NoteError(400, f"Unknown op: {op}")
        self.store.resolve(path)  # path validation
        if new_path:
            self.store.resolve(new_path)
        # Base = what the overseer currently sees (working tree + earlier pending
        # changes), so chained ops don't false-positive as stale, while a USER
        # edit landing after the proposal still flips the file to stale.
        base = await self.effective_content(path)
        if new_content is not None:
            # Normalize frontmatter the same way user saves are normalized.
            existing_meta = fm.parse(base)[0] if base else {}
            new_content = self.store.prepare_content(
                new_path or path,
                new_content,
                existing_created=existing_meta.get("created"),
            )
        cur = await self.db.execute(
            """INSERT INTO pending_changes
               (session_id, op, path, new_path, base_hash, new_content, rationale, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                session_id,
                op,
                path,
                new_path,
                content_hash(base) if base is not None else None,
                new_content,
                rationale,
                fm.now_iso(),
            ),
        )
        await self.db.commit()
        return cur.lastrowid

    # -- queries -------------------------------------------------------------

    async def list_changes(self) -> list[PendingChange]:
        rows = await self.db.execute_fetchall(
            "SELECT * FROM pending_changes ORDER BY id"
        )
        return [_row_to_change(r) for r in rows]

    async def get(self, change_id: int) -> PendingChange:
        cur = await self.db.execute(
            "SELECT * FROM pending_changes WHERE id = ?", (change_id,)
        )
        row = await cur.fetchone()
        if not row:
            raise NoteError(404, f"No pending change #{change_id}")
        return _row_to_change(row)

    async def count(self) -> int:
        cur = await self.db.execute("SELECT COUNT(*) AS c FROM pending_changes")
        row = await cur.fetchone()
        return row["c"]

    async def _effective_before(
        self, c: PendingChange, changes: list[PendingChange] | None = None
    ) -> str | None:
        """Content of c.path as the overseer saw it when proposing c:
        working tree + pending changes staged earlier than c.

        `changes` lets a caller iterating the whole tray hand in one
        prefetched list instead of re-querying per change."""
        content = self.store.read(c.path) if self.store.exists(c.path) else None
        for other in changes if changes is not None else await self.list_changes():
            if other.id >= c.id:
                break
            if other.op in ("create", "edit") and other.path == c.path:
                content = other.new_content
            elif other.op == "delete" and other.path == c.path:
                content = None
            elif other.op == "move":
                if other.path == c.path:
                    content = None
                elif other.new_path == c.path:
                    content = await self._content_before(other)
        return content

    async def is_stale(self, c: PendingChange) -> bool:
        """True if the file changed (user edit) since this was proposed."""
        base_now = await self._effective_before(c)
        current_hash = content_hash(base_now) if base_now is not None else None
        return current_hash != c.base_hash

    async def describe(
        self, c: PendingChange, changes: list[PendingChange] | None = None
    ) -> dict:
        """Full record for the review UI, with old/new content for diffing."""
        old = await self._effective_before(c, changes)
        # Staleness derives from the same base — computing it here instead of
        # via is_stale() halves the work, which was O(n²) across the tray.
        current_hash = content_hash(old) if old is not None else None
        return {
            "id": c.id,
            "session_id": c.session_id,
            "op": c.op,
            "path": c.path,
            "new_path": c.new_path,
            "rationale": c.rationale,
            "created_at": c.created_at,
            "stale": current_hash != c.base_hash,
            "old_content": old,
            "new_content": c.new_content,
        }

    PURGE_AFTER_DAYS = 60

    async def purge_old(self) -> int:
        """Drop proposals nobody reviewed in two months. By then the notes
        they were written against have moved on and the tray is noise; the
        rationale and content remain in the thread that proposed them."""
        from datetime import datetime, timedelta, timezone

        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=self.PURGE_AFTER_DAYS)
        ).isoformat()
        cur = await self.db.execute(
            "DELETE FROM pending_changes WHERE created_at < ?", (cutoff,)
        )
        await self.db.commit()
        return cur.rowcount or 0

    # -- overlay view (what the overseer sees) -------------------------------

    async def effective_content(self, path: str) -> str | None:
        """Working-tree content with pending changes applied in order."""
        content = self.store.read(path) if self.store.exists(path) else None
        for c in await self.list_changes():
            if c.op in ("create", "edit") and c.path == path:
                content = c.new_content
            elif c.op == "delete" and c.path == path:
                content = None
            elif c.op == "move":
                if c.path == path:
                    content = None
                elif c.new_path == path:
                    content = await self._content_before(c)
        return content

    async def _content_before(self, move: PendingChange) -> str | None:
        return self.store.read(move.path) if self.store.exists(move.path) else None

    async def effective_paths(self) -> list[str]:
        paths = {n.path for n in self.store.list_notes()}
        for c in await self.list_changes():
            if c.op == "create":
                paths.add(c.path)
            elif c.op == "delete":
                paths.discard(c.path)
            elif c.op == "move":
                paths.discard(c.path)
                if c.new_path:
                    paths.add(c.new_path)
        return sorted(paths)

    # -- resolution ----------------------------------------------------------

    async def discard(self, change_id: int) -> None:
        await self.get(change_id)
        await self.db.execute("DELETE FROM pending_changes WHERE id = ?", (change_id,))
        await self.db.commit()

    async def discard_all(self) -> int:
        n = await self.count()
        await self.db.execute("DELETE FROM pending_changes")
        await self.db.commit()
        return n

    async def _apply(self, c: PendingChange, force: bool) -> str:
        """Apply one change to the working tree (no commit). Returns summary."""
        if not force and await self.is_stale(c):
            raise NoteError(
                409,
                f"Pending change #{c.id} is stale: {c.path} was edited after it was proposed. "
                "Review the diff again, then approve with force.",
            )
        if c.op in ("create", "edit"):
            previous = self.store.read(c.path) if self.store.exists(c.path) else None
            content, _added = note_parse.normalize_task_metadata(
                c.new_content or "",
                previous_content=previous,
            )
            p = self.store.resolve(c.path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            await self.indexer.index_note(c.path, content)
            return f"{c.op} {c.path}"
        if c.op == "delete":
            if self.store.exists(c.path):
                if self.trash:
                    self.trash.trash_note(c.path)
                else:
                    self.store.delete(c.path)
                await self.indexer.remove_note(c.path)
            return f"delete {c.path}"
        if c.op == "move":
            if not c.new_path:
                raise NoteError(400, f"Pending move #{c.id} has no destination")
            self.store.rename(c.path, c.new_path)
            await self.indexer.rename_note(c.path, c.new_path)
            return f"move {c.path} -> {c.new_path}"
        raise NoteError(400, f"Unknown op {c.op}")

    async def approve(self, change_id: int, force: bool = False) -> dict:
        c = await self.get(change_id)
        summary = await self._apply(c, force)
        await self.db.execute("DELETE FROM pending_changes WHERE id = ?", (change_id,))
        await self.db.commit()
        # Scope the commit to the touched paths, and record it as an undoable
        # action. Approving from the review tray used to sweep the whole working
        # tree into an OMNIA-authored commit and produce no undo at all — the
        # safe path was less recoverable than the direct one.
        paths = [p for p in (c.path, c.new_path) if p]
        sha = await self.vault.commit_paths(
            f"OMNIA: {summary}" + (f"\n\n{c.rationale}" if c.rationale else ""),
            paths,
            author=OVERSEER_AUTHOR,
        )
        await self._record_action(c.session_id, summary, paths, sha, c.rationale)
        return {"applied": summary, "commit": sha}

    async def _record_action(
        self,
        thread_id: str | None,
        summary: str,
        paths: list[str],
        sha: str | None,
        rationale: str | None = None,
    ) -> None:
        """Give an approved proposal the same undo affordance a direct tool gets."""
        actions = getattr(self, "actions", None)
        if actions is None or not sha:
            return
        await actions.record_existing(
            thread_id=thread_id or "",
            tool="approve_pending",
            summary=summary,
            paths=paths,
            refresh=["notes", "tags", "pending"],
            commit=sha,
            rationale=rationale,
        )

    async def approve_all(self, force: bool = False) -> dict:
        changes = await self.list_changes()
        if not force:
            stale = [c.id for c in changes if await self.is_stale(c)]
            if stale:
                raise NoteError(
                    409,
                    f"Pending change(s) {stale} are stale (files edited since proposed). "
                    "Review them individually or approve with force.",
                )
        applied: list[str] = []
        touched: list[str] = []
        for c in changes:
            summary = await self._apply(c, force)
            await self.db.execute("DELETE FROM pending_changes WHERE id = ?", (c.id,))
            await self.db.commit()
            applied.append(summary)
            touched.extend(p for p in (c.path, c.new_path) if p)
        sha = None
        if applied:
            body = "\n".join(f"- {s}" for s in applied)
            sha = await self.vault.commit_all(
                f"OMNIA: apply {len(applied)} change(s)\n\n{body}",
                author=OVERSEER_AUTHOR,
            )
            # Announce only, no action record: this commit sweeps the whole
            # working tree (commit_all) and spans sessions, so an undo record
            # here could revert unrelated user edits. Making approve-all
            # undoable means moving it to scoped commit_paths first.
            actions = getattr(self, "actions", None)
            if actions is not None:
                actions.announce_paths(sorted(set(touched)))
        return {"applied": applied, "commit": sha}
