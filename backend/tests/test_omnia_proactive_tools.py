"""OMNIA's proactivity payloads: board digest, daily briefing, and the in-app
notification channel a scheduled/proactive run uses to reach the user."""
import asyncio

import pytest

from app.alerts import AlertQueue
from app.event_store import EventStore
from app.overseer import tool_expansion
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


def test_repeating_the_same_nudge_coalesces_into_one_alert(omnia_state):
    """The dedup key used to be a timestamp, so it was unique per call and
    AlertQueue's dedup never fired — an hourly scheduled run stacked the same
    nudge every hour."""
    omnia_state.alerts = AlertQueue(omnia_state.settings.data_dir)
    nudge = {"title": "Essay due", "body": "Friday"}

    asyncio.run(notify_user(omnia_state, "t", nudge))
    asyncio.run(notify_user(omnia_state, "t", nudge))
    asyncio.run(notify_user(omnia_state, "t", {"title": "Essay due", "body": "Saturday"}))

    drained = omnia_state.alerts.peek()
    assert [a["body"] for a in drained] == ["Friday", "Saturday"]


def test_the_same_nudge_may_recur_on_a_later_day(omnia_state, monkeypatch):
    """A daily reminder is not a duplicate — the key is scoped by local date."""
    from datetime import datetime as real_datetime

    from app.overseer import tool_expansion

    omnia_state.alerts = AlertQueue(omnia_state.settings.data_dir)

    def day(number: int):
        class Frozen(real_datetime):
            @classmethod
            def now(cls, tz=None):
                return real_datetime(2026, 8, number, 9, 0, tzinfo=tz)

        return Frozen

    monkeypatch.setattr(tool_expansion, "datetime", day(27))
    asyncio.run(notify_user(omnia_state, "t", {"title": "Stand up", "body": "10:00"}))
    monkeypatch.setattr(tool_expansion, "datetime", day(28))
    asyncio.run(notify_user(omnia_state, "t", {"title": "Stand up", "body": "10:00"}))

    assert len(omnia_state.alerts.peek()) == 2


def test_the_push_tag_matches_the_dedup_key(omnia_state, monkeypatch):
    """Same key on the push, so the OS replaces the old notification rather
    than stacking a second one."""
    from app import push_service

    omnia_state.alerts = AlertQueue(omnia_state.settings.data_dir)
    sent: list[list[dict]] = []

    async def capture(state, alerts):
        sent.append(alerts)

    monkeypatch.setattr(push_service, "deliver", capture)
    asyncio.run(notify_user(omnia_state, "t", {"title": "Backup", "body": "done"}))

    queued = omnia_state.alerts.peek()
    assert sent[0][0]["tag"] == queued[0]["key"]
    assert push_service.build_payload(sent[0][0])["tag"] == queued[0]["key"]


# --- notify_user caps --------------------------------------------------------
# The channel reaches the user's phone with model-written text, and a scheduled
# pass may have read a poisoned note, so the limits live in code.


def test_an_overlong_title_is_cut_to_the_cap(omnia_state):
    omnia_state.alerts = AlertQueue(omnia_state.settings.data_dir)
    result = asyncio.run(notify_user(omnia_state, "t", {"title": "x" * 400}))
    assert result["delivered"] is True
    assert len(result["title"]) == tool_expansion.NOTIFY_TITLE_MAX
    assert len(omnia_state.alerts.peek()[0]["title"]) == tool_expansion.NOTIFY_TITLE_MAX


def test_a_third_new_message_from_one_conversation_is_held_back(omnia_state):
    omnia_state.alerts = AlertQueue(omnia_state.settings.data_dir)
    for n in range(2):
        sent = asyncio.run(notify_user(omnia_state, "run", {"title": f"Item {n}"}))
        assert sent["delivered"] is True

    held = asyncio.run(notify_user(omnia_state, "run", {"title": "Item 2"}))

    assert held["delivered"] is False
    assert "10 minutes" in held["note"]
    assert "error" not in held
    assert [a["title"] for a in omnia_state.alerts.peek()] == ["Item 0", "Item 1"]
    # Another conversation is not held back by this one.
    other = asyncio.run(notify_user(omnia_state, "other", {"title": "Item 2"}))
    assert other["delivered"] is True


def test_one_unattended_run_is_held_to_the_cap_in_a_shared_thread(omnia_state):
    from app.scheduler import run_scope

    omnia_state.alerts = AlertQueue(omnia_state.settings.data_dir)

    async def run(label: str, count: int) -> list[dict]:
        async with run_scope(label):
            return [
                await notify_user(omnia_state, "daily", {"title": f"{label} {n}"})
                for n in range(count)
            ]

    burst = asyncio.run(run("schedule:a", 3))
    assert [r["delivered"] for r in burst] == [True, True, False]
    assert "this run" in burst[2]["note"]
    # The next run in the same thread starts its own window, and so does the
    # user's own turn there.
    assert asyncio.run(run("schedule:b", 1))[0]["delivered"] is True
    assert asyncio.run(notify_user(omnia_state, "daily", {"title": "mine"}))["delivered"] is True


def test_a_repeat_of_todays_message_does_not_use_up_the_cap(omnia_state):
    omnia_state.alerts = AlertQueue(omnia_state.settings.data_dir)
    nudge = {"title": "Essay due", "body": "Friday"}
    for _ in range(3):
        assert asyncio.run(notify_user(omnia_state, "run", nudge))["delivered"] is True
    fresh = asyncio.run(notify_user(omnia_state, "run", {"title": "Second thing"}))
    assert fresh["delivered"] is True


def test_the_window_reopens_after_ten_minutes(omnia_state, monkeypatch):
    from datetime import datetime as real_datetime

    omnia_state.alerts = AlertQueue(omnia_state.settings.data_dir)

    def at(minute: int):
        class Frozen(real_datetime):
            @classmethod
            def now(cls, tz=None):
                return real_datetime(2026, 8, 27, 9, minute, tzinfo=tz)

        return Frozen

    monkeypatch.setattr(tool_expansion, "datetime", at(0))
    for n in range(2):
        asyncio.run(notify_user(omnia_state, "run", {"title": f"Item {n}"}))
    monkeypatch.setattr(tool_expansion, "datetime", at(9))
    assert asyncio.run(notify_user(omnia_state, "run", {"title": "Item 2"}))["delivered"] is False
    monkeypatch.setattr(tool_expansion, "datetime", at(11))
    assert asyncio.run(notify_user(omnia_state, "run", {"title": "Item 2"}))["delivered"] is True


def test_the_eleventh_new_message_of_the_day_is_held_back(omnia_state):
    omnia_state.alerts = AlertQueue(omnia_state.settings.data_dir)
    for n in range(tool_expansion.NOTIFY_PER_DAY):
        sent = asyncio.run(notify_user(omnia_state, f"run-{n}", {"title": f"Item {n}"}))
        assert sent["delivered"] is True
    # The day's count is on disk, not in the per-conversation window, so a
    # restart (a fresh state) does not reset it.
    del omnia_state._notify_recent

    held = asyncio.run(notify_user(omnia_state, "fresh", {"title": "One more"}))

    assert held["delivered"] is False
    assert "daily limit" in held["note"]
    assert len(omnia_state.alerts.peek()) == tool_expansion.NOTIFY_PER_DAY


def test_the_tool_description_says_it_can_push():
    tool = next(t for t, _ in tool_expansion.EXPANDED_TOOLS if t.name == "notify_user")
    assert "Web Push" in tool.description
    assert "no external push" not in tool.description
