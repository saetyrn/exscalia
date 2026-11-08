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
    def __init__(self, reply="did it"):
        self.sessions: dict = {}
        self.calls: list = []
        self.reply = reply
        self.raises: BaseException | None = None

    async def chat(self, message, session_id=None, proactive=False):
        self.calls.append(
            {"proactive": proactive, "session_id": session_id, "message": message}
        )
        if self.raises is not None:
            raise self.raises
        return {"reply": self.reply}


def _wire_runtime(state, reply="did it"):
    state.timezone = "UTC"
    state.overseer = StubOverseer(reply)
    state.posted: list = []
    locks: dict = {}
    state.threads = SimpleNamespace(
        ensure_daily=lambda day, title: ({"id": f"daily-{day}"}, True),
        generation_lock=lambda tid: locks.setdefault(tid, asyncio.Lock()),
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


def test_schedules_due_together_each_get_their_notification(acting_state):
    """Every schedule runs in the day's one thread, so a notify_user cap keyed
    by thread refused the third schedule's push."""
    from app.alerts import AlertQueue
    from app.overseer.tool_expansion import notify_user

    state = _wire_runtime(acting_state)
    state.alerts = AlertQueue(state.settings.data_dir)
    results: list = []

    async def chat(message, session_id=None, proactive=False):
        title = message.split("Instruction: ", 1)[1].split(")", 1)[0]
        results.append(await notify_user(state, session_id, {"title": title}))
        return {"reply": "NOTHING"}

    state.overseer.chat = chat
    for n in range(3):
        _due_schedule(state, instruction=f"check {n}")

    ran = asyncio.run(run_due_schedules(state, datetime(2020, 1, 1, 10, 0, tzinfo=UTC)))

    assert len(ran) == 3
    assert [r["delivered"] for r in results] == [True, True, True]
    assert sorted(a["title"] for a in state.alerts.peek()) == ["check 0", "check 1", "check 2"]


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


# --- the null-reply sentinel -----------------------------------------------


@pytest.mark.parametrize("reply", ["NOTHING", "Nothing.", "  nothing  ", "Nothing!"])
def test_a_null_reply_never_reaches_the_daily_thread(acting_state, reply):
    """The prompt asked for "a single word" and the guard only dropped replies
    of one character, so an obedient "Nothing." showed up in the user's daily
    thread as a real message from OMNIA."""
    _wire_runtime(acting_state, reply=reply)
    _due_schedule(acting_state)

    asyncio.run(run_due_schedules(acting_state, datetime(2020, 1, 1, 10, 0, tzinfo=UTC)))

    assert acting_state.posted == []
    # The instruction names the sentinel, or the model can't produce it.
    assert "NOTHING" in acting_state.overseer.calls[0]["message"]


def test_a_real_reply_is_still_appended(acting_state):
    _wire_runtime(acting_state, reply="Nothing is filed under #thesis yet.")
    _due_schedule(acting_state)

    asyncio.run(run_due_schedules(acting_state, datetime(2020, 1, 1, 10, 0, tzinfo=UTC)))

    assert [m["text"] for m in acting_state.posted] == [
        "Nothing is filed under #thesis yet."
    ]


def _narrating_overseer(state, answer: str):
    """The real agent loop, scripted: it says what it is about to do, calls a
    read tool, then gives its answer."""
    from app.overseer.agent import Overseer
    from app.overseer.provider import FakeProvider, ProviderReply, ToolCall

    narration = "Let me look at your tasks."
    return Overseer(
        FakeProvider(
            [
                ProviderReply(
                    text=narration,
                    tool_calls=[ToolCall(id="t1", name="read_structure", input={})],
                    stop_reason="tool_use",
                    raw_content=[
                        {"type": "text", "text": narration},
                        {"type": "tool_use", "id": "t1", "name": "read_structure", "input": {}},
                    ],
                ),
                ProviderReply(text=answer, stop_reason="end_turn"),
            ]
        ),
        state,
    )


@pytest.mark.parametrize(
    "answer, posted, status",
    [
        ("NOTHING", [], "nothing"),
        ("Two tasks are overdue.", ["Two tasks are overdue."], "ok"),
    ],
)
def test_narration_before_a_tool_call_is_not_the_scheduled_reply(
    acting_state, answer, posted, status
):
    """An unstreamed turn's reply joins every round's text, so a run that
    narrated before its tool call and then said NOTHING posted
    "Let me look at your tasks.\n\nNOTHING" to the daily thread. The check
    and the post read the closing round alone."""
    _wire_runtime(acting_state)
    acting_state.overseer = _narrating_overseer(acting_state, answer)
    sid = _due_schedule(acting_state)

    asyncio.run(run_due_schedules(acting_state, datetime(2020, 1, 1, 10, 0, tzinfo=UTC)))

    assert [m["text"] for m in acting_state.posted] == posted
    assert ScheduleStore(acting_state.settings.vault_dir).get(sid)["last_status"] == status


def test_is_nothing_reply_only_matches_the_sentinel():
    from app.scheduler import is_nothing_reply

    assert is_nothing_reply("NOTHING")
    assert is_nothing_reply("nothing.")
    assert not is_nothing_reply("Nothing to report — two tasks are overdue.")
    assert not is_nothing_reply("Staged 3 changes.")


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
    assert after["last_status"] == "skipped_budget"
    assert after["last_run"] is None


# --- run bookkeeping -------------------------------------------------------


def test_the_advance_is_persisted_before_the_run(acting_state):
    """A deploy cancels the ticker mid-run, and CancelledError is not an
    Exception: next_run used to be written only after the run, so the same
    slot fired again within a minute of the restart."""
    _wire_runtime(acting_state)
    acting_state.overseer.raises = asyncio.CancelledError()
    sid = _due_schedule(acting_state)
    moment = datetime(2020, 1, 1, 10, 0, tzinfo=UTC)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run_due_schedules(acting_state, moment))

    after = ScheduleStore(acting_state.settings.vault_dir).get(sid)
    assert datetime.fromisoformat(after["next_run"]) > moment
    assert after["last_run"] is None


