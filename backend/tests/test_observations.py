"""Staged observations: the only way a background pass records anything.

The whole feature is a set of hard limits, so these lean on the limits rather
than on the happy path. A proactive pass can stage ONE suggestion a day, it
expires if nobody answers it, a habit the user declined is not raised again for
months, and nothing becomes a habit or a memory entry until the user says so.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api import overseer as overseer_api
from app.config import Settings
from app.overseer import observations
from app.overseer.tool_expansion import note_observation
from app.overseer.tools import SUGGESTION_TOOL_DEFS, TOOL_MODES, execute_tool
from app.vault.internal import NoteError, atomic_json, iso_utc, utc_now


def stage(state, **kwargs):
    return asyncio.run(note_observation(state, "t", kwargs))


def stored(state) -> dict:
    return observations.load(state)


def request_for(state):
    return SimpleNamespace(app=SimpleNamespace(state=state))


# --- the tool tier ----------------------------------------------------------


def test_a_proactive_pass_can_actually_call_it():
    """The point of the tool. Proactive passes are handed the suggestion set,
    which is read + proposal only — an action-tier tool would be refused call
    by call, so this must be in there."""
    assert TOOL_MODES["note_observation"] == "proposal"
    assert "note_observation" in {t.name for t in SUGGESTION_TOOL_DEFS}


def test_a_background_turn_stages_one_and_is_told_the_tool_exists(acting_state):
    """End to end through the agent loop in proactive mode — the mode that
    could previously notice things and record none of them."""
    from app.overseer.agent import Overseer
    from app.overseer.provider import FakeProvider, ProviderReply, ToolCall

    call = ToolCall(
        id="t1",
        name="note_observation",
        input={
            "text": "You log hours most evenings — worth remembering?",
            "kind": "habit",
            "habit_key": "daily.logs_hours",
            "habit_value": "evening",
        },
    )
    provider = FakeProvider(
        [
            ProviderReply(
                text="",
                tool_calls=[call],
                stop_reason="tool_use",
                raw_content=[
                    {"type": "tool_use", "id": "t1", "name": call.name, "input": call.input}
                ],
            ),
            ProviderReply(text="Nothing else to raise.", stop_reason="end_turn"),
        ]
    )

    result = asyncio.run(
        Overseer(provider, acting_state).chat("morning check", proactive=True)
    )

    assert result["trace"][0]["ok"] is True
    entry = stored(acting_state)["observations"][0]
    assert entry["habit_key"] == "daily.logs_hours"
    assert entry["status"] == "pending"
    # And the pass is told the tool is there, or it never occurs to it.
    assert "note_observation" in provider.systems[-1]


# --- the hard rules ---------------------------------------------------------


def test_only_one_observation_is_staged_per_day(acting_state):
    first = stage(acting_state, text="Logs hours every evening", kind="habit",
                  habit_key="daily.logs_hours", habit_value="evening")
    second = stage(acting_state, text="Also files everything under School", kind="memory")

    assert first["staged"] is True
    assert second["staged"] is False
    assert "already waiting" in second["note"]
    # A refusal is a note, not an error: the pass carries on quietly.
    assert "error" not in second
    assert len(stored(acting_state)["observations"]) == 1


def test_a_pending_observation_expires_after_a_week(acting_state):
    stage(acting_state, text="Reads before bed", kind="memory")
    data = stored(acting_state)
    data["observations"][0]["created_at"] = iso_utc(utc_now() - timedelta(days=8))
    observations.save(acting_state, data)

    # Gone from what the user is shown…
    assert observations.surfaced(observations.prune(stored(acting_state))) is None
    # …and the slot is free again, rather than blocked forever by a question
    # nobody answered.
    assert stage(acting_state, text="Something new", kind="memory")["staged"] is True


def test_a_dismissed_habit_key_is_not_raised_again_for_months(acting_state):
    entry = stage(acting_state, text="Logs hours every evening", kind="habit",
                  habit_key="daily.logs_hours", habit_value="evening")
    observations.settle(acting_state, entry["observation_id"], "dismissed")

    # Even a day later — the daily cap is not what is blocking this.
    data = stored(acting_state)
    data["observations"][0]["created_at"] = iso_utc(utc_now() - timedelta(days=2))
    observations.save(acting_state, data)

    refused = stage(acting_state, text="Logs hours every evening", kind="habit",
                    habit_key="daily.logs_hours", habit_value="evening")

    assert refused["staged"] is False
    assert "declined" in refused["note"]
    # A different key is unaffected.
    assert stage(acting_state, text="Names notes in Title Case", kind="habit",
                 habit_key="notes.title_case", habit_value="yes")["staged"] is True


def test_an_old_dismissal_stops_blocking(acting_state):
    entry = stage(acting_state, text="Logs hours", kind="habit",
                  habit_key="daily.logs_hours", habit_value="evening")
    observations.settle(acting_state, entry["observation_id"], "dismissed")
    data = stored(acting_state)
    data["dismissed"][0]["dismissed_at"] = iso_utc(utc_now() - timedelta(days=120))
    data["observations"] = []
    observations.save(acting_state, data)

    assert stage(acting_state, text="Logs hours", kind="habit",
                 habit_key="daily.logs_hours", habit_value="evening")["staged"] is True


def test_the_text_is_capped_and_the_kind_is_checked(acting_state):
    entry = stage(acting_state, text="x" * 500, kind="memory")
    assert len(stored(acting_state)["observations"][0]["text"]) == observations.MAX_TEXT
    assert entry["staged"] is True

    with pytest.raises(NoteError):
        stage(acting_state, text="something", kind="opinion")


def test_the_habit_value_is_capped_like_the_text(acting_state):
    """Confirming stores habit_value, not the sentence, so it gets update_habit's
    limits when it is staged — a pair update_habit refused would take the
    user's yes and store nothing."""
    from app.overseer import habits

    stage(acting_state, text="Formats replies a certain way", kind="habit",
          habit_key="replies.format", habit_value="line one\nline two " + "v" * 400)

    value = stored(acting_state)["observations"][0]["habit_value"]
    assert len(value) == habits.VALUE_CHARS
    assert value.startswith("line one line two ")


