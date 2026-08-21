import asyncio
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from app.activity_store import ActivityStore
from app.api import activities as activities_api
from app.vault.internal import read_json


class Touch:
    def __init__(self):
        self.count = 0

    def touch(self):
        self.count += 1


def request_for(store: ActivityStore, touch: Touch):
    state = SimpleNamespace(activities=store, autocommit=touch)
    return SimpleNamespace(app=SimpleNamespace(state=state))


def test_activity_totals_group_day_week_and_month(tmp_path: Path):
    store = ActivityStore(tmp_path / "vault")
    store.add(date(2026, 7, 20), "Reading", 1)
    store.add(date(2026, 7, 23), "reading", 0.5)
    store.add(date(2026, 7, 23), "Admin", 2.5)
    store.add(date(2026, 7, 1), "Blender", 3)
    store.add(date(2026, 6, 30), "Admin", 10)

    summary = store.summary(date(2026, 7, 23))

    assert summary["day"]["total_hours"] == 3
    assert summary["week"]["total_hours"] == 4
    assert summary["month"]["total_hours"] == 7
    assert summary["day"]["activities"] == [
        {"activity": "Admin", "hours": 2.5},
        {"activity": "reading", "hours": 0.5},
    ]
    assert summary["week"]["activities"] == [
        {"activity": "Admin", "hours": 2.5},
        {"activity": "Reading", "hours": 1.5},
    ]
    assert summary["suggestions"] == ["Admin", "Blender", "Reading"]


def test_activity_api_persists_updates_and_touches_backup(tmp_path: Path):
    store = ActivityStore(tmp_path / "vault")
    touch = Touch()
    request = request_for(store, touch)

    created = asyncio.run(
        activities_api.create_activity(
            request,
            activities_api.CreateActivity(
                date="2026-07-23", activity="  FL Studio  ", hours=1.25
            ),
        )
    )
    assert created["activity"] == "FL Studio"
    assert touch.count == 1

    updated = asyncio.run(
        activities_api.update_activity(
            request,
            created["id"],
            activities_api.UpdateActivity(hours=2),
        )
    )
    assert updated["hours"] == 2
    assert touch.count == 2

    summary = asyncio.run(
        activities_api.activity_summary(request, date="2026-07-23")
    )
    assert summary["day"]["total_hours"] == 2

    assert asyncio.run(
        activities_api.delete_activity(request, created["id"])
    ) == {"ok": True}
    assert touch.count == 3
    assert store.summary(date(2026, 7, 23))["entries"] == []

    saved = read_json(store.path, {})
    assert saved == {"version": 1, "entries": []}
