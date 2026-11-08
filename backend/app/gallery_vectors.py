"""What the Gallery's pictures look like, as vectors: CLIP, run locally.

CLIP puts images and short texts in one space, so the same vectors answer
"pictures that look like this one" (image to image) and "pictures of a
watercolour castle" (text to image). The two halves of the model come
through fastembed, which the semantic index already uses: no new package,
no API cost, about 0.6 GB of model files on first use, downloaded into the
same cache.

Vectors are kept in their own SQLite file beside the index, keyed by the
image's content digest (the one in its attachment name), so a picture moved
to another board, or saved to two, is embedded once, and an index rebuild
(INDEX_VERSION) never costs a re-embedding. The whole set is held in memory
as normalised float32 rows: a few thousand pictures are a few megabytes, and
a brute-force dot product over them takes milliseconds.

Beside them, each picture's hidden reading (gallery.meaning_text) as a text
vector from the notes' own model (SemanticIndex.embed_texts), keyed by a
hash of the text and stamped "text:<model>". These answer "pictures about
the same thing": two 90s game screenshots read alike whatever their colours.

Lazy and forgiving like SemanticIndex: the models load on first use, on a
worker thread; a failed load switches the store off until RETRY_AFTER has
passed, and every caller treats "off" as "no visual signal".
"""
from __future__ import annotations

import asyncio
import io
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Protocol

import numpy as np

log = logging.getLogger(__name__)

IMAGE_MODEL = "Qdrant/clip-ViT-B-32-vision"
TEXT_MODEL = "Qdrant/clip-ViT-B-32-text"
# Stamped on every row: vectors from another model are not comparable.
MODEL_ID = f"{IMAGE_MODEL}+{TEXT_MODEL}"
EMBED_THREADS = 2
RETRY_AFTER = 600.0
FILE_NAME = "gallery_vectors.sqlite3"
# Larger images are shrunk before they reach the model (it sees 224 x 224);
# decoding a 3,000-pixel JPEG whole for that is wasted memory.
PREPARE_EDGE = 512


class Embedder(Protocol):
    def embed_images(self, images: list[bytes]) -> list[list[float]]: ...

    def embed_texts(self, texts: list[str]) -> list[list[float]]: ...


def _prepared(data: bytes):
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        image.draft("RGB", (PREPARE_EDGE, PREPARE_EDGE))  # JPEG: decode small
        small = image.convert("RGB")
    small.thumbnail((PREPARE_EDGE, PREPARE_EDGE))
    return small


class ClipEmbedder:
    """The real thing: fastembed's CLIP pair, loaded on first use."""

    def __init__(self) -> None:
        from fastembed import ImageEmbedding, TextEmbedding

        self._image = ImageEmbedding(IMAGE_MODEL, threads=EMBED_THREADS)
        self._text = TextEmbedding(TEXT_MODEL, threads=EMBED_THREADS)

    def embed_images(self, images: list[bytes]) -> list[list[float]]:
        return [list(map(float, v)) for v in self._image.embed([_prepared(d) for d in images])]

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [list(map(float, v)) for v in self._text.embed(texts)]


def _unit(vector) -> np.ndarray | None:
    v = np.asarray(vector, dtype=np.float32)
    norm = float(np.linalg.norm(v))
    if not np.isfinite(norm) or norm == 0.0:
        return None
    return v / norm


