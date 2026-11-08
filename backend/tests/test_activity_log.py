import asyncio
import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.activity_store import ActivityStore
from app.api import activities as activities_api
from app.vault.internal import read_json
from app.vault.notes import NoteError


class Touch:
    def __init__(self):
        self.count = 0

    def touch(self, min_delay=None):
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


VALID = {
    "id": "valid",
    "date": "2026-07-23",
    "activity": "Admin",
    "hours": 2,
    "created_at": "2026-07-23T08:00:00Z",
    "updated_at": "2026-07-23T08:00:00Z",
}
# A hand edit in the vault JSON: hours typed as a string.
HAND_EDITED = {
    "id": "hand",
    "date": "2026-07-23",
    "activity": "Reading",
    "hours": "1.5",
}


def write_log(store: ActivityStore, value) -> bytes:
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(json.dumps(value), encoding="utf-8")
    return store.path.read_bytes()


def test_activity_writes_keep_entries_that_fail_validation(tmp_path: Path):
    store = ActivityStore(tmp_path / "vault")
    write_log(store, {"version": 1, "entries": [VALID, HAND_EDITED]})

    store.add(date(2026, 7, 23), "Blender", 1)

    saved = read_json(store.path, {})["entries"]
    assert HAND_EDITED in saved
    assert len(saved) == 3
    summary = store.summary(date(2026, 7, 23))
    assert summary["day"]["total_hours"] == 3
    assert [entry["id"] for entry in summary["entries"]].count("hand") == 0
    assert "Reading" not in summary["suggestions"]
    assert store.range_totals(date(2026, 7, 1), date(2026, 7, 31)) == {
        "2026-07-23": 3
    }


def test_activity_update_and_delete_leave_malformed_entries_alone(
    tmp_path: Path,
):
    store = ActivityStore(tmp_path / "vault")
    # Same id as the valid entry, listed first: only the valid one is touched.
    shadow = {"id": "valid", "activity": "Shadow", "hours": 9}
    write_log(store, {"version": 1, "entries": [shadow, VALID, HAND_EDITED]})

    updated = store.update("valid", hours=3)
    assert updated["hours"] == 3
    assert updated["activity"] == "Admin"
    saved = read_json(store.path, {})["entries"]
    assert saved[0] == shadow
    assert saved[1]["hours"] == 3
    assert saved[2] == HAND_EDITED

    removed = store.delete("valid")
    assert removed["activity"] == "Admin"
    assert read_json(store.path, {})["entries"] == [shadow, HAND_EDITED]

    with pytest.raises(NoteError) as missing:
        store.update("hand", hours=1)
    assert missing.value.status == 404
    with pytest.raises(NoteError) as missing:
        store.delete("hand")
    assert missing.value.status == 404
    assert read_json(store.path, {})["entries"] == [shadow, HAND_EDITED]


def test_activity_prune_tolerates_entries_without_a_date(tmp_path: Path):
    store = ActivityStore(tmp_path / "vault")
    store.MAX_ENTRIES = 2
    write_log(
        store,
        {"version": 1, "entries": [{"id": "x", "activity": "?", "hours": 1}, VALID]},
    )

    store.add(date(2026, 7, 24), "Blender", 1)

    saved = read_json(store.path, {})["entries"]
    assert len(saved) == 2
    assert [entry["activity"] for entry in saved] == ["Admin", "Blender"]


def test_activity_refuses_to_rewrite_a_newer_version_file(tmp_path: Path):
    store = ActivityStore(tmp_path / "vault")
    before = write_log(store, {"version": 2, "entries": [VALID]})

    for write in (
        lambda: store.add(date(2026, 7, 23), "Blender", 1),
        lambda: store.update("valid", hours=3),
        lambda: store.delete("valid"),
    ):
        with pytest.raises(NoteError) as refused:
            write()
        assert refused.value.status == 409
        assert "newer app version (v2)" in refused.value.detail
        assert store.path.read_bytes() == before

    # Display-only readers still show what this version understands.
    assert store.summary(date(2026, 7, 23))["day"]["total_hours"] == 2


@pytest.mark.parametrize(
    "stored", [{"version": 1, "entries": [VALID]}, {"entries": [VALID]}]
)
def test_activity_accepts_writes_to_current_and_unversioned_files(
    tmp_path: Path, stored
):
    store = ActivityStore(tmp_path / "vault")
    write_log(store, stored)

    store.add(date(2026, 7, 23), "Blender", 1)

    saved = read_json(store.path, {})
    assert saved["version"] == 1
    assert [entry["activity"] for entry in saved["entries"]] == [
        "Admin",
        "Blender",
    ]
