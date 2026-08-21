import asyncio
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app.api import calendar as calendar_api
from app.api import meta as meta_api
from app.api import notes as notes_api
from app.api import recovery as recovery_api
from app.activity_store import ActivityStore
from app.config import Settings
from app.event_store import EventStore
from app.index import parse
from app.index.db import content_hash, open_db
from app.live import LiveBus
from app.index.indexer import Indexer
from app.overseer.threads import ThreadStore
from app.overseer.actions import ActionCoordinator
from app.overseer.tools import execute_tool
from app.task_service import create_task, delete_task, migrate_task_metadata, update_task
from app.vault.internal import TaskTombstones, TrashStore, read_json
from app.vault import fm
from app.vault.notes import NoteError, NoteStore
from app.vault.repo import VaultRepo


class Touch:
    def __init__(self):
        self.count = 0

    def touch(self):
        self.count += 1


class NoSemantic:
    available = False

    async def search(self, query, k=8):
        return []


@pytest.fixture
def upgraded_state(tmp_path: Path):
    vault_dir = tmp_path / "vault"
    vault = VaultRepo(vault_dir, "user", "user@test")
    vault.ensure()
    store = NoteStore(vault_dir)
    tombstones = TaskTombstones(vault_dir)

    async def build():
        db = await open_db(tmp_path / "index")
        indexer = Indexer(db, store, tombstones)
        await indexer.sync_all()
        return SimpleNamespace(
            db=db,
            store=store,
            vault=vault,
            indexer=indexer,
            autocommit=Touch(),
            live=LiveBus(),
            trash=TrashStore(vault_dir, store),
            task_tombstones=tombstones,
            semantic=NoSemantic(),
            settings=SimpleNamespace(vault_dir=vault_dir, data_dir=tmp_path),
            timezone="America/New_York",
            activities=ActivityStore(vault_dir),
            events=EventStore(vault_dir),
            backup=None,
        )

    state = asyncio.run(build())
    yield state
    asyncio.run(state.db.close())


def request_for(state):
    return SimpleNamespace(app=SimpleNamespace(state=state))


def test_appearance_sync_is_git_backed_and_survives_timezone_save(upgraded_state):
    state = upgraded_state
    request = request_for(state)
    initial = asyncio.run(meta_api.appearance_settings(request))
    assert initial["configured"] is False

    colors = {name: "#123456" for name in meta_api.THEME_COLOR_VARS}
    custom = {"mode": "dark", "colors": colors}
    saved = asyncio.run(
        meta_api.save_appearance_settings(
            request,
            meta_api.AppearanceSettingsBody(
                theme="custom",
                font="serif",
                intoxicate=True,
                custom_theme=custom,
                saved_themes=[
                    {"id": "midnight", "name": "Midnight", "theme": custom}
                ],
                active_saved_theme_id="midnight",
            ),
        )
    )
    assert saved["configured"] is True
    assert saved["active_saved_theme_id"] == "midnight"
    assert asyncio.run(meta_api.appearance_settings(request)) == saved

    asyncio.run(
        meta_api.save_general_settings(
            request, meta_api.GeneralSettingsBody(timezone="Europe/London")
        )
    )
    config = read_json(
        state.settings.vault_dir / NoteStore.INTERNAL_DIR / "config.json", {}
    )
    assert config["timezone"] == "Europe/London"
    assert config["appearance"]["saved_themes"][0]["name"] == "Midnight"


def test_revision_conflict_creates_lossless_copy(upgraded_state):
    state = upgraded_state
    request = request_for(state)
    state.store.create("Shared.md", "---\ntitle: Shared\n---\n\nbase")
    asyncio.run(state.indexer.index_note("Shared.md", state.store.read("Shared.md")))

    first = asyncio.run(notes_api.read_note(request, "Shared.md"))
    saved = asyncio.run(
        notes_api.save_note(
            request,
            "Shared.md",
            notes_api.SaveNote(content=first["content"] + "\nfirst", base_revision=first["revision"]),
        )
    )
    assert saved["status"] == "saved"

    conflict = asyncio.run(
        notes_api.save_note(
            request,
            "Shared.md",
            notes_api.SaveNote(content=first["content"] + "\nsecond", base_revision=first["revision"]),
        )
    )
    assert conflict["status"] == "conflict_copy"
    assert "first" in state.store.read("Shared.md")
    assert "second" in state.store.read(conflict["conflict_path"])


