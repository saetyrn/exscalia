"""The semantic index recovers instead of switching itself off for good.

A failed init (one bad model download after a deploy) used to disable
semantic search, Related Notes and OMNIA's semantic tools until a restart,
say nothing about why, and turn Settings' rebuild into a no-op reported as a
success. Removals needed the model, the boot backfill never dropped vectors
of deleted notes, and a table built by another embedding model was reused.

Everything here runs the real index over a real LanceDB table; only the
embedding model is fake.
"""
import asyncio
import json
import logging
import shutil
from pathlib import Path
from types import SimpleNamespace

import lancedb

from app.api import meta as meta_api
from app.api import overseer as overseer_api
from app.index import semantic as semantic_module
from app.index.chunker import CHUNKER_VERSION
from app.index.db import content_hash
from app.index.semantic import STAMP, SemanticIndex

from conftest import make_client


class FakeModelIndex(SemanticIndex):
    """SemanticIndex with a stand-in model: each text embeds as its length
    padded to `dim`, and a text containing EXPLODE raises. The first
    `fail_loads` model loads raise, like a failed download."""

    def __init__(
        self, index_dir: Path, model_name: str = "model-a", dim: int = 2, fail_loads: int = 0
    ):
        super().__init__(index_dir, model_name)
        self.dim = dim
        self.fail_loads = fail_loads
        self.loads = 0

    def _load_model_sync(self) -> None:
        if self._model is not None:
            return
        self.loads += 1
        if self.fail_loads:
            self.fail_loads -= 1
            raise RuntimeError("model download failed")

        def embed(texts):
            texts = list(texts)
            if any("EXPLODE" in t for t in texts):
                raise RuntimeError("cannot embed")
            return iter([[float(len(t))] + [1.0] * (self.dim - 1) for t in texts])

        self._model = SimpleNamespace(embed=embed)
        self._dim = self.dim


def note(title: str, body: str = "Some body text.") -> str:
    return f"---\ntitle: {title}\n---\n\n{body}\n"


def paths(index: SemanticIndex) -> set[str]:
    table = lancedb.connect(index.index_dir / "lancedb").open_table("chunks")
    return set(table.to_arrow().column("path").to_pylist())


def listed(state) -> set[str]:
    """The notes the index should hold (templates are never embedded)."""
    return {
        info.path
        for info in state.store.list_notes()
        if not info.path.startswith("templates/")
    }


def stamp(index_dir: Path) -> dict:
    return json.loads((index_dir / "lancedb" / STAMP).read_text())


def run(coro):
    return asyncio.run(coro)


# -- init failures -------------------------------------------------------------


def test_an_init_error_says_why_and_is_retried_after_the_window(tmp_path, monkeypatch, caplog):
    index = FakeModelIndex(tmp_path, fail_loads=1)

    run(index.index_note("a.md", note("A")))
    assert index.available is False
    assert index.error["error"] == "model download failed"
    assert index.error["since"]

    run(index.index_note("a.md", note("A")))
    assert index.loads == 1  # inside the window: no second download attempt

    monkeypatch.setattr(semantic_module, "RETRY_AFTER", 0.0)
    assert index.available is True  # callers gate on this, so it lets the retry in
    with caplog.at_level(logging.WARNING, logger="app.index.semantic"):
        run(index.index_note("a.md", note("A")))
    assert index.loads == 2
    assert "retrying" in caplog.text
    assert index.error == {"error": None, "since": None}
    assert paths(index) == {"a.md"}


def test_the_status_route_reports_the_error(state):
    index = FakeModelIndex(state.settings.data_dir / "index", fail_loads=1)
    run(index.search("anything"))
    state.semantic = index

    body = make_client(state, overseer_api.router).get("/api/search/semantic/status").json()

    assert body["available"] is False
    assert body["error"] == "model download failed"
    assert body["since"] == index.error["since"]


# -- rebuild -------------------------------------------------------------------


