"""Semantic index: fastembed (local ONNX embeddings) + LanceDB (embedded,
file-based). No server processes, no API cost.

Incremental: notes are re-embedded only when their content hash changes.
Lazy: the embedding model loads on first use to keep boot fast and RAM low
for AI-free usage.
"""
import asyncio
import logging
from pathlib import Path

from ..vault import fm
from .chunker import chunk_note
from .db import content_hash

log = logging.getLogger(__name__)


class SemanticIndex:
    def __init__(self, index_dir: Path, model_name: str, enabled: bool = True):
        self.index_dir = index_dir
        self.model_name = model_name
        self.enabled = enabled
        self._model = None
        self._table = None
        self._db = None
        self._lock = asyncio.Lock()
        self._init_error: str | None = None
        # Tail of the background embedding chain (see index_note_soon).
        self._pending: asyncio.Task | None = None

    @property
    def available(self) -> bool:
        return self.enabled and self._init_error is None

    # -- lazy init -----------------------------------------------------------

    def _connect_sync(self) -> bool:
        """Open the store WITHOUT loading the embedding model. Enough for
        reads of what's already indexed; returns False when no table exists
        yet (creating one needs the model for its vector width)."""
        if self._table is not None:
            return True
        import lancedb

        if self._db is None:
            self._db = lancedb.connect(self.index_dir / "lancedb")
        if "chunks" in self._db.table_names():
            self._table = self._db.open_table("chunks")
            return True
        return False

    def _ensure_sync(self):
        if self._model is not None:
            return
        from fastembed import TextEmbedding

        self._model = TextEmbedding(model_name=self.model_name)
        self._dim = len(next(iter(self._model.embed(["probe"]))))
        if not self._connect_sync():
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

    async def _ensure(self) -> bool:
        if not self.enabled:
            return False
        if self._init_error:
            return False
        try:
            await asyncio.to_thread(self._ensure_sync)
            return True
        except Exception as e:
            log.exception("semantic index unavailable")
            self._init_error = str(e)
            return False

    async def stored_hashes(self) -> dict[str, str] | None:
        """path → content hash for everything indexed, from ONE column scan.

        None when the index is off or broken; {} when nothing is indexed yet.
        Never loads the embedding model — this is how boot decides whether
        the model needs to load at all."""
        if not self.enabled or self._init_error:
            return None
        try:
            if not await asyncio.to_thread(self._connect_sync):
                return {}

            def _scan() -> dict[str, str]:
                table = self._table.to_arrow().select(["path", "hash"])
                return dict(
                    zip(
                        table.column("path").to_pylist(),
                        table.column("hash").to_pylist(),
                    )
                )

            return await asyncio.to_thread(_scan)
        except Exception as e:
            log.exception("semantic index unavailable")
            self._init_error = str(e)
            return None

    # -- indexing ------------------------------------------------------------

    def _note_hash_sync(self, rel_path: str) -> str | None:
        rows = self._table.search().where(f"path = '{_esc(rel_path)}'").limit(1).to_list()
        return rows[0]["hash"] if rows else None

    def _index_sync(self, rel_path: str, content: str):
        chash = content_hash(content)
        if self._note_hash_sync(rel_path) == chash:
            return
        meta, body = fm.parse(content)
        title = str(meta.get("title") or Path(rel_path).stem)
        chunks = chunk_note(title, body)
        self._table.delete(f"path = '{_esc(rel_path)}'")
        if not chunks:
            return
        vectors = list(self._model.embed([c.text for c in chunks]))
        rows = [
            {
                "id": f"{rel_path}#{c.index}",
                "path": rel_path,
                "heading": c.heading,
                "text": c.text,
                "hash": chash,
                "vector": [float(x) for x in v],
            }
            for c, v in zip(chunks, vectors)
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
        if not await self._ensure():
            return
        async with self._lock:
            await asyncio.to_thread(self._table.delete, f"path = '{_esc(rel_path)}'")

    # -- background indexing --------------------------------------------------
    # Embedding is a search-freshness concern, not a durability one: nothing
    # reads a vector synchronously after a save, and the boot backfill
    # re-embeds anything whose stored hash is stale. So writes must never wait
    # on it — before this, the first note write after a restart (typically the
    # morning's daily-note press) stalled the whole request on the lazy
    # embedding-model load.

    def _spawn(self, coro) -> None:
        # Chain onto the previous task so notes embed in write order — two
        # to_thread hops racing could otherwise land an older revision last.
        prev, task_coro = self._pending, coro

        async def run():
            if prev is not None:
                try:
                    await prev
                except Exception:
                    pass  # already logged by drain/its own handler; keep the chain moving
            await task_coro

        self._pending = asyncio.create_task(run())

    async def index_note_soon(self, rel_path: str, content: str) -> None:
        self._spawn(self.index_note(rel_path, content))

    async def remove_note_soon(self, rel_path: str) -> None:
        self._spawn(self.remove_note(rel_path))

    async def drain(self) -> None:
        """Wait out queued background embeddings (shutdown, tests)."""
        while self._pending is not None:
            tail = self._pending
            try:
                await tail
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("background embedding failed")
            if self._pending is tail:
                self._pending = None

    async def rebuild(self, notes: list[tuple[str, str]]) -> int:
        """Full reindex from (path, content) pairs. Returns chunk count."""
        if not await self._ensure():
            return 0
        async with self._lock:
            await asyncio.to_thread(self._table.delete, "true")
            for path, content in notes:
                if path.startswith("templates/"):
                    continue
                await asyncio.to_thread(self._index_sync, path, content)
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

    async def search(self, query: str, k: int = 8) -> list[dict]:
        if not await self._ensure():
            return []

        def _search():
            qv = [float(x) for x in next(iter(self._model.embed([query])))]
            res = (
                self._table.search(qv)
                .limit(k)
                .select(["path", "heading", "text"])
                .to_list()
            )
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
