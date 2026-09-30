"""Background embedding: writes never wait on the embedding model, a burst of
saves embeds only the latest revision of each note, notes still embed in the
order they were first queued, and failures don't stall the queue. The model
itself loads once, however many cold callers arrive together. The real model
never loads here — index_note and the load are stubbed, and the chunk-reuse
tests use a real LanceDB table with a fake model."""
import asyncio
import itertools
import threading
import time
from types import SimpleNamespace

import pytest

from app.index.db import content_hash
from app.index.semantic import RETRY_AFTER, SemanticIndex


def recording(sem: SemanticIndex):
    """Stub the operations the queue runs, recording each call."""
    calls = SimpleNamespace(indexed=[], removed=[], ran=asyncio.Event())

    async def fake_index(path: str, content: str):
        calls.indexed.append((path, content))
        calls.ran.set()

    async def fake_remove(path: str):
        calls.removed.append(path)
        calls.ran.set()

    sem.index_note = fake_index
    sem.remove_note = fake_remove
    return calls


def test_a_burst_of_saves_embeds_only_the_last_revision(tmp_path):
    """300 autosaves of one note used to queue 300 full embeds; the debounce
    timer fires once, with whatever was saved last."""
    sem = SemanticIndex(tmp_path, "stub-model", debounce=0.02)

    async def run():
        calls = recording(sem)
        await sem.index_note_soon("a.md", "v1")
        await sem.index_note_soon("a.md", "v2")
        await sem.index_note_soon("a.md", "v3")
        assert calls.indexed == []  # nothing runs before the quiet period
        await asyncio.wait_for(calls.ran.wait(), timeout=2)
        await asyncio.sleep(0.1)
        return calls

    calls = asyncio.run(run())
    assert calls.indexed == [("a.md", "v3")]


def test_notes_embed_in_first_queued_order(tmp_path):
    sem = SemanticIndex(tmp_path, "stub-model")

    async def run():
        calls = recording(sem)
        await sem.index_note_soon("a.md", "a1")
        await sem.index_note_soon("b.md", "b1")
        await sem.index_note_soon("a.md", "a2")
        await asyncio.wait_for(sem.drain(), timeout=2)
        return calls

    calls = asyncio.run(run())
    assert calls.indexed == [("a.md", "a2"), ("b.md", "b1")]


def test_the_last_operation_on_a_path_wins(tmp_path):
    sem = SemanticIndex(tmp_path, "stub-model")

    async def run():
        calls = recording(sem)
        await sem.index_note_soon("gone.md", "x")
        await sem.remove_note_soon("gone.md")
        await sem.remove_note_soon("back.md")
        await sem.index_note_soon("back.md", "y")
        await asyncio.wait_for(sem.drain(), timeout=2)
        return calls

    calls = asyncio.run(run())
    assert calls.removed == ["gone.md"]
    assert calls.indexed == [("back.md", "y")]


def test_a_revision_saved_mid_embedding_lands_after_it(tmp_path):
    """An older revision must never land last: when the timer fires again
    while a note is still embedding, the newer save runs after that embed,
    not beside it."""
    sem = SemanticIndex(tmp_path, "stub-model", debounce=0.02)
    seen: list[tuple[str, str]] = []
    started, release = asyncio.Event(), asyncio.Event()

    async def fake_index(path: str, content: str):
        if not started.is_set():  # only the first embed parks
            started.set()
            await release.wait()
        seen.append((path, content))

    sem.index_note = fake_index

    async def run():
        await sem.index_note_soon("a.md", "old")
        await asyncio.wait_for(started.wait(), timeout=2)
        await sem.index_note_soon("a.md", "new")
        await asyncio.sleep(0.1)  # its timer fires while "old" is mid-embed
        release.set()
        await asyncio.wait_for(sem.drain(), timeout=2)

    asyncio.run(run())
    assert seen == [("a.md", "old"), ("a.md", "new")]