def test_a_habit_key_update_habit_would_refuse_is_refused_up_front(acting_state):
    for key in ("k" * 81, "replies format"):
        with pytest.raises(NoteError):
            stage(acting_state, text="Formats replies a certain way", kind="habit",
                  habit_key=key, habit_value="bullets")
    assert stored(acting_state)["observations"] == []


def test_the_card_carries_what_confirming_stores():
    """The briefing showed only the sentence; confirming stored habit_value,
    which could be something else entirely."""
    card = observations.public(
        {
            "id": "abc",
            "text": "You usually log hours in daily notes",
            "kind": "habit",
            "habit_key": "daily_note.logs_hours",
            "habit_value": "evening",
            "created_at": iso_utc(),
            "status": "pending",
        }
    )
    assert card["habit_key"] == "daily_note.logs_hours"
    assert card["habit_value"] == "evening"


def test_a_corrupt_file_does_not_stop_a_pass(acting_state):
    observations.path(acting_state).parent.mkdir(parents=True, exist_ok=True)
    observations.path(acting_state).write_text("{not json", encoding="utf-8")

    assert stage(acting_state, text="Starts fresh", kind="memory")["staged"] is True


def test_settling_an_unknown_observation_is_a_404(acting_state):
    with pytest.raises(NoteError) as exc:
        observations.settle(acting_state, "nope", "confirmed")
    assert exc.value.status == 404


def test_an_observation_cannot_be_settled_twice(acting_state):
    entry = stage(acting_state, text="Reads before bed", kind="memory")
    observations.settle(acting_state, entry["observation_id"], "confirmed")

    with pytest.raises(NoteError):
        observations.settle(acting_state, entry["observation_id"], "dismissed")


# --- confirming applies it --------------------------------------------------


def test_confirming_a_habit_observation_writes_the_habit(acting_state):
    acting_state.timezone = "America/New_York"
    entry = stage(acting_state, text="Logs hours every evening", kind="habit",
                  habit_key="daily.logs_hours", habit_value="evening")

    asyncio.run(
        overseer_api.confirm_observation(
            request_for(acting_state), entry["observation_id"]
        )
    )

    out = json.loads(asyncio.run(execute_tool(acting_state, "s", "read_habits", {})))
    assert out["habits"][0]["key"] == "daily.logs_hours"
    assert out["habits"][0]["value"] == "evening"
    assert stored(acting_state)["observations"][0]["status"] == "confirmed"
    # Recorded and undoable, as _apply_observation promises.
    (action,) = acting_state.actions.list()
    assert action["tool"] == "update_habit" and action["undoable"] is True
    assert action["thread_id"].startswith("daily-")


