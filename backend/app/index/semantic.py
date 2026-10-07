"""Semantic index: fastembed (local ONNX embeddings) + LanceDB (embedded,
file-based). No server processes, no API cost.

Incremental: notes are re-embedded only when their content hash changes,
and then only the chunks whose text changed.
Lazy: the embedding model loads on first use to keep boot fast and RAM low
for AI-free usage.
"""
import asyncio
import logging
import threading
import time
from pathlib import Path

from ..vault import fm
from ..vault.internal import atomic_json, iso_utc, read_json
from ..vault.notes import NoteError, NoteStore
from .chunker import CHUNKER_VERSION, chunk_note
from .db import content_hash

log = logging.getLogger(__name__)

# Autosave fires shortly after every pause in typing, so one editing burst is
# a run of saves of the same note. Embedding waits for this much quiet and
# then embeds only the revision that stuck; hybrid search stays fresh within
# seconds.
EMBED_DEBOUNCE = 8.0
# ...but never longer than this after the first queued save, so a long
# writing session with short pauses still reaches semantic search.
EMBED_MAX_DELAY = 60.0
# onnxruntime defaults to every core; the container also serves requests.
EMBED_THREADS = 2
# A failed init (typically the model download on the first embed after a
# deploy) used to switch semantic search off until the next restart. It is
# tried again once this long has passed.
RETRY_AFTER = 600.0
# {model, dim, chunker_version} of whatever built the `chunks` table, beside
# it in the LanceDB directory (see _connect_sync).
STAMP = "semantic.json"