def test_a_queued_revision_superseded_mid_batch_is_skipped(tmp_path):
    sem = SemanticIndex(tmp_path, "stub-model")
    seen: list[tuple[str, str]] = []
    started, release = asyncio.Event(), asyncio.Event()

    async def fake_index(path: str, content: str):
        if path == "a.md":
            started.set()
            await release.wait()
        seen.append((path, content))

    sem.index_note = fake_index

    async def run():
        await sem.index_note_soon("a.md", "a")
        await sem.index_note_soon("b.md", "b-old")
        draining = asyncio.create_task(sem.drain())
        await asyncio.wait_for(started.wait(), timeout=2)
        await sem.index_note_soon("b.md", "b-new")
        release.set()
        await asyncio.wait_for(draining, timeout=2)

    asyncio.run(run())
    assert seen == [("a.md", "a"), ("b.md", "b-new")]


def test_a_failed_embedding_does_not_stall_later_ones(tmp_path):
    sem = SemanticIndex(tmp_path, "stub-model")
    seen: list[str] = []

    async def fake_index(path: str, content: str):
        if path == "bad.md":
            raise RuntimeError("boom")
        seen.append(path)

    sem.index_note = fake_index

    async def run():
        await sem.index_note_soon("bad.md", "x")
        await sem.index_note_soon("good.md", "y")
        await asyncio.wait_for(sem.drain(), timeout=2)

    asyncio.run(run())
    assert seen == ["good.md"]


def test_drain_runs_an_armed_timer_immediately(tmp_path):
    sem = SemanticIndex(tmp_path, "stub-model", debounce=3600)

    async def run():
        calls = recording(sem)
        await sem.index_note_soon("a.md", "x")
        await asyncio.wait_for(sem.drain(), timeout=2)
        return calls

    calls = asyncio.run(run())
    assert calls.indexed == [("a.md", "x")]


def test_a_steady_stream_of_saves_still_embeds_within_the_max_delay(tmp_path):
    """Every save re-arms the debounce; the cap keeps a long writing session
    with short pauses from never reaching semantic search."""
    sem = SemanticIndex(tmp_path, "stub-model", debounce=3600, max_delay=0.05)

    async def run():
        calls = recording(sem)
        for i in range(200):
            if calls.indexed:
                break
            await sem.index_note_soon("a.md", f"v{i}")
            await asyncio.sleep(0.01)
        return calls

    calls = asyncio.run(run())
    assert calls.indexed and calls.indexed[0][0] == "a.md"


def test_drain_with_nothing_queued_returns_immediately(tmp_path):
    sem = SemanticIndex(tmp_path, "stub-model")
    asyncio.run(asyncio.wait_for(sem.drain(), timeout=1))


def test_concurrent_cold_callers_share_one_model_load(tmp_path):
    """Each cold caller used to build its own model in its own thread."""
    sem = SemanticIndex(tmp_path, "stub-model")
    loads: list[int] = []

    def slow_load():
        loads.append(1)
        time.sleep(0.05)
        sem._model = sem._table = object()

    sem._ensure_sync = slow_load

    async def run():
        return await asyncio.gather(sem._ensure(), sem._ensure(), sem._ensure())

    assert not sem.ready
    assert asyncio.run(run()) == [True, True, True]
    assert loads == [1]
    assert sem.ready


def test_a_failed_load_is_tried_once(tmp_path):
    sem = SemanticIndex(tmp_path, "stub-model")
    loads: list[int] = []

    def broken_load():
        loads.append(1)
        time.sleep(0.02)
        raise RuntimeError("no model")

    sem._ensure_sync = broken_load

    async def run():
        return await asyncio.gather(sem._ensure(), sem._ensure())

    assert asyncio.run(run()) == [False, False]
    assert loads == [1]
    assert not sem.available


def test_warm_returns_at_once_and_loads_in_the_background(tmp_path):
    sem = SemanticIndex(tmp_path, "stub-model")
    release = threading.Event()
    loads: list[int] = []

    def gated_load():
        loads.append(1)
        release.wait(5)
        sem._model = sem._table = object()

    sem._ensure_sync = gated_load

    async def run():
        sem.warm()
        first = sem._warming
        sem.warm()  # already warming: a no-op
        assert sem._warming is first
        await asyncio.sleep(0.01)
        assert not sem.ready
        release.set()
        await sem._warming
        assert sem.ready
        sem.warm()  # ready: nothing left to do

    asyncio.run(run())
    assert loads == [1]