def test_autosave_preserves_indented_body_boundaries(upgraded_state):
    state = upgraded_state
    request = request_for(state)
    state.store.create("Indented.md", "Initial body")
    first = asyncio.run(notes_api.read_note(request, "Indented.md"))
    body = "\tfirst indented line\n\t\tsecond indented line\ntrailing tab\t"
    submitted = fm.serialize(first["meta"], body)

    saved = asyncio.run(
        notes_api.save_note(
            request,
            "Indented.md",
            notes_api.SaveNote(content=submitted, base_revision=first["revision"]),
        )
    )

    assert fm.parse(saved["content"])[1] == body
    assert fm.parse(state.store.read("Indented.md"))[1] == body


def test_task_identity_survives_repeated_editor_saves(upgraded_state):
    state = upgraded_state
    request = request_for(state)
    state.store.create("Stable task.md", "- [ ] Keep this identity\n")
    asyncio.run(state.indexer.index_note("Stable task.md", state.store.read("Stable task.md")))

    first = asyncio.run(notes_api.read_note(request, "Stable task.md"))
    saved = asyncio.run(
        notes_api.save_note(
            request,
            "Stable task.md",
            notes_api.SaveNote(
                content=first["content"] + "\nA note below the task.\n",
                base_revision=first["revision"],
            ),
        )
    )
    _meta, body = fm.parse(saved["content"])
    first_id = parse.extract_tasks(
        body, parse.frontmatter_line_count(saved["content"], body)
    )[0].id

    saved_again = asyncio.run(
        notes_api.save_note(
            request,
            "Stable task.md",
            notes_api.SaveNote(
                content=saved["content"] + "Another edit.\n",
                base_revision=saved["revision"],
            ),
        )
    )
    _meta, body = fm.parse(saved_again["content"])
    second_id = parse.extract_tasks(
        body, parse.frontmatter_line_count(saved_again["content"], body)
    )[0].id
    assert first_id == second_id


def test_task_lifecycle_first_dates_and_tombstone(upgraded_state):
    state = upgraded_state
    task = asyncio.run(create_task(state, text="Ship release", due="2030-01-02"))
    assert task["path"] == "Tasks Inbox.md"
    assert task["status"] == "todo"
    assert task["created_at"]
    assert task["started_at"] is None

    started = asyncio.run(update_task(state, task["id"], status="in_progress"))
    first_started = started["started_at"]
    completed = asyncio.run(update_task(state, task["id"], status="done"))
    first_completed = completed["completed_at"]
    reopened = asyncio.run(update_task(state, task["id"], status="todo"))
    assert reopened["started_at"] == first_started
    assert reopened["completed_at"] == first_completed
    assert reopened["reopened_at"]

    asyncio.run(delete_task(state, task["id"]))
    tombstone = state.task_tombstones.all()[task["id"]]
    assert tombstone["started_at"] == first_started
    assert tombstone["completed_at"] == first_completed
    assert tombstone["reopened_at"] == reopened["reopened_at"]


def test_task_migration_is_idempotent_and_preserves_visible_due(tmp_path: Path):
    vault_dir = tmp_path / "vault"
    vault = VaultRepo(vault_dir, "user", "user@test")
    vault.ensure()
    store = NoteStore(vault_dir)
    store.create("Legacy.md", "- [x] old task 📅 2030-04-05\n")

    changed = migrate_task_metadata(store, vault_dir)
    first = store.read("Legacy.md")
    assert "Legacy.md" in changed
    assert "exscalia-task:" in first
    assert '"completed_at":' in first
    assert "📅 2030-04-05" in first
    assert migrate_task_metadata(store, vault_dir) == []
    assert store.read("Legacy.md") == first


def test_trash_restore_and_history_restore(upgraded_state):
    state = upgraded_state
    request = request_for(state)
    state.store.create("Recover.md", "---\ntitle: Recover\n---\n\none")
    asyncio.run(state.indexer.index_note("Recover.md", state.store.read("Recover.md")))
    asyncio.run(state.vault.commit_all("Add recoverable note"))
    old_sha = state.vault.repo.head.commit.hexsha
    state.store.write("Recover.md", "---\ntitle: Recover\n---\n\ntwo")
    asyncio.run(state.indexer.index_note("Recover.md", state.store.read("Recover.md")))

    restored = asyncio.run(
        recovery_api.restore_history(
            request,
            recovery_api.RestoreHistoryBody(
                path="Recover.md", sha=old_sha, mode="copy", target_path="Recovered copy.md"
            ),
        )
    )
    assert restored["path"] == "Recovered copy.md"
    assert "one" in state.store.read("Recovered copy.md")
    assert "two" in state.store.read("Recover.md")

    manifest = state.trash.trash_note("Recover.md")
    asyncio.run(state.indexer.remove_note("Recover.md"))
    assert not state.store.exists("Recover.md")
    result = state.trash.restore(manifest["id"])
    for path in result["notes"]:
        asyncio.run(state.indexer.index_note(path, state.store.read(path)))
    assert "two" in state.store.read("Recover.md")