class SemanticIndex:
    def __init__(
        self,
        index_dir: Path,
        model_name: str,
        enabled: bool = True,
        debounce: float = EMBED_DEBOUNCE,
        max_delay: float = EMBED_MAX_DELAY,
    ):
        self.index_dir = index_dir
        self.model_name = model_name
        self.enabled = enabled
        self.debounce = debounce
        self.max_delay = max_delay
        self._model = None
        self._table = None
        self._db = None
        self._lock = asyncio.Lock()
        # Model load, table open/create/drop: they run on worker threads
        # (boot's backfill and a first search can race), so one at a time.
        self._init_lock = threading.RLock()
        # And one cold load at a time on the loop side: index_note takes
        # `_lock` only after `_ensure` returns, so sharing that one would
        # deadlock, and concurrent cold callers (a search, the embedding
        # queue, a Related fetch) must not each hop to their own thread.
        self._load_lock = asyncio.Lock()
        self._warming: asyncio.Task | None = None
        self._init_error: str | None = None
        self._init_failed_at = 0.0  # time.monotonic() of that failure
        self._init_failed_on: str | None = None  # the same, for people
        # Paths the last rebuild() could not embed.
        self.last_rebuild_errors: list[str] = []
        # Background embedding (see index_note_soon): the latest pending
        # operation per path, in first-queued order, and what runs it.
        self._queued: dict[str, tuple[str, str | None]] = {}
        self._timer: asyncio.TimerHandle | None = None
        self._deadline = 0.0
        self._due = False
        self._worker: asyncio.Task | None = None

    @property
    def available(self) -> bool:
        """True while working, and again once a failed init is due a retry:
        callers gate on this, so the retry has to be let through here."""
        return self.enabled and (self._init_error is None or self._retry_due())

    @property
    def error(self) -> dict:
        """Why the index is off and since when; both None while it works."""
        return {"error": self._init_error, "since": self._init_failed_on}

    @property
    def ready(self) -> bool:
        """The model is loaded and its table open, so a search answers without
        the cold load."""
        return self._model is not None and self._table is not None

    def warm(self) -> None:
        """Start the model load in the background and return at once. The
        interactive routes call this instead of waiting out the load; repeat
        calls while it runs are no-ops. One that finished without a model (a
        failed load, now due its retry; a rebuild that dropped the table)
        lets the next call start another."""
        if self.ready or not self.available:
            return
        if self._warming is not None and not self._warming.done():
            return
        self._warming = asyncio.create_task(self._ensure())

    # -- lazy init -----------------------------------------------------------

    def _retry_due(self) -> bool:
        return time.monotonic() - self._init_failed_at >= RETRY_AFTER

    def _fail(self, e: Exception) -> None:
        log.exception("semantic index unavailable")
        self._init_error = str(e) or type(e).__name__
        self._init_failed_at = time.monotonic()
        self._init_failed_on = iso_utc()

    def _clear_error(self) -> None:
        self._init_error = self._init_failed_on = None

    def _usable(self) -> bool:
        """False when off, or within RETRY_AFTER of a failed init; past it,
        the failure is forgotten and init runs again."""
        if not self.enabled:
            return False
        if self._init_error is None:
            return True
        if not self._retry_due():
            return False
        log.warning("semantic index: retrying after %s", self._init_error)
        self._clear_error()
        return True

    def _read_stamp(self) -> dict | None:
        """None for a table from before the stamp existed; {} when the stamp
        is unreadable, which matches no model."""
        try:
            stamp = read_json(self.index_dir / "lancedb" / STAMP, None)
        except NoteError:
            return {}
        return stamp if isinstance(stamp, dict) or stamp is None else {}

    def _write_stamp(self, dim: int) -> None:
        try:
            atomic_json(
                self.index_dir / "lancedb" / STAMP,
                {"model": self.model_name, "dim": dim, "chunker_version": CHUNKER_VERSION},
            )
        except OSError:  # an unstamped table is adopted on the next open
            log.warning("could not stamp the semantic index", exc_info=True)

    def _width(self) -> int:
        return self._table.schema.field("vector").type.list_size

    def _drop_sync(self) -> None:
        """Drop the vector table, then its stamp. The index is derived data:
        every vector comes back from the notes and Paper pages."""
        self._table = None
        self._db.drop_table("chunks", ignore_missing=True)
        (self.index_dir / "lancedb" / STAMP).unlink(missing_ok=True)

    def _connect_sync(self) -> bool:
        """Open the store WITHOUT loading the embedding model. Enough for
        reads of what's already indexed and for removals; returns False when
        no table exists yet (creating one needs the model for its vector
        width).

        A table built by another model or chunker is dropped HERE, not once
        the model loads: boot's backfill never gets further than this, and it
        has to find the index empty to re-embed everything."""
        with self._init_lock:
            if self._table is not None:
                return True
            import lancedb

            if self._db is None:
                self._db = lancedb.connect(self.index_dir / "lancedb")
            if "chunks" not in self._db.table_names():
                return False
            stamp = self._read_stamp()
            if stamp is not None and (
                stamp.get("model") != self.model_name
                or stamp.get("chunker_version") != CHUNKER_VERSION
            ):
                log.warning(
                    "semantic index was built by %s (chunker %s); dropping it "
                    "to re-embed with %s",
                    stamp.get("model"),
                    stamp.get("chunker_version"),
                    self.model_name,
                )
                self._drop_sync()
                return False
            self._table = self._db.open_table("chunks")
            if stamp is None:
                self._write_stamp(self._width())
            return True

    def _create_sync(self) -> None:
        import pyarrow as pa

        schema = pa.schema(
            [
                pa.field("id", pa.string()),
                pa.field("path", pa.string()),
                pa.field("heading", pa.string()),
                pa.field("text", pa.string()),
                pa.field("hash", pa.string()),
                pa.field("vector", pa.list_(pa.float32(), self._dim)),
            ]
        )
        self._table = self._db.create_table("chunks", schema=schema)
        self._write_stamp(self._dim)

    def _load_model_sync(self) -> None:
        if self._model is not None:
            return
        from fastembed import TextEmbedding

        model = TextEmbedding(model_name=self.model_name, threads=EMBED_THREADS)
        self._dim = len(next(iter(model.embed(["probe"]))))
        self._model = model

    def _ensure_sync(self):
        with self._init_lock:
            if self._model is not None and self._table is not None:
                return
            self._load_model_sync()
            if self._connect_sync():
                if self._width() == self._dim:
                    return
                # Only an unstamped table can get here: it was adopted as
                # built by the running model, and its width says otherwise.
                log.warning(
                    "semantic index holds %d-dim vectors, %s makes %d; dropping it",
                    self._width(),
                    self.model_name,
                    self._dim,
                )
                self._drop_sync()
            self._create_sync()

    async def _ensure(self) -> bool:
        if not self._usable():
            return False
        if self.ready:
            return True
        # One load at a time. Concurrent cold callers used to each build their
        # own model in their own thread, and could race create_table into a
        # permanent error.
        async with self._load_lock:
            if not self._usable():
                return False
            if self.ready:
                return True
            try:
                await asyncio.to_thread(self._ensure_sync)
                return True
            except Exception as e:
                self._fail(e)
                return False

    async def stored_hashes(self) -> dict[str, str] | None:
        """path → content hash for everything indexed, from ONE column scan.

        None when the index is off or broken; {} when nothing is indexed yet.
        Never loads the embedding model — this is how boot decides whether
        the model needs to load at all."""
        if not self._usable():
            return None

        def _scan() -> dict[str, str]:
            with self._init_lock:
                if not self._connect_sync():
                    return {}
                table = self._table
            columns = table.to_arrow().select(["path", "hash"])
            return dict(
                zip(
                    columns.column("path").to_pylist(),
                    columns.column("hash").to_pylist(),
                )
            )

        try:
            return await asyncio.to_thread(_scan)
        except Exception as e:
            self._fail(e)
            return None

    # -- indexing ------------------------------------------------------------

    def _index_sync(self, rel_path: str, content: str):
        chash = content_hash(content)
        where = f"path = '{_esc(rel_path)}'"
        stored = self._table.search().where(where).select(["text", "hash", "vector"]).to_list()
        if stored and stored[0]["hash"] == chash:
            return
        meta, body = fm.parse(content)
        title = str(meta.get("title") or Path(rel_path).stem)
        chunks = chunk_note(title, body)
        # A save usually changes one section: sections whose text is unchanged
        # keep their stored vector, and only new text goes through the model.
        vectors = {r["text"]: r["vector"] for r in stored}
        fresh = list(dict.fromkeys(c.text for c in chunks if c.text not in vectors))
        if fresh:
            vectors.update(zip(fresh, self._model.embed(fresh)))
        self._table.delete(where)
        if not chunks:
            return
        rows = [
            {
                "id": f"{rel_path}#{c.index}",
                "path": rel_path,
                "heading": c.heading,
                "text": c.text,
                "hash": chash,
                "vector": [float(x) for x in vectors[c.text]],
            }
            for c in chunks
        ]
        self._table.add(rows)

    async def index_note(self, rel_path: str, content: str):
        if rel_path.startswith("templates/"):
            return
        if not await self._ensure():
            return
        async with self._lock:
            try:
                await asyncio.to_thread(self._index_sync, rel_path, content)
            except Exception:
                log.exception("semantic index failed for %s", rel_path)

    async def remove_note(self, rel_path: str):
        """Drop a path's vectors. Needs the table only, never the model: a
        delete right after a restart must not load it, and a model that
        failed to load must not keep a deleted note in the results."""
        if not self.enabled:
            return

        def _remove() -> None:
            with self._init_lock:
                if not self._connect_sync():
                    return
                table = self._table
            table.delete(f"path = '{_esc(rel_path)}'")

        async with self._lock:
            try:
                await asyncio.to_thread(_remove)
            except Exception:
                log.exception("semantic removal failed for %s", rel_path)

    async def backfill(
        self, store, paper_documents: list[tuple[str, str]] | None = None
    ) -> None:
        """Boot: embed every note and Paper page whose stored hash is stale,
        and drop the vectors of paths that no longer exist.

        One column scan decides what needs embedding, so an up-to-date index
        loads NEITHER the embedding model nor a single vector. Removals are
        healed here as well as additions: the queue dies with the process
        (a deploy SIGKILLs it). Without `paper_documents` nothing is removed,
        since a page's vectors could not be told from an orphan's."""
        stored = await self.stored_hashes()
        if stored is None:
            return
        live: set[str] = set()
        for info in store.list_notes():
            if info.path.startswith("templates/"):
                continue
            live.add(info.path)
            try:  # one unreadable note must not keep every later one out
                content = store.read(info.path)
                if content_hash(content) != stored.get(info.path):
                    await self.index_note(info.path, content)
            except Exception:
                log.exception("semantic backfill failed for %s", info.path)
        if paper_documents is None:
            return
        for path, document in paper_documents:
            live.add(path)
            if content_hash(document) != stored.get(path):
                await self.index_note(path, document)
        for gone in sorted(set(stored) - live):
            if not store.exists(gone):  # not created since the listing
                await self.remove_note(gone)

    # -- background indexing --------------------------------------------------
    # Embedding is a search-freshness concern, not a durability one: nothing
    # reads a vector synchronously after a save, and the boot backfill
    # re-embeds anything whose stored hash is stale. So writes must never wait
    # on it — before this, the first note write after a restart (typically the
    # morning's daily-note press) stalled the whole request on the lazy
    # embedding-model load.
    #
    # Saves only record the latest operation per path and (re)arm a debounce
    # timer; one worker then runs them in first-queued order. Intermediate
    # revisions are never embedded, and a single worker means an older
    # revision can never land after a newer one.

    def _queue(self, rel_path: str, op: str, content: str | None) -> None:
        self._queued[rel_path] = (op, content)
        loop = asyncio.get_running_loop()
        now = loop.time()
        if self._timer is None:
            self._deadline = now + self.max_delay
        else:
            self._timer.cancel()
        self._timer = loop.call_at(min(now + self.debounce, self._deadline), self._flush)

    def _flush(self) -> None:
        """Hand everything queued so far to the worker (timer or drain)."""
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._due = True
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._work())

    async def _work(self) -> None:
        while self._due:
            self._due = False
            batch, self._queued = self._queued, {}
            for rel_path, (op, content) in batch.items():
                if rel_path in self._queued:
                    continue  # superseded while this batch ran; that one runs next
                try:
                    if op == "index":
                        await self.index_note(rel_path, content)
                    else:
                        await self.remove_note(rel_path)
                except Exception:
                    log.exception("background embedding failed for %s", rel_path)

    async def index_note_soon(self, rel_path: str, content: str) -> None:
        self._queue(rel_path, "index", content)

    async def remove_note_soon(self, rel_path: str) -> None:
        self._queue(rel_path, "remove", None)

    async def drain(self) -> None:
        """Run queued background embeddings now and wait for them (shutdown,
        tests)."""
        while self._queued or self._timer is not None or (
            self._worker is not None and not self._worker.done()
        ):
            self._flush()
            await self._worker

    def _reset_sync(self) -> None:
        with self._init_lock:
            self._load_model_sync()
            if self._db is None:
                import lancedb

                self._db = lancedb.connect(self.index_dir / "lancedb")
            self._drop_sync()
            self._create_sync()

    async def rebuild(self, notes: list[tuple[str, str]]) -> int:
        """Full reindex from (path, content) pairs into a fresh table. Returns
        the chunk count; `last_rebuild_errors` names what failed to embed.

        This is the manual way back from a broken index: it forgets a failed
        init instead of waiting out RETRY_AFTER, and it never reads the old
        table, so a corrupt or wrong-width one is dropped like any other."""
        self.last_rebuild_errors = []
        if not self.enabled:
            return 0
        self._clear_error()
        async with self._lock:
            try:
                await asyncio.to_thread(self._reset_sync)
            except Exception as e:
                self._fail(e)
                return 0
            for path, content in notes:
                if path.startswith("templates/"):
                    continue
                try:
                    await asyncio.to_thread(self._index_sync, path, content)
                except Exception:
                    log.exception("semantic rebuild failed for %s", path)
                    self.last_rebuild_errors.append(path)
            return await asyncio.to_thread(self._table.count_rows)

    async def optimize(self):
        """Compact fragments and drop old table versions.

        Per-note delete+add leaves one fragment and one version file per
        write, and nothing garbage-collects them — a 25-note vault had 606
        version files before this existed. Runs from the maintenance loop;
        skipped entirely while the index is idle/unloaded."""
        if self._table is None:  # never force the model to load just for GC
            return
        async with self._lock:
            def _optimize():
                from datetime import timedelta

                self._table.optimize(cleanup_older_than=timedelta(days=1))

            try:
                await asyncio.to_thread(_optimize)
            except Exception:
                log.warning("lancedb optimize failed", exc_info=True)

    # -- search --------------------------------------------------------------

    async def search(
        self,
        query: str,
        k: int = 8,
        path_prefix: str | list[str] | None = None,
        exclude_prefix: str | list[str] | None = None,
        include_paper: bool = False,
    ) -> list[dict]:
        """Nearest chunks, optionally scoped to (or away from) folder prefixes.

        The scoping is a prefilter on the `path` column, not a slice of the
        top-k afterwards: filtering after the fact returns nothing whenever the
        wanted folder's notes rank below k, which is exactly when a scope is
        worth asking for.

        Paper transcripts share the table under their page directory, which is
        not a note path. Only a caller that maps those hits itself asks for
        them with `include_paper`; everyone else gets notes.
        """
        if not await self._ensure():
            return []
        where = search_filter(path_prefix, exclude_prefix, include_paper)

        def _search():
            qv = [float(x) for x in next(iter(self._model.embed([query])))]
            builder = self._table.search(qv).limit(k).select(["path", "heading", "text"])
            if where:
                builder = builder.where(where)
            res = builder.to_list()
            return [
                {
                    "path": r["path"],
                    "heading": r["heading"],
                    "text": r["text"],
                    "score": round(1.0 / (1.0 + r.get("_distance", 0.0)), 4),
                }
                for r in res
            ]

        return await asyncio.to_thread(_search)


