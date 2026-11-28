"""execute_tool: a cancelled turn never leaves a mutation half-done, and a
failing tool leaves a traceback in the server log."""
import asyncio
import json
import logging

import pytest

from app.index.db import content_hash
from app.overseer import tools
from app.overseer.tools import execute_tool


def add_note(state, path: str, content: str) -> str:
    created = state.store.create(path, content)
    asyncio.run(state.indexer.index_note(path, created))
    return created


def test_cancel_during_direct_tool_finishes_write_commit_and_action(acting_state, monkeypatch):
    add_note(acting_state, "n.md", '---\ntitle: "N"\n---\n\n# N\n\nfirst\n')
    revision = content_hash(acting_state.store.read("n.md"))
    real_index = acting_state.indexer.index_note

    async def scenario():
        parked = asyncio.Event()
        release = asyncio.Event()

        async def slow_index(path, content):
            parked.set()
            await release.wait()
            return await real_index(path, content)

        monkeypatch.setattr(acting_state.indexer, "index_note", slow_index)
        run = asyncio.create_task(
            execute_tool(
                acting_state,
                "t",
                "append_to_note",
                {"path": "n.md", "text": "second", "base_revision": revision},
            )
        )
        await parked.wait()
        # Stop, then the SSE teardown: the turn is cancelled twice while the
        # note is written but not yet indexed, committed, or recorded.
        run.cancel()
        await asyncio.sleep(0)
        run.cancel()
        await asyncio.sleep(0)
        assert not run.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await run

    asyncio.run(scenario())

    assert acting_state.store.read("n.md").rstrip().endswith("second")
    actions = acting_state.actions.list()
    assert [a["tool"] for a in actions] == ["append_to_note"]
    assert actions[0]["commit"]
    assert actions[0]["undoable"] is True


def test_cancel_while_waiting_for_the_lock_writes_nothing(acting_state):
    add_note(acting_state, "n.md", '---\ntitle: "N"\n---\n\n# N\n\nfirst\n')
    before = acting_state.store.read("n.md")
    revision = content_hash(before)

    async def scenario():
        lock = acting_state.actions.mutation_lock
        # A task-board write holds the lock; the queued OMNIA edit has not
        # started, so Stop drops it instead of running it afterwards.
        await lock.acquire()
        run = asyncio.create_task(
            execute_tool(
                acting_state,
                "t",
                "append_to_note",
                {"path": "n.md", "text": "second", "base_revision": revision},
            )
        )
        for _ in range(5):
            await asyncio.sleep(0)
        assert not run.done()
        run.cancel()
        done, _ = await asyncio.wait({run}, timeout=2)
        assert run in done and run.cancelled()
        lock.release()
        await asyncio.sleep(0)
        assert not lock.locked()

    asyncio.run(scenario())

    assert acting_state.store.read("n.md") == before
    assert acting_state.actions.list() == []


def test_failure_after_cancel_is_logged(acting_state, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger="app.overseer.tools")

    async def scenario():
        parked = asyncio.Event()
        release = asyncio.Event()

        async def fails_late(state, session_id, inp):
            parked.set()
            await release.wait()
            raise RuntimeError("commit failed")

        monkeypatch.setitem(tools.EXECUTORS, "append_to_note", fails_late)
        run = asyncio.create_task(
            execute_tool(acting_state, "t", "append_to_note", {"path": "n.md", "text": "x"})
        )
        await parked.wait()
        run.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await run
        assert not acting_state.actions.mutation_lock.locked()

    asyncio.run(scenario())

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "append_to_note" in errors[0].getMessage()
    assert errors[0].exc_info and errors[0].exc_info[0] is RuntimeError


def test_cancel_during_read_tool_is_prompt(acting_state, monkeypatch):
    add_note(acting_state, "n.md", "# N\n\nbody\n")
    seen: list[str] = []

    async def scenario():
        parked = asyncio.Event()
        never = asyncio.Event()

        async def stuck(path):
            parked.set()
            try:
                await never.wait()
            except asyncio.CancelledError:
                seen.append("cancelled")
                raise

        monkeypatch.setattr(acting_state.pending, "effective_content", stuck)
        run = asyncio.create_task(
            execute_tool(acting_state, "t", "read_notes", {"paths": ["n.md"]})
        )
        await parked.wait()
        run.cancel()
        done, _ = await asyncio.wait({run}, timeout=2)
        assert run in done and run.cancelled()

    asyncio.run(scenario())

    assert seen == ["cancelled"]
    assert acting_state.actions.list() == []


def test_tool_exception_is_logged_without_input_values(acting_state, monkeypatch, caplog):
    async def boom(state, session_id, inp):
        raise RuntimeError("store signature changed")

    monkeypatch.setitem(tools.EXECUTORS, "append_to_note", boom)
    caplog.set_level(logging.DEBUG, logger="app.overseer.tools")

    output = asyncio.run(
        execute_tool(
            acting_state,
            "t",
            "append_to_note",
            {"path": "Private/diary.md", "text": "very private body", "base_revision": "r"},
        )
    )

    assert json.loads(output) == {"error": "Tool failed: store signature changed"}
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "append_to_note" in errors[0].getMessage()
    assert errors[0].exc_info and errors[0].exc_info[0] is RuntimeError
    assert "very private body" not in caplog.text
    assert "Private/diary.md" not in caplog.text


def test_internal_key_error_is_logged_at_debug(acting_state, monkeypatch, caplog):
    async def lookup_bug(state, session_id, inp):
        return {}["missing"]

    monkeypatch.setitem(tools.EXECUTORS, "read_notes", lookup_bug)
    caplog.set_level(logging.DEBUG, logger="app.overseer.tools")

    output = asyncio.run(execute_tool(acting_state, "t", "read_notes", {"paths": ["n.md"]}))

    assert json.loads(output)["error"].startswith("Missing required field")
    debug = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert debug and debug[0].exc_info[0] is KeyError