def test_history_follows_note_renames(upgraded_state):
    state = upgraded_state
    state.store.create("Before.md", "---\ntitle: Before\n---\n\noriginal body")
    asyncio.run(state.vault.commit_all("Create note before rename"))
    state.store.rename("Before.md", "After.md")
    asyncio.run(state.vault.commit_all("Rename note"))

    versions = state.vault.history_sync("After.md")
    old = next(version for version in versions if version["version_path"] == "Before.md")
    assert "original body" in old["preview"]
    assert (
        state.vault.commit_content_sync(old["sha"], old["version_path"]) is not None
    )


def test_unified_search_returns_structured_safe_highlights(upgraded_state):
    state = upgraded_state
    request = request_for(state)
    content = state.store.create(
        "Unsafe.md",
        "---\ntitle: Unsafe\n---\n\nneedle <img src=x onerror=alert(1)>",
    )
    asyncio.run(state.indexer.index_note("Unsafe.md", content))
    response = asyncio.run(
        meta_api.unified_search(
            request,
            q="needle",
            limit=30,
            folder=None,
            tag=None,
            result_type="all",
            created_from=None,
            modified_from=None,
        )
    )
    result = next(item for item in response["results"] if item["path"] == "Unsafe.md")
    assert "<mark>" not in result["snippet"]
    assert any(segment["highlight"] for segment in result["highlights"])


def test_unified_search_strongly_ranks_exact_titles_and_tags(upgraded_state):
    state = upgraded_state
    request = request_for(state)
    exact = state.store.create(
        "Needle.md",
        "---\ntitle: Needle\n---\n\n#needle exact-title content",
    )
    incidental = state.store.create(
        "Other.md",
        "---\ntitle: Other\n---\n\nThis paragraph happens to mention needle.",
    )
    asyncio.run(state.indexer.index_note("Needle.md", exact))
    asyncio.run(state.indexer.index_note("Other.md", incidental))
    response = asyncio.run(
        meta_api.unified_search(
            request,
            q="needle",
            limit=30,
            folder=None,
            tag=None,
            result_type="note",
            created_from=None,
            modified_from=None,
        )
    )
    assert response["results"][0]["path"] == "Needle.md"


def test_thread_store_survives_reconstruction(tmp_path: Path):
    first = ThreadStore(tmp_path)
    daily, created = first.ensure_daily("2026-07-23", "Thursday, July 23")
    assert created
    first.append_public(daily["id"], {"role": "assistant", "text": "Morning."})
    named = first.create()
    first.append_public(named["id"], {"role": "user", "text": "Plan my studio renovation"})

    second = ThreadStore(tmp_path)
    assert second.get(daily["id"])["messages"][0]["text"] == "Morning."
    assert second.get(named["id"])["title"] == "Plan my studio renovation"
    assert len(second.list()) == 2


def test_thread_store_marks_abandoned_generation_interrupted(tmp_path: Path):
    store = ThreadStore(tmp_path)
    thread = store.create()
    message = store.append_public(
        thread["id"], {"role": "assistant", "text": "Partial", "status": "streaming"}
    )
    assert store.reconcile_incomplete() == 1
    restored = store.get(thread["id"])
    persisted = next(item for item in restored["messages"] if item["id"] == message["id"])
    assert persisted["text"] == "Partial"
    assert persisted["status"] == "interrupted"


def test_calendar_contains_task_milestones(upgraded_state):
    state = upgraded_state
    request = request_for(state)
    task = asyncio.run(create_task(state, text="Calendar task"))
    asyncio.run(update_task(state, task["id"], status="in_progress"))
    # Milestones are stored in UTC and bucketed into the user's local day, so
    # the expected day must be converted the same way. Slicing the raw UTC
    # timestamp made this fail for the hours where the two dates differ —
    # every evening in a western timezone.
    created_day = calendar_api._local_day(
        task["created_at"], calendar_api._timezone(request)
    )
    day = asyncio.run(calendar_api.day_view(request, created_day))
    kinds = {event["type"] for event in day["task_events"]}
    assert "created" in kinds
    assert "started" in kinds