def _esc(s: str) -> str:
    return s.replace("'", "''")


def _like(prefix: str) -> str:
    """A prefix as a LIKE pattern body. `%` and `_` are wildcards, so a folder
    literally named `School_x` would otherwise also match `School/x` — escape
    them (and the escape character) rather than silently over-matching."""
    return _esc(prefix).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def prefix_list(value: str | list[str] | None) -> list[str]:
    if value is None:
        return []
    values = value if isinstance(value, (list, tuple)) else [value]
    return [p for p in (str(v).strip() for v in values) if p]


def search_filter(
    path_prefix: str | list[str] | None,
    exclude_prefix: str | list[str] | None,
    include_paper: bool = False,
) -> str:
    """The predicate `search` applies: the caller's scope, plus Paper pages
    kept out unless asked for."""
    if not include_paper:
        exclude_prefix = [*prefix_list(exclude_prefix), f"{NoteStore.PAPER_DIR}/"]
    return path_filter(path_prefix, exclude_prefix)


def path_filter(
    path_prefix: str | list[str] | None, exclude_prefix: str | list[str] | None
) -> str:
    """Folder scoping as a LanceDB (DataFusion SQL) predicate over `path`.

    Includes are OR'd together, excludes are AND'd on top — "anything under
    School/ or Work/, but nothing under School/Archive/".
    """
    clauses: list[str] = []
    included = [f"path LIKE '{_like(p)}%' ESCAPE '\\'" for p in prefix_list(path_prefix)]
    if included:
        clauses.append("(" + " OR ".join(included) + ")")
    clauses += [
        f"path NOT LIKE '{_like(p)}%' ESCAPE '\\'" for p in prefix_list(exclude_prefix)
    ]
    return " AND ".join(clauses)