def test_a_failed_run_is_recorded_as_failed_not_as_run(acting_state):
    """A failing run used to be stamped last_run exactly like a success, so
    neither the record nor OMNIA could tell failing from quiet."""
    _wire_runtime(acting_state)
    acting_state.overseer.raises = RuntimeError("boom")
    sid = _due_schedule(acting_state)
    store = ScheduleStore(acting_state.settings.vault_dir)
    moment = datetime(2020, 1, 1, 10, 0, tzinfo=UTC)

    assert asyncio.run(run_due_schedules(acting_state, moment)) == []

    after = store.get(sid)
    assert after["last_status"] == "failed"
    assert "boom" in after["last_error"]
    assert after["failures"] == 1
    assert after["last_run"] is None
    assert datetime.fromisoformat(after["next_run"]) > moment

    asyncio.run(run_due_schedules(acting_state, datetime(2020, 1, 2, 10, 0, tzinfo=UTC)))
    assert store.get(sid)["failures"] == 2

    # A later success clears the streak and stamps last_run.
    acting_state.overseer.raises = None
    ran = asyncio.run(run_due_schedules(acting_state, datetime(2020, 1, 3, 10, 0, tzinfo=UTC)))

    assert ran == [sid]
    after = store.get(sid)
    assert after["last_status"] == "ok"
    assert after["failures"] == 0
    assert after["last_error"] is None
    assert after["last_run"]


def test_a_null_reply_is_recorded_as_nothing(acting_state):
    _wire_runtime(acting_state, reply="NOTHING")
    sid = _due_schedule(acting_state)

    asyncio.run(run_due_schedules(acting_state, datetime(2020, 1, 1, 10, 0, tzinfo=UTC)))

    after = ScheduleStore(acting_state.settings.vault_dir).get(sid)
    assert after["last_status"] == "nothing"
    assert after["last_run"]


def test_a_schedule_cancelled_during_its_own_run_does_not_abort_the_tick(acting_state):
    _wire_runtime(acting_state)
    store = ScheduleStore(acting_state.settings.vault_dir)
    first = _due_schedule(acting_state, instruction="cancel yourself")
    second = _due_schedule(acting_state)
    stub_chat = acting_state.overseer.chat

    async def chat(message, **kwargs):
        if "cancel yourself" in message:
            store.remove(first)
        return await stub_chat(message, **kwargs)

    acting_state.overseer.chat = chat

    ran = asyncio.run(run_due_schedules(acting_state, datetime(2020, 1, 1, 10, 0, tzinfo=UTC)))

    assert ran == [first, second]
    assert [item["id"] for item in store.list()] == [second]
    assert store.get(second)["last_status"] == "ok"


def test_resuming_a_paused_schedule_waits_for_its_next_slot(acting_state):
    """Paused for a week and resumed at night, a morning schedule used to fire
    its week-old slot on the very next tick."""
    _wire_runtime(acting_state)
    sid = _due_schedule(acting_state, paused=True)
    store = ScheduleStore(acting_state.settings.vault_dir)
    moment = datetime(2020, 1, 8, 23, 10, tzinfo=UTC)

    assert asyncio.run(run_due_schedules(acting_state, moment)) == []
    assert acting_state.overseer.calls == []
    after = store.get(sid)
    assert datetime.fromisoformat(after["next_run"]) > moment
    assert after["last_run"] is None

    store.update(sid, paused=False)  # what manage_scheduled_runs(resume) does
    assert asyncio.run(run_due_schedules(acting_state, moment)) == []
    assert acting_state.overseer.calls == []


def test_a_timezone_change_recomputes_next_run_instead_of_firing(acting_state):
    """next_run is an absolute UTC instant. After a timezone change it used to
    fire once at the old zone's wall-clock time before correcting itself."""
    _wire_runtime(acting_state)
    sid = _due_schedule(acting_state)  # 09:00 daily, computed in UTC
    acting_state.timezone = "America/Los_Angeles"
    moment = datetime(2020, 1, 1, 9, 30, tzinfo=UTC)

    ran = asyncio.run(run_due_schedules(acting_state, moment))

    assert ran == []
    assert acting_state.overseer.calls == []
    after = ScheduleStore(acting_state.settings.vault_dir).get(sid)
    assert after["next_run"] == next_run(
        after["cadence"], moment, ZoneInfo("America/Los_Angeles")
    )
    assert after["next_run"].startswith("2020-01-01T17:00")  # 09:00 in LA
    assert after["next_run_tz"] == "America/Los_Angeles"


def test_a_legacy_schedule_is_stamped_with_its_zone_without_recompute(acting_state):
    _wire_runtime(acting_state)
    store = ScheduleStore(acting_state.settings.vault_dir)
    sid = _due_schedule(acting_state, next_run="2999-01-01T09:00:00+00:00")
    records = store.list()
    for record in records:
        record.pop("next_run_tz")
    store._save(records)

    asyncio.run(run_due_schedules(acting_state, datetime(2020, 1, 1, 10, 0, tzinfo=UTC)))

    after = store.get(sid)
    assert after["next_run_tz"] == "UTC"
    assert after["next_run"] == "2999-01-01T09:00:00+00:00"
