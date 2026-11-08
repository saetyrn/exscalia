"""The user's clock (app/tz.py) and the routes that must read it.

The container runs in UTC, so anything that falls back to the server's own
clock is 4-5 hours off the user's evening.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app import tz as tz_module
from app.config import Settings
from app.tz import DEFAULT_TZ, local_today, parse_user_datetime, user_tz


@pytest.mark.parametrize("name", ["", "Nope/Zone", "../etc/passwd", None])
def test_user_tz_falls_back_instead_of_raising(name):
    assert user_tz(SimpleNamespace(timezone=name)).key == DEFAULT_TZ


def test_user_tz_without_a_timezone_falls_back():
    assert user_tz(SimpleNamespace()).key == DEFAULT_TZ


def test_local_today_is_the_users_date_not_utcs(monkeypatch):
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 22, 1, 30, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(tz_module, "datetime", Frozen)

    assert local_today(SimpleNamespace(timezone="America/New_York")) == "2026-09-21"
    assert local_today(SimpleNamespace(timezone="UTC")) == "2026-09-22"


def test_parse_user_datetime_converts_an_offset_and_keeps_naive_values():
    new_york = ZoneInfo("America/New_York")

    assert parse_user_datetime("2026-09-24T14:00Z", new_york) == datetime(2026, 9, 24, 10, 0)
    assert parse_user_datetime("2026-09-24T14:00", new_york) == datetime(2026, 9, 24, 14, 0)
    with pytest.raises(ValueError):
        parse_user_datetime("Friday", new_york)


# --- routes ------------------------------------------------------------------


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


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    from app import main as main_module

    monkeypatch.setattr(main_module, "get_settings", lambda: _settings(tmp_path))
    with TestClient(main_module.app) as client:
        yield client


@pytest.mark.parametrize("name", ["", "Nope/Zone"])
def test_saving_an_invalid_timezone_is_a_400(client, name):
    # An empty name raises ValueError, not ZoneInfoNotFoundError: it was a 500.
    response = client.put("/api/settings/general", json={"timezone": name})

    assert response.status_code == 400, response.text
    assert client.app.state.timezone != name


def test_the_greeting_leaves_a_later_reminder_for_its_real_time(client):
    """The greeting consumed reminders against the server's naive clock (UTC in
    the container, hours ahead of the user), so a reminder due later that day
    was marked surfaced and the ticker never pushed it."""
    state = client.app.state
    # The zone furthest behind every server clock, so the server's naive now is
    # always hours past the user's.
    state.timezone = "Etc/GMT+12"
    now = datetime.now(ZoneInfo(state.timezone)).replace(tzinfo=None, second=0, microsecond=0)
    start = now + timedelta(minutes=30)
    event = state.events.add(
        state.events.normalize(
            title="Later today",
            start=start.isoformat(timespec="minutes"),
            end=(start + timedelta(minutes=15)).isoformat(timespec="minutes"),
            all_day=False,
            timezone_name=state.timezone,
            notes=None,
            note_path=None,
            recurrence=None,
            reminders=[0],
        )
    )

    assert client.post("/api/overseer/morning").status_code == 200

    assert not state.events.get(event["id"]).get("surfaced_reminders")