def test_rebuild_clears_the_error_and_reembeds(state):
    state.store.create("Alpha.md", note("Alpha"))
    index = FakeModelIndex(state.settings.data_dir / "index", fail_loads=1)
    run(index.search("anything"))  # fails, well inside the retry window
    assert not index.available
    state.semantic = index

    body = make_client(state, overseer_api.router).post("/api/index/rebuild").json()

    assert body["semantic"] is True
    assert body["semantic_error"] is None
    assert body["semantic_failed"] == 0
    assert body["chunks"] >= 1
    assert "Alpha.md" in paths(index)


def test_a_rebuild_that_cannot_embed_reports_failure(state):
    state.store.create("Alpha.md", note("Alpha"))
    state.semantic = FakeModelIndex(state.settings.data_dir / "index", fail_loads=5)

    body = make_client(state, overseer_api.router).post("/api/index/rebuild").json()

    assert body["semantic"] is False
    assert body["semantic_error"] == "model download failed"
    assert body["chunks"] == 0


def test_one_bad_document_does_not_stop_a_rebuild(tmp_path):
    index = FakeModelIndex(tmp_path)
    docs = [
        ("a.md", note("A")),
        ("bad.md", note("Bad", "EXPLODE")),
        ("z.md", note("Z")),
    ]

    chunks = run(index.rebuild(docs))

    assert chunks == 2
    assert index.last_rebuild_errors == ["bad.md"]
    assert paths(index) == {"a.md", "z.md"}


def test_rebuild_replaces_a_table_it_cannot_open(tmp_path):
    """delete('true') needed a readable table of the right width."""
    run(FakeModelIndex(tmp_path, dim=3).index_note("old.md", note("Old")))
    shutil.rmtree(tmp_path / "lancedb" / "chunks.lance" / "_versions")

    index = FakeModelIndex(tmp_path, dim=2)
    assert run(index.stored_hashes()) is None  # the broken table is reported...
    assert index.error["error"]

    assert run(index.rebuild([("new.md", note("New"))])) == 1  # ...and replaced
    assert index.available and index.error["error"] is None
    assert paths(index) == {"new.md"}
    assert stamp(tmp_path)["dim"] == 2


# -- removals ------------------------------------------------------------------


def test_remove_note_needs_no_model(tmp_path):
    seeded = FakeModelIndex(tmp_path)
    run(seeded.index_note("a.md", note("A")))
    run(seeded.index_note("b.md", note("B")))

    index = FakeModelIndex(tmp_path, fail_loads=99)
    run(index.search("anything"))  # the model cannot load at all
    assert index.error["error"]
    run(index.remove_note("a.md"))

    assert paths(index) == {"b.md"}
    assert index.loads == 1  # the failed search's; the removal loaded nothing


def test_a_failed_removal_is_logged(tmp_path, caplog):
    run(FakeModelIndex(tmp_path).index_note("a.md", note("A")))
    index = FakeModelIndex(tmp_path)
    shutil.rmtree(tmp_path / "lancedb" / "chunks.lance" / "_versions")

    with caplog.at_level(logging.ERROR, logger="app.index.semantic"):
        run(index.remove_note("a.md"))

    assert "semantic removal failed for a.md" in caplog.text


# -- boot backfill -------------------------------------------------------------


PAGE = "paper/Ideas.paper/pages/" + "a" * 32


def test_backfill_drops_gone_paths_and_reads_past_a_bad_note(state):
    root = state.settings.vault_dir
    state.store.create("a-keep.md", note("Keep"))
    (root / "m-bad.md").write_bytes(b"\xff\xfe not utf-8")
    state.store.create("z-later.md", note("Later"))
    index = FakeModelIndex(state.settings.data_dir / "index")
    page_doc = "---\ntitle: Ideas · 1\n---\n\nhandwritten words"
    run(index.index_note("gone.md", note("Gone")))
    run(index.index_note(PAGE, page_doc))

    run(index.backfill(state.store, [(PAGE, page_doc)]))

    assert "z-later.md" in paths(index)  # embedded after the note that failed
    assert paths(index) == listed(state) - {"m-bad.md"} | {PAGE}