def test_confirming_a_memory_observation_writes_the_memory_note(acting_state):
    acting_state.store.create("omnia/memory.md", "---\ntitle: OMNIA Memory\n---\n\n")
    entry = stage(acting_state, text="Reads before bed most nights", kind="memory")

    asyncio.run(
        overseer_api.confirm_observation(
            request_for(acting_state), entry["observation_id"]
        )
    )

    assert "Reads before bed most nights" in acting_state.store.read("omnia/memory.md")


def test_a_habit_observation_with_no_value_falls_back_to_memory(acting_state):
    acting_state.store.create("omnia/memory.md", "---\ntitle: OMNIA Memory\n---\n\n")
    entry = stage(acting_state, text="Seems to plan on Sundays", kind="habit",
                  habit_key="planning.day")

    asyncio.run(
        overseer_api.confirm_observation(
            request_for(acting_state), entry["observation_id"]
        )
    )

    # Nothing structured to store, but what the user agreed to is not thrown away.
    assert "Seems to plan on Sundays" in acting_state.store.read("omnia/memory.md")


def test_dismissing_records_the_tombstone_and_changes_nothing_else(acting_state):
    entry = stage(acting_state, text="Logs hours every evening", kind="habit",
                  habit_key="daily.logs_hours", habit_value="evening")

    asyncio.run(
        overseer_api.dismiss_observation(
            request_for(acting_state), entry["observation_id"]
        )
    )

    out = json.loads(asyncio.run(execute_tool(acting_state, "s", "read_habits", {})))
    assert out["habits"] == []
    data = stored(acting_state)
    assert data["observations"][0]["status"] == "dismissed"
    assert data["dismissed"][0]["habit_key"] == "daily.logs_hours"


def test_confirming_an_expired_observation_is_a_404(acting_state):
    entry = stage(acting_state, text="Reads before bed", kind="memory")
    data = stored(acting_state)
    data["observations"][0]["created_at"] = iso_utc(utc_now() - timedelta(days=30))
    observations.save(acting_state, data)

    with pytest.raises(NoteError) as exc:
        asyncio.run(
            overseer_api.confirm_observation(
                request_for(acting_state), entry["observation_id"]
            )
        )
    assert exc.value.status == 404


# --- the morning contract ---------------------------------------------------


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "app-data",
        frontend_dir=Path(__file__).resolve().parents[2] / "frontend" / "build",
        embedding_backend="none",
        autocommit_debounce=0.01,
        auth_password=None,
        anthropic_api_key=None,
        openai_api_key=None,
        google_api_key=None,
        backup_remote=None,
    )


def test_the_morning_response_carries_the_observation_on_every_open(
    tmp_path: Path, monkeypatch
):
    """The banner reads this key on first open AND on reload, so it has to be
    present (as null) either way — a missing key is a different bug from an
    absent observation."""
    from app import main as main_module

    settings = _settings(tmp_path)
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)

    with TestClient(main_module.app) as client:
        first = client.post("/api/overseer/morning").json()
        assert first["greet"] is True
        assert first["observation"] is None

        # Stage one the way a scheduled pass would, then reload.
        atomic_json(
            settings.vault_dir / observations.OBSERVATIONS_PATH,
            {
                "version": 1,
                "observations": [
                    {
                        "id": "abc123",
                        "text": "You log hours most evenings — worth remembering?",
                        "kind": "habit",
                        "habit_key": "daily.logs_hours",
                        "habit_value": "evening",
                        "created_at": iso_utc(),
                        "status": "pending",
                    }
                ],
                "dismissed": [],
            },
        )
        again = client.post("/api/overseer/morning").json()

        assert again["greet"] is False  # already greeted; the observation still rides
        assert again["observation"] == {
            "id": "abc123",
            "text": "You log hours most evenings — worth remembering?",
            "kind": "habit",
            "habit_key": "daily.logs_hours",
            "habit_value": "evening",
            "created_at": again["observation"]["created_at"],
        }

        assert client.post("/api/overseer/observations/abc123/confirm").json() == {
            "ok": True
        }
        # Answered, so it is not offered again.
        assert client.post("/api/overseer/morning").json()["observation"] is None
        assert client.post("/api/overseer/observations/abc123/dismiss").status_code == 404
