"""Boot and maintenance must survive what a SIGKILL or a hand-edited file
leaves behind: a stale git lock, a broken Trash manifest, one failing step."""
import asyncio
import logging
import time
import uuid
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from git import Repo
from git.exc import GitCommandError

from app.config import Settings
from app.overseer.memory import MEMORY_PATH, MEMORY_TEMPLATE
from app.task_service import migrate_task_metadata
from app.vault.internal import TrashStore, atomic_json, iso_utc, utc_now
from app.vault.notes import NoteStore
from app.vault.repo import VaultRepo


def _settings(tmp_path: Path, **overrides) -> Settings:
    values = dict(
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
    values.update(overrides)
    return Settings(**values)


def _killed_mid_commit(settings: Settings) -> Path:
    """A vault as a SIGKILL during `git add` leaves it: an edit not yet
    committed, and git's index.lock still on disk. Returns the lock path."""
    vault_dir = settings.vault_dir
    vault = VaultRepo(vault_dir, settings.git_author_name, settings.git_author_email)
    vault.ensure()
    # A vault from an earlier run: already migrated, so the boot takes the
    # plain recovery path, and OMNIA's memory note (whose creation would
    # nudge the autocommitter by itself) already exists.
    store = NoteStore(vault_dir)
    store.create(MEMORY_PATH, MEMORY_TEMPLATE)
    migrate_task_metadata(store, vault_dir)
    vault.commit_all_sync("Migrate task lifecycle metadata", vault.user_author)
    (vault_dir / "Unsaved.md").write_text(
        "---\ntitle: Unsaved\n---\n\nTyped just before the kill.\n", encoding="utf-8"
    )
    lock = vault_dir / ".git" / "index.lock"
    lock.touch()
    with pytest.raises(GitCommandError):  # the lock really does block git add
        vault.commit_all_sync("autosave", vault.user_author)
    return lock


def test_boot_clears_a_stale_index_lock_and_commits_the_leftovers(
    tmp_path: Path, monkeypatch
):
    from app import main as main_module

    settings = _settings(tmp_path)
    lock = _killed_mid_commit(settings)
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)

    with TestClient(main_module.app) as client:
        assert client.get("/api/health").status_code == 200
        assert not lock.exists()
        repo = Repo(settings.vault_dir)
        recover = next(
            commit
            for commit in repo.iter_commits(max_count=20)
            if commit.message.startswith("Recover working tree after restart")
        )
        blob = recover.tree / "Unsaved.md"
        assert b"Typed just before the kill." in blob.data_stream.read()


def test_boot_survives_a_git_failure_it_cannot_clear(
    tmp_path: Path, monkeypatch, caplog
):
    from app import main as main_module

    settings = _settings(tmp_path)
    lock = _killed_mid_commit(settings)
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    # The boot-time commit guard on its own, without the lock sweep behind it.
    monkeypatch.setattr(VaultRepo, "_clear_stale_locks", lambda self: None)

    with caplog.at_level(logging.ERROR):
        with TestClient(main_module.app) as client:
            assert client.get("/api/health").status_code == 200
            assert lock.exists()
            assert (settings.vault_dir / "Unsaved.md").is_file()
            # The autocommitter retries on its own, without waiting for an edit.
            deadline = time.monotonic() + 5
            while "autosave commit failed" not in caplog.text:
                assert time.monotonic() < deadline, "the autocommitter never retried"
                time.sleep(0.02)
            # Once git works again, the shutdown autosave sweeps it up.
            lock.unlink()
    assert "boot commit failed; the autocommitter will retry" in caplog.text

    head = Repo(settings.vault_dir).head.commit
    assert b"Typed just before the kill." in (
        (head.tree / "Unsaved.md").data_stream.read()
    )


