"""The background embedding chain: writes never wait on the embedding model,
but embeddings still land in write order and failures don't stall the queue.
The real model never loads here — index_note is stubbed."""
import asyncio

from app.index.semantic import SemanticIndex


def test_background_embedding_preserves_write_order(tmp_path):
    """Two racing to_thread hops could land an older revision last; the chain
    serializes them in write order instead."""
    sem = SemanticIndex(tmp_path, "stub-model")
    seen: list[tuple[str, str]] = []
    release = asyncio.Event()

    async def fake_index(path: str, content: str):
        # The first task parks mid-flight; without chaining, the second
        # would overtake it and the note's final embedding would be stale.
        if not seen:
            await release.wait()
        seen.append((path, content))

    sem.index_note = fake_index

    async def run():
        await sem.index_note_soon("a.md", "old")
        await sem.index_note_soon("a.md", "new")
        await asyncio.sleep(0)
        release.set()
        await sem.drain()

    asyncio.run(run())
    assert seen == [("a.md", "old"), ("a.md", "new")]


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
        await sem.drain()

    asyncio.run(run())
    assert seen == ["good.md"]


def test_drain_with_nothing_queued_returns_immediately(tmp_path):
    sem = SemanticIndex(tmp_path, "stub-model")
    asyncio.run(sem.drain())
