"""OMNIA scheduled runs: cadence math, the schedule store/tools, and the
firing loop (with a stubbed overseer so no model is called)."""
import asyncio
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.overseer.tool_expansion import manage_scheduled_runs, schedule_agent_run
from app.scheduler import (
    ScheduleStore,
    clean_cadence,
    next_run,
    run_due_schedules,
)
from app.vault.notes import NoteError

UTC = ZoneInfo("UTC")


# --- cadence math ----------------------------------------------------------


def test_clean_cadence_rejects_unknown_frequency():
    with pytest.raises(NoteError):
        clean_cadence("monthly", 1, "09:00", None)


def test_clean_cadence_weekly_validates_weekday():
    with pytest.raises(NoteError):
        clean_cadence("weekly", 1, "09:00", 9)
    cadence = clean_cadence("weekly", 2, "8:5", 0)
    assert cadence == {"frequency": "weekly", "interval": 2, "at": "08:05", "weekday": 0}


def test_next_run_daily_rolls_to_tomorrow_when_time_passed():
    nxt = next_run(
        {"frequency": "daily", "interval": 1, "at": "09:00"},
        datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
        UTC,
    )
    assert nxt.startswith("2026-01-02T09:00")


def test_next_run_daily_stays_today_when_time_ahead():
    nxt = next_run(
        {"frequency": "daily", "interval": 1, "at": "09:00"},
        datetime(2026, 1, 1, 8, 0, tzinfo=UTC),
        UTC,
    )
    assert nxt.startswith("2026-01-01T09:00")


def test_next_run_weekly_finds_next_weekday():
    # 2026-01-01 is a Thursday; the next Monday (weekday 0) is Jan 5.
    nxt = next_run(
        {"frequency": "weekly", "interval": 1, "at": "09:00", "weekday": 0},
        datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
        UTC,
    )
    assert nxt.startswith("2026-01-05T09:00")


# --- schedule tools --------------------------------------------------------


def test_schedule_agent_run_creates_and_persists(acting_state):
    acting_state.timezone = "UTC"

    result = asyncio.run(
        schedule_agent_run(
            acting_state, "t", {"instruction": "brief me", "frequency": "daily", "at": "08:00"}
        )
    )

    schedule = result["schedule"]
    assert schedule["instruction"] == "brief me"
    assert schedule["cadence"]["at"] == "08:00"
    assert schedule["next_run"]
    assert ScheduleStore(acting_state.settings.vault_dir).get(schedule["id"])


def test_manage_list_pause_and_cancel(acting_state):
    acting_state.timezone = "UTC"
    schedule = asyncio.run(
        schedule_agent_run(
            acting_state,
            "t",
            {"instruction": "weekly review", "frequency": "weekly", "weekday": 0},
        )
    )["schedule"]

    listed = asyncio.run(manage_scheduled_runs(acting_state, "t", {"operation": "list"}))
    assert any(item["id"] == schedule["id"] for item in listed["schedules"])

    paused = asyncio.run(
        manage_scheduled_runs(
            acting_state, "t", {"operation": "pause", "schedule_id": schedule["id"]}
        )
    )
    assert paused["schedule"]["paused"] is True

    asyncio.run(
        manage_scheduled_runs(
            acting_state, "t", {"operation": "cancel", "schedule_id": schedule["id"]}
        )
    )
    assert ScheduleStore(acting_state.settings.vault_dir).list() == []


# --- firing loop -----------------------------------------------------------


class StubOverseer:
    def __init__(self):
        self.sessions: dict = {}
        self.calls: list = []

    async def chat(self, message, session_id=None, proactive=False):
        self.calls.append({"proactive": proactive, "session_id": session_id})
        return {"reply": "did it"}


def _wire_runtime(state):
    state.timezone = "UTC"
    state.overseer = StubOverseer()
    state.posted: list = []
    state.threads = SimpleNamespace(
        ensure_daily=lambda day, title: ({"id": f"daily-{day}"}, True),
        provider_history=lambda tid: [],
        set_provider_history=lambda tid, history: None,
        append_public=lambda tid, msg: state.posted.append(msg),
    )
    return state


def _due_schedule(state, **overrides):
    store = ScheduleStore(state.settings.vault_dir)
    schedule = store.add(
        overrides.get("instruction", "summarize tasks"),
        clean_cadence("daily", 1, "09:00", None),
        UTC,
        datetime(2020, 1, 1, 8, 0, tzinfo=UTC),
    )
    changes = {"next_run": "2020-01-01T09:00:00+00:00"}
    changes.update({k: v for k, v in overrides.items() if k != "instruction"})
    store.update(schedule["id"], **changes)
    return schedule["id"]


def test_due_schedule_fires_proactively_and_advances(acting_state):
    _wire_runtime(acting_state)
    sid = _due_schedule(acting_state)

    ran = asyncio.run(run_due_schedules(acting_state, datetime(2020, 1, 1, 10, 0, tzinfo=UTC)))

    assert ran == [sid]
    assert acting_state.overseer.calls and acting_state.overseer.calls[0]["proactive"] is True
    assert acting_state.posted  # the reply was posted to the daily thread
    after = ScheduleStore(acting_state.settings.vault_dir).get(sid)
    assert after["last_run"]
    assert datetime.fromisoformat(after["next_run"]) > datetime(2020, 1, 1, 10, 0, tzinfo=UTC)


def test_paused_schedule_does_not_fire(acting_state):
    _wire_runtime(acting_state)
    _due_schedule(acting_state, paused=True)

    ran = asyncio.run(run_due_schedules(acting_state, datetime(2020, 1, 1, 10, 0, tzinfo=UTC)))

    assert ran == []
    assert acting_state.overseer.calls == []


def test_future_schedule_does_not_fire(acting_state):
    _wire_runtime(acting_state)
    _due_schedule(acting_state, next_run="2999-01-01T09:00:00+00:00")

    ran = asyncio.run(run_due_schedules(acting_state, datetime(2020, 1, 1, 10, 0, tzinfo=UTC)))

    assert ran == []


def test_budget_block_advances_without_running(acting_state):
    _wire_runtime(acting_state)
    acting_state.usage = SimpleNamespace(allow_background=lambda budget: False)
    sid = _due_schedule(acting_state)

    ran = asyncio.run(run_due_schedules(acting_state, datetime(2020, 1, 1, 10, 0, tzinfo=UTC)))

    assert ran == []
    assert acting_state.overseer.calls == []
    # Still advanced, so it does not re-check every single tick.
    after = ScheduleStore(acting_state.settings.vault_dir).get(sid)
    assert datetime.fromisoformat(after["next_run"]) > datetime(2020, 1, 1, 10, 0, tzinfo=UTC)