def test_omnia_direct_action_is_scoped_and_undoable(upgraded_state):
    state = upgraded_state
    state.actions = ActionCoordinator(state)
    unrelated = state.settings.vault_dir / "Unrelated.md"
    unrelated.write_text("unsaved user work", encoding="utf-8")

    result = json.loads(
        asyncio.run(
            execute_tool(
                state,
                "thread-a",
                "create_note",
                {"path": "Created by OMNIA.md", "content": "hello"},
                auth_session_id="device-a",
            )
        )
    )
    action = result["_action"]
    assert action["commit"]
    assert action["undoable"]
    assert state.store.exists("Created by OMNIA.md")
    assert unrelated.read_text(encoding="utf-8") == "unsaved user work"
    assert "Unrelated.md" not in state.vault.repo.head.commit.stats.files

    undone = asyncio.run(state.actions.undo(action["id"], "thread-a"))
    assert undone["commit"]
    assert not state.store.exists("Created by OMNIA.md")
    assert unrelated.read_text(encoding="utf-8") == "unsaved user work"


def test_omnia_confirmation_is_bound_to_device_and_thread(upgraded_state):
    state = upgraded_state
    state.actions = ActionCoordinator(state)
    content = state.store.create("Confirm delete.md", "keep until approved")
    asyncio.run(state.indexer.index_note("Confirm delete.md", content))
    asyncio.run(state.vault.commit_paths("Add confirmation fixture", ["Confirm delete.md"]))

    pending = json.loads(
        asyncio.run(
            execute_tool(
                state,
                "thread-a",
                "delete_note",
                {
                    "path": "Confirm delete.md",
                    "base_revision": content_hash(state.store.read("Confirm delete.md")),
                },
                auth_session_id="device-a",
            )
        )
    )["confirmation_required"]
    assert state.store.exists("Confirm delete.md")
    with pytest.raises(NoteError):
        state.actions.consume_confirmation(pending["token"], "thread-a", "device-b")
    expiring = state.actions.request_confirmation(
        "thread-a", "delete_note", {"path": "Confirm delete.md"}, "device-a"
    )
    state.actions._confirmations[expiring["token"]]["expires_at"] = (
        "2000-01-01T00:00:00Z"
    )
    with pytest.raises(NoteError):
        state.actions.consume_confirmation(
            expiring["token"], "thread-a", "device-a"
        )

    # A failed cross-device approval consumes no authority: request a fresh card.
    pending = json.loads(
        asyncio.run(
            execute_tool(
                state,
                "thread-a",
                "delete_note",
                {
                    "path": "Confirm delete.md",
                    "base_revision": content_hash(state.store.read("Confirm delete.md")),
                },
                auth_session_id="device-a",
            )
        )
    )["confirmation_required"]
    item = state.actions.consume_confirmation(
        pending["token"], "thread-a", "device-a"
    )
    applied = json.loads(
        asyncio.run(
            execute_tool(
                state,
                "thread-a",
                item["tool"],
                item["inputs"],
                confirmed=True,
                auth_session_id="device-a",
            )
        )
    )
    assert applied["_action"]["undoable"]
    assert not state.store.exists("Confirm delete.md")
    assert state.trash.list()[0]["original_path"] == "Confirm delete.md"


def test_omnia_activity_whole_hours_and_recurring_events(upgraded_state):
    state = upgraded_state
    state.actions = ActionCoordinator(state)
    activity = json.loads(
        asyncio.run(
            execute_tool(
                state,
                "thread-a",
                "add_activity",
                {"date": "2026-07-23", "activity": "admin", "hours": 8},
            )
        )
    )
    assert activity["entry"]["hours"] == 8
    assert activity["summary"]["day"]["total_hours"] == 8

    event = state.events.add(
        state.events.normalize(
            title="Month end",
            start="2026-01-31T09:00",
            end="2026-01-31T10:00",
            all_day=False,
            timezone_name="America/New_York",
            notes=None,
            note_path=None,
            recurrence={"frequency": "monthly", "interval": 1},
            reminders=[60],
        )
    )
    occurrences = state.events.occurrences(
        datetime.fromisoformat("2026-01-01").date(),
        datetime.fromisoformat("2026-04-30").date(),
    )
    assert [item["occurrence_date"] for item in occurrences] == [
        "2026-01-31",
        "2026-03-31",
    ]
    assert all(item["event_id"] == event["id"] for item in occurrences)

    state.events.add(
        state.events.normalize(
            title="DST-safe weekly",
            start="2026-03-01T09:00",
            end="2026-03-01T10:00",
            all_day=False,
            timezone_name="America/New_York",
            notes=None,
            note_path=None,
            recurrence={
                "frequency": "weekly",
                "interval": 1,
                "weekdays": [6],
                "count": 3,
            },
            reminders=[],
        )
    )
    dst = [
        item
        for item in state.events.occurrences(
            datetime.fromisoformat("2026-03-01").date(),
            datetime.fromisoformat("2026-03-20").date(),
        )
        if item["title"] == "DST-safe weekly"
    ]
    assert [item["occurrence_start"][11:] for item in dst] == ["09:00"] * 3
    assert dst[0]["occurrence_start_utc"].endswith("14:00:00Z")
    assert dst[-1]["occurrence_start_utc"].endswith("13:00:00Z")