class GalleryVectors:
    def __init__(
        self,
        index_dir: Path,
        enabled: bool = True,
        embedder_factory=None,
        text_embed=None,
        text_model: str = "",
    ):
        """`text_embed` is an async callable from a list of texts to their
        vectors (None while unavailable); without it readings get none."""
        self.path = Path(index_dir) / FILE_NAME
        self.enabled = enabled
        self._factory = embedder_factory or ClipEmbedder
        self._embedder: Embedder | None = None
        self._vectors: dict[str, np.ndarray] | None = None
        self._texts: dict[str, np.ndarray] = {}
        self._text_embed = text_embed
        self._text_model = f"text:{text_model}"
        self._text_lock = asyncio.Lock()
        self._init_lock = threading.Lock()
        # One embedding at a time: the laptop serves requests too.
        self._embed_lock = asyncio.Lock()
        self._init_error: str | None = None
        self._failed_at = 0.0

    # -- state ----------------------------------------------------------------

    @property
    def available(self) -> bool:
        return self.enabled and (
            self._init_error is None or time.monotonic() - self._failed_at >= RETRY_AFTER
        )

    @property
    def error(self) -> str | None:
        return self._init_error

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS vectors ("
            " digest TEXT PRIMARY KEY, model TEXT NOT NULL, data BLOB NOT NULL)"
        )
        return conn

    def _load_vectors_sync(self) -> dict[str, np.ndarray]:
        with self._init_lock:
            if self._vectors is not None:
                return self._vectors
            vectors: dict[str, np.ndarray] = {}
            try:
                conn = self._connect()
                try:
                    for digest, model, data in conn.execute(
                        "SELECT digest, model, data FROM vectors"
                    ):
                        v = _unit(np.frombuffer(data, dtype=np.float32))
                        if v is None:
                            continue
                        if model == MODEL_ID:
                            vectors[digest] = v
                        elif model == self._text_model and digest.startswith("t:"):
                            self._texts[digest[2:]] = v
                finally:
                    conn.close()
            except sqlite3.DatabaseError:
                # A rebuildable cache: set the broken file aside and start over.
                log.exception("gallery vectors unreadable; starting over")
                try:
                    self.path.rename(self.path.with_suffix(".corrupt"))
                except OSError:
                    pass
            self._vectors = vectors
            return vectors

    async def _vectors_loaded(self) -> dict[str, np.ndarray]:
        if self._vectors is not None:
            return self._vectors
        return await asyncio.to_thread(self._load_vectors_sync)

    def _embedder_sync(self) -> Embedder | None:
        with self._init_lock:
            if self._embedder is not None:
                return self._embedder
            try:
                self._embedder = self._factory()
                self._init_error = None
            except Exception as exc:
                log.exception("gallery image model unavailable")
                self._init_error = str(exc) or type(exc).__name__
                self._failed_at = time.monotonic()
                return None
            return self._embedder

    async def _ready_embedder(self) -> Embedder | None:
        if not self.available:
            return None
        if self._embedder is not None:
            return self._embedder
        return await asyncio.to_thread(self._embedder_sync)

    # -- writing --------------------------------------------------------------

    async def has(self, digest: str) -> bool:
        return digest in await self._vectors_loaded()

    async def known(self) -> set[str]:
        return set(await self._vectors_loaded())

    async def embed_image(self, digest: str, data: bytes) -> bool:
        """Embed one image unless it already has a vector. True when it has
        one afterwards."""
        vectors = await self._vectors_loaded()
        if digest in vectors:
            return True
        async with self._embed_lock:
            if digest in vectors:
                return True
            embedder = await self._ready_embedder()
            if embedder is None:
                return False

            def run() -> np.ndarray | None:
                raw = embedder.embed_images([data])[0]
                v = _unit(raw)
                if v is None:
                    return None
                self._store_sync(digest, MODEL_ID, v)
                return v

            try:
                v = await asyncio.to_thread(run)
            except Exception:
                log.warning("could not embed gallery image %s", digest, exc_info=True)
                return False
            if v is None:
                return False
            vectors[digest] = v
            return True

    def _store_sync(self, key: str, model: str, v: np.ndarray) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO vectors(digest, model, data) VALUES (?,?,?)",
                (key, model, v.astype(np.float32).tobytes()),
            )
            conn.commit()
        finally:
            conn.close()

    @property
    def reads_text(self) -> bool:
        return self.enabled and self._text_embed is not None

    async def has_text(self, key: str) -> bool:
        await self._vectors_loaded()
        return key in self._texts

    async def known_texts(self) -> set[str]:
        await self._vectors_loaded()
        return set(self._texts)

    async def _embed_texts(self, texts: list[str]) -> list[np.ndarray | None] | None:
        if not self.reads_text:
            return None
        try:
            raw = await self._text_embed(texts)
        except Exception:
            log.warning("could not embed gallery text", exc_info=True)
            return None
        if not raw:
            return None
        return [_unit(v) for v in raw]

    async def embed_text(self, key: str, text: str) -> bool:
        """Embed one reading unless its text already has a vector."""
        await self._vectors_loaded()
        if not key or not text.strip():
            return False
        if key in self._texts:
            return True
        async with self._text_lock:
            if key in self._texts:
                return True
            got = await self._embed_texts([text])
            v = got[0] if got else None
            if v is None:
                return False
            try:
                await asyncio.to_thread(self._store_sync, f"t:{key}", self._text_model, v)
            except Exception:
                log.warning("could not store a gallery text vector", exc_info=True)
            self._texts[key] = v
            return True

    async def query_text_vector(self, text: str) -> np.ndarray | None:
        """A query in the readings' space."""
        got = await self._embed_texts([text]) if text.strip() else None
        return got[0] if got else None

    async def nearest_text(
        self, query: np.ndarray, candidates: dict[str, str], k: int
    ) -> list[tuple[str, float]]:
        """As `nearest`, over reading vectors (candidates: path -> text key)."""
        await self._vectors_loaded()
        paths = [p for p, key in candidates.items() if key in self._texts]
        if not paths:
            return []
        matrix = np.stack([self._texts[candidates[p]] for p in paths])
        scores = matrix @ query
        order = np.argsort(-scores)[:k]
        return [(paths[i], float(scores[i])) for i in order]

    async def like_text_key(
        self, key: str, candidates: dict[str, str], k: int
    ) -> list[tuple[str, float]]:
        await self._vectors_loaded()
        query = self._texts.get(key)
        if query is None:
            return []
        return await self.nearest_text(query, candidates, k)

    # -- reading --------------------------------------------------------------

    async def text_vector(self, text: str) -> np.ndarray | None:
        embedder = await self._ready_embedder()
        if embedder is None or not text.strip():
            return None
        try:
            raw = await asyncio.to_thread(lambda: embedder.embed_texts([text])[0])
        except Exception:
            log.warning("could not embed a gallery query", exc_info=True)
            return None
        return _unit(raw)

    async def nearest(
        self, query: np.ndarray, candidates: dict[str, str], k: int
    ) -> list[tuple[str, float]]:
        """The `k` candidates (path -> digest) whose image is nearest `query`,
        with their cosine similarity, best first. Candidates without a
        vector are skipped."""
        vectors = await self._vectors_loaded()
        paths = [p for p, d in candidates.items() if d in vectors]
        if not paths:
            return []
        matrix = np.stack([vectors[candidates[p]] for p in paths])
        scores = matrix @ query
        order = np.argsort(-scores)[:k]
        return [(paths[i], float(scores[i])) for i in order]

    async def like_image(
        self, digest: str, candidates: dict[str, str], k: int
    ) -> list[tuple[str, float]]:
        vectors = await self._vectors_loaded()
        query = vectors.get(digest)
        if query is None:
            return []
        return await self.nearest(query, candidates, k)

    async def like_text(
        self, text: str, candidates: dict[str, str], k: int
    ) -> list[tuple[str, float]]:
        query = await self.text_vector(text)
        if query is None:
            return []
        return await self.nearest(query, candidates, k)
