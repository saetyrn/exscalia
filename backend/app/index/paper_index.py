"""The rebuildable index over ``paper/``: search, counts and the change feed.

Everything here is a cache. ``paper/<Folder>/<Title>.paper`` and the JSON files
inside it stay the source of truth, and :meth:`PaperIndex.sync_all` can rebuild
every row from them — which is what boot does, and what the OMNIA
``rebuild_search_index`` tool does on demand.

Three jobs:

* **Search.** ``paper_fts`` holds the recognised handwriting (and any AI
  transcript) so a typed query finds the page it was written on. The web
  viewer, ``/api/paper/search`` and OMNIA's ``search_handwriting`` all come
  through :meth:`search`.
* **The change feed.** ``PaperStore`` stamps a monotonic ``seq`` into every
  ``notebook.json``/``page.json`` it writes; those numbers are mirrored into
  the tables so ``GET /api/paper/changes`` is one indexed query instead of a
  walk over every page on disk. ``api/paper.changes_since`` keeps the walking
  version as the fallback and as the definition of correct behaviour — the two
  answer identically for the same vault.
* **Semantic reach.** Each transcribed page is handed to the vector index as a
  tiny document keyed by its page directory, so "what did I write about the
  budget" finds handwriting as readily as it finds markdown.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from ..vault.internal import read_json
from ..vault.notes import NoteError
from ..paper_store import PaperStore, paper_page_url
from .db import write_transaction
from .highlight import HIGHLIGHT_CLOSE, HIGHLIGHT_OPEN, safe_result
from .thread_index import fts_query

log = logging.getLogger(__name__)

# Columns the change feed reports for a page, in the shape the iPad expects.
_PAGE_FEED_COLUMNS = (
    "path, notebook, page_id, revision, render_revision, text_revision, "
    "modified, seq"
)


def page_view_url(page_dir: str) -> str:
    """Where the web viewer shows this page.

    The notebook path already starts with ``paper/`` and the route is
    ``/paper/<rest>``, so the prefix is dropped exactly once.
    """
    notebook, _, page_id = page_dir.rpartition("/pages/")
    return f"/paper/{notebook.removeprefix('paper/')}#{page_id}"


class PaperIndex:
    """Async over the app's single aiosqlite connection, like ``Indexer``.

    Boot hands it the Indexer's ``write_lock``: both write that one connection,
    and statements from two writers must never share a transaction. The lock
    covers this class's own statements only — never the semantic hand-off.
    """

    def __init__(self, db, store, semantic=None, write_lock=None):
        self.db = db
        self.store = store
        self.semantic = semantic
        self.write_lock = write_lock if write_lock is not None else asyncio.Lock()
        # Held by a route from a write taking its seq until that change is in
        # the tables, and by the feed while it reads: a cursor never covers a
        # change the feed has not reported. Its own lock, not ``write_lock``:
        # the index hooks run inside it and take ``write_lock`` for their
        # statements. A route takes it while holding the vault's git lock and
        # keeps it after letting that go, so nothing holding it waits on git.
        self.feed_lock = asyncio.Lock()

    # -- semantic hand-off ---------------------------------------------------
    #
    # The vector index treats a path as opaque (only `templates/` is special to
    # it), so a page directory is a perfectly good document id. Every call is
    # guarded: the semantic index is optional at runtime (no embedding backend
    # configured) and absent entirely in tests that build a bare state.

    def _semantic_call(self, name: str):
        fn = getattr(self.semantic, name, None) if self.semantic is not None else None
        return fn if callable(fn) else None

    @staticmethod
    def semantic_document(notebook_title: str, page_title: str, transcript: str) -> str:
        """The little markdown document a page is embedded as."""
        return f"---\ntitle: {notebook_title} · {page_title}\n---\n\n{transcript}"

    async def _semantic_index(
        self, page_dir: str, notebook_title: str, page_title: str, transcript: str
    ) -> None:
        """Index a page's transcript, or drop it once the transcript is gone."""
        if transcript.strip():
            fn = self._semantic_call("index_note_soon")
            if fn is None:
                return
            document = self.semantic_document(notebook_title, page_title, transcript)
            try:
                await fn(page_dir, document)
            except Exception:  # pragma: no cover - the index logs its own failures
                log.exception("semantic index failed for %s", page_dir)
            return
        await self._semantic_remove(page_dir)

    async def _semantic_remove(self, page_dir: str) -> None:
        fn = self._semantic_call("remove_note_soon")
        if fn is None:
            return
        try:
            await fn(page_dir)
        except Exception:  # pragma: no cover
            log.exception("semantic removal failed for %s", page_dir)

    # -- reading the vault ---------------------------------------------------

    def _manifest(self, rel: str) -> dict | None:
        manifest = read_json(
            self.store.dir / rel / "notebook.json", None, skip_corrupt=True
        )
        return manifest if isinstance(manifest, dict) else None

    @staticmethod
    def _page_title(page: dict, position: int) -> str:
        """First line of the page's first text object, else ``Page N``.

        Handwriting has no title; a typed text box is the only thing on a page
        that a human deliberately wrote as words, so it is the best guess going.
        """
        if str(page.get("title") or "").strip():
            return str(page["title"]).strip()[:120]
        for obj in page.get("objects") or []:
            if not isinstance(obj, dict) or obj.get("type") != "text":
                continue
            text = str(obj.get("text") or obj.get("content") or "").strip()
            if text:
                return text.splitlines()[0].strip()[:120]
        return f"Page {position + 1}"

    @staticmethod
    def _transcript(text: dict | None, revision: int | None = None) -> str:
        """Recognised handwriting plus any AI reading, as one blob for FTS."""
        if not isinstance(text, dict):
            return ""
        parts = [PaperStore.effective_transcript(text, revision)]
        ai = text.get("transcript_ai")
        if isinstance(ai, dict):
            parts.append(str(ai.get("text") or "").strip())
        return "\n\n".join(part for part in parts if part)

    # -- notebooks -----------------------------------------------------------

    async def index_notebook(self, rel: str) -> None:
        """Upsert one notebook row and refresh its pages' positions."""
        try:
            rel = self.store.notebook_path(rel)
        except NoteError:
            return
        manifest = self._manifest(rel)
        if manifest is None:
            await self.remove_notebook(rel)
            return
        pages = [p for p in manifest.get("pages") or [] if isinstance(p, str)]
        async with write_transaction(self.db, self.write_lock):
            await self.db.execute("DELETE FROM paper_notebooks WHERE path = ?", (rel,))
            await self.db.execute(
                """INSERT INTO paper_notebooks(
                       path, id, title, folder, template, paper_size, cover, tags,
                       favorite, created, modified, page_count, seq
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    rel,
                    str(manifest.get("id") or ""),
                    str(manifest.get("title") or Path(rel).stem),
                    str(Path(rel).parent),
                    manifest.get("template"),
                    manifest.get("paperSize"),
                    manifest.get("cover"),
                    json.dumps([str(tag) for tag in manifest.get("tags") or []]),
                    1 if manifest.get("favorite") else 0,
                    manifest.get("created"),
                    manifest.get("modified"),
                    len(pages),
                    int(manifest.get("seq") or 0),
                ),
            )
            # A reorder moves pages without rewriting any page.json, so position
            # is refreshed from the manifest rather than left to the page indexer.
            if pages:
                await self.db.executemany(
                    "UPDATE paper_pages SET position = ? WHERE path = ?",
                    [
                        (position, f"{rel}/pages/{page_id}")
                        for position, page_id in enumerate(pages)
                    ],
                )

    async def reindex_notebook(self, rel: str) -> None:
        """The notebook row and every page under it, from disk.

        For a notebook that arrives at a path with no page rows — a rename or a
        Trash restore, both of which follow a ``remove_notebook`` — where
        :meth:`index_notebook` alone only repositions rows that do not exist.
        """
        try:
            rel = self.store.notebook_path(rel)
        except NoteError:
            return
        await self.index_notebook(rel)
        pages_dir = self.store.dir / rel / "pages"
        if not pages_dir.is_dir():
            return
        for page_dir in sorted(p for p in pages_dir.iterdir() if p.is_dir()):
            await self.index_page(f"{rel}/pages/{page_dir.name}", refresh_notebook=False)

    async def remove_notebook(self, rel: str) -> None:
        """Drop a notebook and everything under it, index and vectors alike."""
        try:
            rel = self.store.notebook_path(rel)
        except NoteError:
            return
        async with write_transaction(self.db, self.write_lock):
            rows = await self.db.execute_fetchall(
                "SELECT path FROM paper_pages WHERE notebook = ?", (rel,)
            )
            await self.db.execute("DELETE FROM paper_notebooks WHERE path = ?", (rel,))
            await self.db.execute("DELETE FROM paper_pages WHERE notebook = ?", (rel,))
            await self.db.execute("DELETE FROM paper_fts WHERE notebook = ?", (rel,))
        for row in rows:
            await self._semantic_remove(row["path"])

    # -- pages ---------------------------------------------------------------

    async def index_page(self, page_dir: str, refresh_notebook: bool = True) -> None:
        """Upsert one page from its directory (``paper/…/pages/<id>``).

        The manifest is refreshed alongside it by default: saving a page bumps
        the notebook's own ``seq`` (a new page is appended to ``pages[]``), and
        a change feed that missed that would hand the iPad a page belonging to
        a notebook it has never heard of. ``sync_all`` passes ``False`` because
        it already indexes each notebook once, before its pages.
        """
        notebook, _, page_id = str(page_dir).rpartition("/pages/")
        if not notebook or not page_id:
            return
        try:
            notebook = self.store.notebook_path(notebook)
        except NoteError:
            return
        if refresh_notebook:
            await self.index_notebook(notebook)
        rel = f"{notebook}/pages/{page_id}"
        page_json = self.store.dir / rel / "page.json"
        page = read_json(page_json, None, skip_corrupt=True)
        if not isinstance(page, dict):
            await self.remove_page(rel)
            return
        manifest = self._manifest(notebook) or {}
        notebook_title = str(manifest.get("title") or Path(notebook).stem)
        order = [p for p in manifest.get("pages") or [] if isinstance(p, str)]
        position = order.index(page_id) if page_id in order else len(order)
        title = self._page_title(page, position)
        transcript = self._transcript(
            read_json(self.store.dir / rel / "text.json", None, skip_corrupt=True),
            int(page.get("revision") or 0),
        )
        try:
            mtime = page_json.stat().st_mtime
        except OSError:  # pragma: no cover - the file was read a line ago
            mtime = 0.0

        async with write_transaction(self.db, self.write_lock):
            await self._delete_page_rows(rel)
            await self.db.execute(
                """INSERT INTO paper_pages(
                       path, notebook, page_id, title, position, revision,
                       render_revision, text_revision, transcript, conflict_of,
                       created, modified, mtime, seq
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    rel,
                    notebook,
                    page_id,
                    title,
                    position,
                    int(page.get("revision") or 0),
                    int(page.get("render_revision") or 0),
                    int(page.get("text_revision") or 0),
                    transcript,
                    page.get("conflict_of"),
                    page.get("created"),
                    page.get("modified"),
                    mtime,
                    int(page.get("seq") or 0),
                ),
            )
            await self.db.execute(
                "INSERT INTO paper_fts(path, notebook, title, text) VALUES (?,?,?,?)",
                (rel, notebook, title, transcript),
            )
        await self._semantic_index(rel, notebook_title, title, transcript)

    async def remove_page(self, page_dir: str) -> None:
        notebook, _, page_id = str(page_dir).rpartition("/pages/")
        if not notebook or not page_id:
            return
        try:
            rel = f"{self.store.notebook_path(notebook)}/pages/{page_id}"
        except NoteError:
            return
        async with write_transaction(self.db, self.write_lock):
            await self._delete_page_rows(rel)
        await self._semantic_remove(rel)

    async def _delete_page_rows(self, rel: str) -> None:
        await self.db.execute("DELETE FROM paper_pages WHERE path = ?", (rel,))
        await self.db.execute("DELETE FROM paper_fts WHERE path = ?", (rel,))

    # -- reconciliation ------------------------------------------------------

    async def sync_all(self, force: bool = False) -> int:
        """Reconcile the tables with ``paper/``. Returns pages (re)indexed.

        A page is reindexed when any of ``(revision, text_revision, mtime)``
        moved: the first two cover a write the index missed, the mtime covers a
        file restored underneath us by git.
        """
        await self._check_seq_rollback()
        known: dict[str, tuple[int, int, float]] = {}
        async with self.db.execute(
            "SELECT path, revision, text_revision, mtime FROM paper_pages"
        ) as cur:
            async for row in cur:
                known[row["path"]] = (
                    int(row["revision"] or 0),
                    int(row["text_revision"] or 0),
                    float(row["mtime"] or 0),
                )
        known_notebooks = {
            row["path"]
            for row in await self.db.execute_fetchall(
                "SELECT path FROM paper_notebooks"
            )
        }

        on_disk_notebooks = set(self.store.notebook_dirs())
        seen_pages: set[str] = set()
        count = 0
        for rel in sorted(on_disk_notebooks):
            await self.index_notebook(rel)
            pages_dir = self.store.dir / rel / "pages"
            if not pages_dir.is_dir():
                continue
            for page_dir in sorted(p for p in pages_dir.iterdir() if p.is_dir()):
                page_rel = f"{rel}/pages/{page_dir.name}"
                seen_pages.add(page_rel)
                page = read_json(page_dir / "page.json", None, skip_corrupt=True)
                if not isinstance(page, dict):
                    continue
                try:
                    mtime = (page_dir / "page.json").stat().st_mtime
                except OSError:  # pragma: no cover
                    mtime = 0.0
                fingerprint = (
                    int(page.get("revision") or 0),
                    int(page.get("text_revision") or 0),
                    mtime,
                )
                if not force and known.get(page_rel) == fingerprint:
                    continue
                await self.index_page(page_rel, refresh_notebook=False)
                count += 1

        for gone in known_notebooks - on_disk_notebooks:
            await self.remove_notebook(gone)
        for gone in set(known) - seen_pages:
            await self.remove_page(gone)
        if count:
            log.info("indexed %d Paper page(s)", count)
        return count

    async def _check_seq_rollback(self) -> None:
        """Rotate the feed's epoch when seq.json fell behind the tables.

        seq.json lives in the vault and rewinds with it; this database does
        not, so the highest seq it holds is the witness. Read before the walk
        reindexes the rolled-back files and lowers it.
        """
        row = await (
            await self.db.execute(
                "SELECT MAX(seq) FROM (SELECT seq FROM paper_pages "
                "UNION ALL SELECT seq FROM paper_notebooks)"
            )
        ).fetchone()
        indexed = int(row[0] or 0) if row else 0
        if not indexed:
            return
        current = int(self.store.seq_state().get("seq") or 0)
        if indexed > current:
            state = self.store.rotate_epoch(indexed)
            log.info(
                "Paper seq.json is behind the index (%d < %d); new feed epoch %s",
                current,
                indexed,
                state["epoch"],
            )

    # -- counts --------------------------------------------------------------

    async def count_pages(self) -> int:
        row = await (await self.db.execute("SELECT COUNT(*) FROM paper_pages")).fetchone()
        return int(row[0] if row else 0)

    async def count_transcribed(self) -> int:
        """Pages that carry any recognised or AI text — the ones search can find."""
        row = await (
            await self.db.execute(
                "SELECT COUNT(*) FROM paper_pages WHERE transcript <> ''"
            )
        ).fetchone()
        return int(row[0] if row else 0)

    # -- search --------------------------------------------------------------

    async def search(
        self, query: str, limit: int = 20, notebook: str | None = None
    ) -> list[dict]:
        """FTS5 over the transcripts, newest-relevance first.

        The query is normalised the way conversation recall normalises one
        (each word quoted and OR-ed) so typed punctuation cannot turn into FTS
        syntax and so a multi-word question still matches; a literal phrase is
        the fallback if even that will not parse.
        """
        query = (query or "").strip()
        if not query:
            return []
        notebook_filter = None
        if notebook:
            try:
                notebook_filter = self.store.notebook_path(notebook)
            except NoteError:
                return []

        async def run(match: str):
            conditions = ["paper_fts MATCH ?"]
            params: list[object] = [match]
            if notebook_filter:
                conditions.append("p.notebook = ?")
                params.append(notebook_filter)
            return await self.db.execute_fetchall(
                f"""SELECT f.path, p.notebook, p.page_id, p.position, p.title,
                           nb.title AS notebook_title,
                           snippet(paper_fts, 3, ?, ?, ' … ', 20) AS snippet
                    FROM paper_fts f
                    JOIN paper_pages p ON p.path = f.path
                    LEFT JOIN paper_notebooks nb ON nb.path = p.notebook
                    WHERE {' AND '.join(conditions)}
                    ORDER BY rank LIMIT ?""",
                (HIGHLIGHT_OPEN, HIGHLIGHT_CLOSE, *params, max(1, int(limit))),
            )

        try:
            rows = await run(fts_query(query))
        except Exception:
            try:
                rows = await run('"' + query.replace('"', '""') + '"')
            except Exception:
                log.exception("paper search failed for %r", query)
                return []

        results = []
        for row in rows:
            data = safe_result(dict(row), query)
            results.append(
                {
                    "path": data["path"],
                    "notebook": data["notebook"],
                    "notebook_title": data.get("notebook_title")
                    or Path(data["notebook"]).stem,
                    "page_id": data["page_id"],
                    "position": int(data.get("position") or 0),
                    "title": data.get("title") or "",
                    "snippet": data["snippet"],
                    "highlights": data["highlights"],
                    # Keep url for existing web clients; durable_url is the
                    # rename-proof form new clients should copy.
                    "url": page_view_url(data["path"]),
                    "durable_url": paper_page_url(
                        str((self._manifest(data["notebook"]) or {}).get("id") or ""),
                        str(data["page_id"]),
                    ),
                }
            )
        return results

    # -- change feed ---------------------------------------------------------

    async def changes_since(self, epoch: str | None = None, since: int = 0) -> dict:
        """The iPad's sync cursor, answered from the tables.

        Identical in meaning to ``api.paper.changes_since``: the ``seq`` values
        the index carries were copied from the same JSON files that function
        reads. Only the selection is indexed — the notebook payloads are still
        built by the store so both paths return byte-identical summaries.

        Under ``feed_lock``: a route takes its seq before its row reaches the
        tables, and a cursor read in between would skip that change for good.
        """
        async with self.feed_lock:
            return await self._changes_since(epoch, since)

    async def _changes_since(self, epoch: str | None, since: int) -> dict:
        state = self.store.seq_state()
        current_epoch = state["epoch"]
        seq = int(state.get("seq") or 0)
        pruned = int(state.get("pruned") or 0)
        since = max(0, int(since or 0))
        full = bool(epoch and epoch != current_epoch) or since <= 0 or since < pruned

        if full:
            notebook_rows = await self.db.execute_fetchall(
                "SELECT path FROM paper_notebooks ORDER BY path"
            )
            page_rows = await self.db.execute_fetchall(
                f"SELECT {_PAGE_FEED_COLUMNS} FROM paper_pages ORDER BY seq"
            )
        else:
            notebook_rows = await self.db.execute_fetchall(
                "SELECT path FROM paper_notebooks WHERE seq > ? ORDER BY path",
                (since,),
            )
            page_rows = await self.db.execute_fetchall(
                f"SELECT {_PAGE_FEED_COLUMNS} FROM paper_pages WHERE seq > ? "
                "ORDER BY seq",
                (since,),
            )

        notebooks = []
        for row in notebook_rows:
            try:
                notebooks.append(self.store.summary(row["path"]))
            except NoteError:
                # Deleted between the sync and now; the tombstone reports it.
                continue
        pages = [
            {
                "path": row["path"],
                "notebook": row["notebook"],
                "id": row["page_id"],
                "revision": int(row["revision"] or 0),
                "render_revision": int(row["render_revision"] or 0),
                "text_revision": int(row["text_revision"] or 0),
                "modified": row["modified"],
                "seq": int(row["seq"] or 0),
            }
            for row in page_rows
        ]
        return {
            "epoch": current_epoch,
            "seq": seq,
            "full": full,
            "notebooks": notebooks,
            "pages": pages,
            "deleted": [] if full else self.store.tombstones_since(since),
        }

    # -- semantic rebuild ----------------------------------------------------

    async def semantic_documents(self) -> list[tuple[str, str]]:
        """``(page_dir, document)`` for every transcribed page.

        The rebuild paths feed this to ``SemanticIndex.rebuild`` alongside the
        notes, so a full reindex does not silently drop handwriting.
        """
        rows = await self.db.execute_fetchall(
            """SELECT p.path, p.title, p.transcript, nb.title AS notebook_title,
                      p.notebook
               FROM paper_pages p
               LEFT JOIN paper_notebooks nb ON nb.path = p.notebook
               WHERE p.transcript <> ''
               ORDER BY p.path"""
        )
        return [
            (
                row["path"],
                self.semantic_document(
                    row["notebook_title"] or Path(row["notebook"]).stem,
                    row["title"] or "",
                    row["transcript"],
                ),
            )
            for row in rows
        ]