def test_backfill_embeds_stale_paper_pages(state):
    index = FakeModelIndex(state.settings.data_dir / "index")
    page_doc = "---\ntitle: Ideas · 1\n---\n\nhandwritten words"

    run(index.backfill(state.store, [(PAGE, page_doc)]))

    assert run(index.stored_hashes())[PAGE] == content_hash(page_doc)


def test_backfill_without_the_page_list_removes_nothing(state):
    index = FakeModelIndex(state.settings.data_dir / "index")
    run(index.index_note(PAGE, "---\ntitle: Ideas · 1\n---\n\nhandwritten words"))
    run(index.index_note("gone.md", note("Gone")))

    run(index.backfill(state.store, None))

    assert {PAGE, "gone.md"} <= paths(index)


# -- which model built the table -------------------------------------------------


def test_a_table_built_by_another_model_is_dropped_before_the_backfill(state):
    state.store.create("Alpha.md", note("Alpha"))
    index_dir = state.settings.data_dir / "index"
    run(FakeModelIndex(index_dir, "model-a").index_note("Alpha.md", note("Alpha")))
    assert stamp(index_dir) == {"model": "model-a", "dim": 2, "chunker_version": CHUNKER_VERSION}

    swapped = FakeModelIndex(index_dir, "model-b", dim=3)
    assert run(swapped.stored_hashes()) == {}  # no model load needed to notice
    assert swapped.loads == 0
    assert "chunks" not in lancedb.connect(index_dir / "lancedb").table_names()

    run(swapped.backfill(state.store, []))  # so this boot re-embeds everything
    assert paths(swapped) == listed(state)
    assert stamp(index_dir) == {"model": "model-b", "dim": 3, "chunker_version": CHUNKER_VERSION}


def test_a_table_from_an_older_chunker_is_dropped(tmp_path):
    run(FakeModelIndex(tmp_path).index_note("a.md", note("A")))
    old = {**stamp(tmp_path), "chunker_version": CHUNKER_VERSION - 1}
    (tmp_path / "lancedb" / STAMP).write_text(json.dumps(old))

    assert run(FakeModelIndex(tmp_path).stored_hashes()) == {}


def test_an_unstamped_table_is_adopted_not_reembedded(tmp_path):
    """Tables built before the stamp existed: dropping them would re-embed the
    whole vault on the first deploy for nothing."""
    run(FakeModelIndex(tmp_path).index_note("a.md", note("A")))
    (tmp_path / "lancedb" / STAMP).unlink()

    index = FakeModelIndex(tmp_path)
    assert set(run(index.stored_hashes())) == {"a.md"}
    assert stamp(tmp_path) == {"model": "model-a", "dim": 2, "chunker_version": CHUNKER_VERSION}


def test_an_adopted_table_of_the_wrong_width_is_replaced(tmp_path):
    run(FakeModelIndex(tmp_path, dim=3).index_note("a.md", note("A")))
    (tmp_path / "lancedb" / STAMP).unlink()

    index = FakeModelIndex(tmp_path, dim=2)
    run(index.index_note("b.md", note("B")))

    assert index.available
    assert paths(index) == {"b.md"}
    assert stamp(tmp_path)["dim"] == 2


# -- Related Notes ---------------------------------------------------------------


def test_related_notes_skips_hits_that_are_not_notes(state):
    for path in ("Open.md", "Alive.md"):
        created = state.store.create(path, note(path[:-3], "shared idea"))
        run(state.indexer.index_note(path, created))

    class Semantic:
        available = True

        async def search(self, q, k=8, path_prefix=None, exclude_prefix=None):
            return [
                {"path": p, "heading": "", "text": "T\n\nshared idea", "score": 0.9}
                for p in ("Dead.md", "Alive.md", PAGE)
            ]

    state.semantic = Semantic()
    body = make_client(state, meta_api.router).get("/api/related/Open.md").json()

    assert [r["path"] for r in body["related"]] == ["Alive.md"]
