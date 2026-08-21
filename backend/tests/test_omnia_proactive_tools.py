"""OMNIA's proactivity payloads: board digest, daily briefing, and the in-app
notification channel a scheduled/proactive run uses to reach the user."""
import asyncio

import pytest

from app.alerts import AlertQueue
from app.event_store import EventStore
from app.overseer.tool_expansion import (
    board_digest,
    create_task,
    create_task_board,
    generate_daily_briefing,
    notify_user,
)

INBOX = "Tasks Inbox.md"


@pytest.fixture
def omnia_state(acting_state):
    acting_state.timezone = "UTC"
    acting_state.events = EventStore(acting_state.settings.vault_dir)
    return acting_state


def add_task(state, text: str, path: str = INBOX, **extra) -> dict:
    return asyncio.run(create_task(state, "t", {"text": text, "path": path, **extra}))["task"]


# --- board_digest ----------------------------------------------------------


def test_board_digest_counts_and_buckets(omnia_state):
    omnia_state.store.create_folder("projects")
    board = asyncio.run(create_task_board(omnia_state, "t", {"folder": "projects"}))["path"]
    add_task(omnia_state, "open one", path=board)
    add_task(omnia_state, "shipped", path=board, status="done")
    add_task(omnia_state, "late", path=board, due="2000-01-01")

    digest = asyncio.run(board_digest(omnia_state, "t", {"path": board}))

    assert digest["board"] == board
    assert digest["total"] == 3
    assert digest["counts"]["done"] == 1
    assert digest["open"] == 2
    assert any(t["text"] == "late" for t in digest["overdue"])


def test_board_digest_defaults_to_the_universal_board(omnia_state):
    add_task(omnia_state, "loose")
    digest = asyncio.run(board_digest(omnia_state, "t", {}))
    assert digest["board"] == INBOX


# --- generate_daily_briefing -----------------------------------------------


def test_briefing_surfaces_overdue_and_open_boards(omnia_state):
    add_task(omnia_state, "late thing", due="2000-01-01")
    omnia_state.store.create_folder("projects")
    board = asyncio.run(create_task_board(omnia_state, "t", {"folder": "projects"}))["path"]
    add_task(omnia_state, "board task", path=board)

    brief = asyncio.run(generate_daily_briefing(omnia_state, "t", {"days": 1}))

    assert "today" in brief
    assert any(t["text"] == "late thing" for t in brief["overdue"])
    assert any(b["board"] == board for b in brief["boards"])


# --- notify_user -----------------------------------------------------------


def test_notify_user_queues_an_alert(omnia_state):
    omnia_state.alerts = AlertQueue(omnia_state.settings.data_dir)

    result = asyncio.run(
        notify_user(omnia_state, "t", {"title": "Backup done", "body": "all good"})
    )

    assert result["delivered"] is True
    drained = omnia_state.alerts.drain()
    assert len(drained) == 1
    assert drained[0]["title"] == "Backup done"
    assert drained[0]["source"] == "omnia"


def test_notify_user_without_a_channel_is_graceful(omnia_state):
    result = asyncio.run(notify_user(omnia_state, "t", {"title": "nobody home"}))
    assert result["delivered"] is False