def test_full_application_lifespan_api_smoke(tmp_path: Path, monkeypatch):
    from app import main as main_module

    settings = Settings(
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
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)

    with TestClient(main_module.app) as client:
        assert client.get("/api/health").json() == {"ok": True}
        created = client.post(
            "/api/tasks",
            json={
                "text": "End-to-end smoke task",
                "due": "2030-05-06",
                "due_visible": False,
            },
        )
        assert created.status_code == 200
        task = created.json()
        assert task["status"] == "todo"
        assert task["due_visible"] is False

        started = client.patch(
            f"/api/tasks/{task['id']}", json={"status": "in_progress"}
        )
        assert started.status_code == 200
        assert started.json()["started_at"]

        local_day = (
            datetime.fromisoformat(task["created_at"].replace("Z", "+00:00"))
            .astimezone(ZoneInfo(settings.timezone))
            .date()
            .isoformat()
        )
        events = client.get(f"/api/calendar/day/{local_day}").json()["task_events"]
        assert {"created", "started"}.issubset({event["type"] for event in events})

        search = client.get(
            "/api/search/unified", params={"q": "End-to-end smoke task"}
        ).json()
        assert any(
            result["type"] == "task" and result["id"] == task["id"]
            for result in search["results"]
        )

        note_path = quote(task["path"], safe="/")
        assert client.delete(f"/api/notes/{note_path}").status_code == 200
        trash = client.get("/api/trash").json()["items"]
        item = next(entry for entry in trash if entry["original_path"] == task["path"])
        assert (
            client.post(f"/api/trash/{item['id']}/restore", json={}).status_code
            == 200
        )
        assert client.get(f"/api/notes/{note_path}").status_code == 200

        page = client.get("/tasks")
        assert page.status_code == 200
        assert "kit.start" in page.text


def test_full_auto_skips_the_confirmation_but_not_the_action_record(upgraded_state):
    """Full auto answers yes before the question exists: confirmed-tier tools
    execute immediately — but every one still lands as an undoable action, or
    the mode would be a footgun instead of a convenience."""
    state = upgraded_state
    state.actions = ActionCoordinator(state)
    state.omnia_full_auto = True
    content = state.store.create("Auto delete.md", "goes without asking")
    asyncio.run(state.indexer.index_note("Auto delete.md", content))
    asyncio.run(state.vault.commit_paths("Add full-auto fixture", ["Auto delete.md"]))

    result = json.loads(
        asyncio.run(
            execute_tool(
                state,
                "thread-a",
                "delete_note",
                {
                    "path": "Auto delete.md",
                    "base_revision": content_hash(state.store.read("Auto delete.md")),
                },
                auth_session_id="device-a",
            )
        )
    )
    assert "confirmation_required" not in result
    assert not state.store.exists("Auto delete.md")
    recorded = state.actions.list(limit=5)
    assert any(
        a.get("tool") == "delete_note" and a.get("undoable") for a in recorded
    ), recorded

    # Off again: the same call goes back to asking first.
    state.omnia_full_auto = False
    content = state.store.create("Ask again.md", "needs a yes")
    asyncio.run(state.indexer.index_note("Ask again.md", content))
    result = json.loads(
        asyncio.run(
            execute_tool(
                state,
                "thread-a",
                "delete_note",
                {
                    "path": "Ask again.md",
                    "base_revision": content_hash(state.store.read("Ask again.md")),
                },
                auth_session_id="device-a",
            )
        )
    )
    assert "confirmation_required" in result
    assert state.store.exists("Ask again.md")