def test_warm_is_a_no_op_when_the_index_is_off(tmp_path):
    sem = SemanticIndex(tmp_path, "stub-model", enabled=False)
    sem.warm()
    assert sem._warming is None


def test_warm_loads_again_once_a_failed_load_is_due_its_retry(tmp_path):
    """The finished task of a failed load used to stay in _warming and turn
    every later warm() into a no-op, so search answered 'warming' with no
    semantic pass until some other caller happened to load the model."""
    sem = SemanticIndex(tmp_path, "stub-model")
    loads: list[int] = []

    def load():
        loads.append(1)
        if len(loads) == 1:
            raise RuntimeError("model download failed")
        sem._model = sem._table = object()

    sem._ensure_sync = load

    async def run():
        sem.warm()
        await sem._warming
        assert not sem.available
        sem.warm()  # within RETRY_AFTER: still off
        assert loads == [1]
        sem._init_failed_at -= RETRY_AFTER
        assert sem.available and not sem.ready
        sem.warm()
        await sem._warming
        assert sem.ready

    asyncio.run(run())
    assert loads == [1, 1]


# -- chunk reuse ---------------------------------------------------------------


@pytest.fixture
def lance_index(tmp_path):
    """A SemanticIndex over a real LanceDB table whose model records every
    batch of texts it is asked to embed."""
    import lancedb
    import pyarrow as pa

    schema = pa.schema(
        [
            pa.field("id", pa.string()),
            pa.field("path", pa.string()),
            pa.field("heading", pa.string()),
            pa.field("text", pa.string()),
            pa.field("hash", pa.string()),
            pa.field("vector", pa.list_(pa.float32(), 2)),
        ]
    )
    index = SemanticIndex(tmp_path, "stub-model")
    index._table = lancedb.connect(tmp_path / "lancedb").create_table("chunks", schema=schema)
    embedded: list[list[str]] = []
    counter = itertools.count()

    def embed(texts):
        texts = list(texts)
        embedded.append(texts)
        return iter([[float(next(counter)), 1.0] for _ in texts])

    index._model = SimpleNamespace(embed=embed)

    async def ready():
        return True

    index._ensure = ready
    return index, embedded


def note(sections: dict[str, str]) -> str:
    return "---\ntitle: Long\n---\n\n" + "\n\n".join(
        f"## {heading}\n\n{body}" for heading, body in sections.items()
    )


def rows(index) -> dict[str, dict]:
    return {
        r["heading"]: r for r in index._table.to_arrow().to_pylist() if r["path"] == "long.md"
    }


def test_a_save_embeds_only_the_sections_that_changed(lance_index):
    index, embedded = lance_index
    # More sections than a default query limit, so a truncated read of the
    # stored rows would show up as re-embedded sections.
    sections = {f"S{i}": f"body {i}" for i in range(12)}
    asyncio.run(index.index_note("long.md", note(sections)))
    assert len(embedded) == 1 and len(embedded[0]) == 12
    before = rows(index)

    sections["S5"] = "rewritten"
    v2 = note(sections)
    asyncio.run(index.index_note("long.md", v2))
    assert embedded[1] == ["Long — S5\n\nrewritten"]

    after = rows(index)
    assert set(after) == set(before)
    assert {r["hash"] for r in after.values()} == {content_hash(v2)}
    for heading, row in after.items():
        if heading != "S5":
            assert row["vector"] == before[heading]["vector"]
    assert after["S5"]["vector"] != before["S5"]["vector"]


def test_dropping_a_section_or_resaving_embeds_nothing(lance_index):
    index, embedded = lance_index
    sections = {f"S{i}": f"body {i}" for i in range(3)}
    asyncio.run(index.index_note("long.md", note(sections)))
    del sections["S1"]
    v2 = note(sections)
    asyncio.run(index.index_note("long.md", v2))
    version = index._table.version
    asyncio.run(index.index_note("long.md", v2))

    assert index._table.version == version  # an unchanged note isn't rewritten
    assert len(embedded) == 1  # the first index only
    after = rows(index)
    assert set(after) == {"S0", "S2"}
    assert {r["hash"] for r in after.values()} == {content_hash(v2)}
