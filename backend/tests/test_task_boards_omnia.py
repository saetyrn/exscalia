"""OMNIA files tasks on the board of the folder the user is working in.

These are the only tests that drive `context_note` through a tool, so they also
pin the recovery path: the chat endpoint writes it onto the user's message
before the model runs, and handlers read it back off the thread.
"""
import asyncio
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.overseer.tool_expansion import capture, create_task

BOARD = '---\ntitle: "Tasks"\ntask_board: true\n---\n\n# Tasks\n'


@pytest.fixture
def omnia_state(acting_state):
    """acting_state plus the wiring capture and the resolver need."""
    from app.event_store import EventStore

    acting_state.timezone = "UTC"
    acting_state.events = EventStore(acting_state.settings.vault_dir)
    return acting_state


def with_open_note(state, path: str | None):
    """Stand in for the thread store, whose last user message carries the note
    the user has open."""
    messages = [{"role": "assistant", "text": "hi"}]
    if path is not None:
        messages.append({"role": "user", "text": "…", "context_note": path})
    state.threads = SimpleNamespace(get=lambda _tid: {"messages": messages})
    return state


def make_board(state, folder: str) -> str:
    path = f"{folder}/Tasks.md"
    state.store.create(path, BOARD)
    asyncio.run(state.indexer.index_note(path, state.store.read(path)))
    return path


def test_task_lands_on_the_board_of_the_folder_in_use(omnia_state):
    make_board(omnia_state, "projects")
    with_open_note(omnia_state, "projects/plan.md")

    result = asyncio.run(create_task(omnia_state, "t", {"text": "email the professor"}))
    assert result["task"]["path"] == "projects/Tasks.md"


def test_a_subfolder_without_a_board_uses_its_nearest_ancestor(omnia_state):
    make_board(omnia_state, "projects")
    with_open_note(omnia_state, "projects/2026/deep/plan.md")

    result = asyncio.run(create_task(omnia_state, "t", {"text": "book the room"}))
    assert result["task"]["path"] == "projects/Tasks.md"


def test_a_subfolders_own_board_wins_over_its_parents(omnia_state):
    make_board(omnia_state, "projects")
    make_board(omnia_state, "projects/2026")
    with_open_note(omnia_state, "projects/2026/plan.md")

    result = asyncio.run(create_task(omnia_state, "t", {"text": "draft the outline"}))
    assert result["task"]["path"] == "projects/2026/Tasks.md"


def test_an_unboarded_folder_still_uses_the_inbox(omnia_state):
    make_board(omnia_state, "projects")
    with_open_note(omnia_state, "ideas/thought.md")

    result = asyncio.run(create_task(omnia_state, "t", {"text": "read that paper"}))
    assert result["task"]["path"] == "Tasks Inbox.md"


def test_an_explicit_path_always_wins(omnia_state):
    make_board(omnia_state, "projects")
    with_open_note(omnia_state, "projects/plan.md")

    result = asyncio.run(
        create_task(omnia_state, "t", {"text": "elsewhere", "path": "Tasks Inbox.md"})
    )
    assert result["task"]["path"] == "Tasks Inbox.md"


def test_no_thread_store_falls_back_instead_of_raising(omnia_state):
    """The resolver runs in harnesses that have no threads at all."""
    make_board(omnia_state, "projects")
    assert not hasattr(omnia_state, "threads")

    result = asyncio.run(create_task(omnia_state, "t", {"text": "orphan"}))
    assert result["task"]["path"] == "Tasks Inbox.md"


def test_capture_files_on_the_board_and_still_marks_the_day(omnia_state):
    make_board(omnia_state, "projects")
    with_open_note(omnia_state, "projects/plan.md")
    today = datetime.now(ZoneInfo(omnia_state.timezone)).date().isoformat()

    result = asyncio.run(
        capture(omnia_state, "t", {"text": "email the professor", "kind": "task"})
    )
    assert result["task"]["path"] == "projects/Tasks.md"
    # The task left the daily note, so the day needs the line back or it loses
    # the trace of the commitment entirely.
    daily = omnia_state.store.read(f"daily/{today}.md")
    assert "- email the professor" in daily


def test_capture_without_a_board_still_writes_into_the_daily_note(omnia_state):
    with_open_note(omnia_state, "ideas/thought.md")
    today = datetime.now(ZoneInfo(omnia_state.timezone)).date().isoformat()

    result = asyncio.run(capture(omnia_state, "t", {"text": "call the bank", "kind": "task"}))
    assert result["task"]["path"] == f"daily/{today}.md"
    daily = omnia_state.store.read(f"daily/{today}.md")
    # The task *is* the record here; a prose line would duplicate it.
    assert "- [ ] call the bank" in daily
    assert "- call the bank" not in daily