def test_purge_expired_skips_hand_broken_entries(tmp_path: Path, caplog):
    store = NoteStore(tmp_path)
    store.create("Old.md", "---\ntitle: Old\n---\n\nGone.\n")
    store.create("Fresh.md", "---\ntitle: Fresh\n---\n\nKept.\n")
    trash = TrashStore(tmp_path, store)
    past = iso_utc(utc_now() - timedelta(days=1))

    old = trash.trash_note("Old.md")
    atomic_json(trash.root / old["id"] / "manifest.json", {**old, "expires_at": past})
    fresh = trash.trash_note("Fresh.md")

    def hand_edited(dirname: str, **fields) -> Path:
        entry = trash.root / dirname
        (entry / "payload").mkdir(parents=True)
        atomic_json(
            entry / "manifest.json",
            {
                "id": dirname,
                "kind": "note",
                "original_path": f"{dirname}.md",
                "deleted_at": "2020-01-01T00:00:00Z",
                "expires_at": past,
                "note_count": 1,
                **fields,
            },
        )
        return entry

    broken = [
        # parse_utc(None) -> AttributeError; a null deleted_at broke the sort.
        hand_edited(uuid.uuid4().hex, expires_at=None, deleted_at=None),
        # Naive timestamp -> TypeError against the aware clock.
        hand_edited(uuid.uuid4().hex, expires_at="2020-01-31T00:00:00"),
        # Non-hex id -> NoteError(400) from _entry.
        hand_edited("not-a-hex-id"),
        # Hex id that is not its own directory -> NoteError(404).
        hand_edited("restored-by-hand", id=uuid.uuid4().hex),
    ]

    with caplog.at_level(logging.WARNING, logger="app.vault.internal"):
        removed = trash.purge_expired()

    assert [item["id"] for item in removed] == [old["id"]]
    assert not (trash.root / old["id"]).exists()
    assert (trash.root / fresh["id"]).exists()
    assert all(entry.exists() for entry in broken)
    assert caplog.text.count("skipping trash entry") == len(broken)


def test_maintenance_once_runs_every_step_past_a_failing_one(caplog):
    from app.main import maintenance_once

    class Trash:
        def __init__(self):
            self.calls = 0

        def purge_expired(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("hand-edited manifest")
            return [{"id": "0123abcd"}]

    class Semantic:
        def __init__(self):
            self.calls = 0

        async def optimize(self):
            self.calls += 1

    class Db:
        def __init__(self):
            self.executed: list[str] = []

        async def execute(self, sql):
            self.executed.append(sql)

    touched: list[bool] = []
    state = SimpleNamespace(
        trash=Trash(),
        semantic=Semantic(),
        db=Db(),
        autocommit=SimpleNamespace(touch=lambda: touched.append(True)),
    )

    with caplog.at_level(logging.ERROR, logger="app.main"):
        asyncio.run(maintenance_once(state))
        asyncio.run(maintenance_once(state))

    assert state.trash.calls == 2
    assert state.semantic.calls == 2
    assert state.db.executed == ["PRAGMA wal_checkpoint(TRUNCATE)"] * 2
    assert touched == [True]  # only the pass that purged something
    assert "trash purge failed" in caplog.text
    assert "hand-edited manifest" in caplog.text


def test_api_schema_and_docs_are_not_served(tmp_path: Path, monkeypatch):
    from app import main as main_module

    settings = _settings(tmp_path, auth_username="owner", auth_password="secret")
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    # Whether the SPA catch-all exists is decided at import, from the real
    # settings: without a frontend build these are plain 404s, with one they
    # fall through to the app shell.
    spa = main_module.settings.frontend_dir.is_dir()
    shell = main_module.settings.frontend_dir / "index.html"

    with TestClient(main_module.app) as client:
        for path in ("/openapi.json", "/docs", "/redoc", "/docs/oauth2-redirect"):
            response = client.get(path)
            assert '"openapi"' not in response.text, path
            assert "swagger-ui" not in response.text, path
            assert "redoc.standalone" not in response.text, path
            if spa:
                assert response.status_code == 200, path
                assert response.text == shell.read_text(encoding="utf-8"), path
            else:
                assert response.status_code == 404, path
        assert client.get("/api/notes").status_code == 401

    # The schema can still be produced offline, e.g. for type generation.
    assert main_module.app.openapi()["info"]["title"] == "EXSCALIA"
