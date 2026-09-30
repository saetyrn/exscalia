"""Off-site backup push: failures are counted, persisted and surfaced, and the
network push never holds the vault's git lock or outlives its timeout.

Prod's hourly push failed with exit 128 for weeks and the only trace was one
line on the Settings page; a hung remote would also have frozen every commit
behind the lock.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from git import Repo

from app.config import Settings
from app.vault.repo import BackupPusher

TOKEN = "SEKRET-backup-token-123"


def _bare(tmp_path: Path) -> Path:
    bare = tmp_path / "offsite.git"
    Repo.init(bare, bare=True)
    return bare


def _fail(sha, branch):
    raise RuntimeError(f"git push exited 128: fatal: https://x:{TOKEN}@host/v.git")


def _hanging_ssh(tmp_path: Path, monkeypatch) -> tuple[str, Path]:
    """An ssh transport that records its pid and then never answers, so the
    push hangs deterministically without touching the network."""
    pidfile = tmp_path / "ssh.pid"
    monkeypatch.setenv("GIT_SSH_VARIANT", "simple")
    monkeypatch.setenv("GIT_SSH_COMMAND", f"sh -c 'echo $$ > {pidfile}; exec sleep 30'")
    return "git@backup.invalid:vault.git", pidfile


def _gone(pid: int, within: float = 3.0) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def test_failed_push_is_redacted_and_counted(state, tmp_path):
    url = f"https://x-access-token:{TOKEN}@127.0.0.1:9/vault.git"
    pusher = BackupPusher(state.vault, url, interval=3600, state_dir=tmp_path)
    pusher.configure_remote_sync()

    result = asyncio.run(pusher.push())

    assert result["pushed"] is False and result["error"]
    assert TOKEN not in json.dumps(result)
    assert TOKEN not in pusher.last_error
    assert pusher.failures == 1 and pusher.failing_since
    assert not pusher.stale  # one failure is not yet a pattern
    assert TOKEN not in (tmp_path / "backup-status.json").read_text()


def test_repeated_failures_alert_once_a_day_and_success_resets(state, tmp_path, monkeypatch):
    bare = _bare(tmp_path)
    pusher = BackupPusher(state.vault, str(bare), interval=3600, state_dir=tmp_path)
    pusher.configure_remote_sync()
    alerts: list[dict] = []

    async def on_failure(alert):
        alerts.append(alert)

    pusher.on_persistent_failure = on_failure
    monkeypatch.setattr(pusher, "_push_sync", _fail)

    for _ in range(2):
        asyncio.run(pusher.push())
    assert alerts == [] and not pusher.stale

    asyncio.run(pusher.push())
    assert len(alerts) == 1
    alert = alerts[0]
    today = datetime.now(timezone.utc).date().isoformat()
    assert alert["key"] == f"backup:failing:{today}"
    assert alert["source"] == "omnia"
    assert alert["title"] == f"Vault backup failing since {pusher.failing_since[:10]}"
    assert TOKEN not in json.dumps(alert)
    assert pusher.stale

    # A fourth failure the same day stays quiet — also across a restart.
    asyncio.run(pusher.push())
    assert len(alerts) == 1
    restarted = BackupPusher(state.vault, str(bare), interval=3600, state_dir=tmp_path)
    restarted.on_persistent_failure = on_failure
    monkeypatch.setattr(restarted, "_push_sync", _fail)
    asyncio.run(restarted.push())
    assert len(alerts) == 1 and restarted.failures == 5

    # A success clears the whole failure state.
    monkeypatch.undo()
    result = asyncio.run(restarted.push())
    assert result["pushed"] is True
    assert restarted.failures == 0
    assert restarted.failing_since is None and restarted.last_error is None
    assert not restarted.stale
    assert Repo(bare).commit("main").hexsha == state.vault.repo.head.commit.hexsha


def test_push_does_not_hold_the_vault_lock(state, tmp_path, monkeypatch):
    bare = _bare(tmp_path)
    pusher = BackupPusher(state.vault, str(bare), interval=3600)
    pusher.configure_remote_sync()
    monkeypatch.setattr(pusher, "_push_sync", lambda sha, branch: time.sleep(3.0))

    async def scenario():
        push = asyncio.create_task(pusher.push())
        await asyncio.sleep(0.1)
        state.store.create("During Push.md", "---\ntitle: During Push\n---\n\nedit")
        # Blocked for the whole push before; now it commits straight away.
        sha = await asyncio.wait_for(state.vault.commit_all("user edit"), 2.0)
        return sha, await push

    sha, result = asyncio.run(scenario())
    assert sha is not None
    assert result["pushed"] is True
    # The push carried the HEAD it resolved, not the commit made meanwhile.
    assert result["commit"] != sha and pusher.last_pushed_sha == result["commit"]


def test_hung_push_times_out_and_kills_the_transport(state, tmp_path, monkeypatch):
    url, pidfile = _hanging_ssh(tmp_path, monkeypatch)
    pusher = BackupPusher(state.vault, url, interval=3600)
    pusher.configure_remote_sync()
    pusher.PUSH_TIMEOUT = 1.0

    started = time.monotonic()
    result = asyncio.run(pusher.push())

    assert time.monotonic() - started < 10
    assert result["pushed"] is False and "timed out" in result["error"]
    assert pusher.failures == 1
    # Git's transport child went down with it, not just the git process.
    assert _gone(int(pidfile.read_text()))


def test_shutdown_push_is_bounded_and_kills_git(state, tmp_path, monkeypatch):
    url, pidfile = _hanging_ssh(tmp_path, monkeypatch)
    pusher = BackupPusher(state.vault, url, interval=3600)
    pusher.configure_remote_sync()

    started = time.monotonic()
    asyncio.run(pusher.final_push(timeout=1.0))

    assert time.monotonic() - started < 5
    assert _gone(int(pidfile.read_text()))


def test_shutdown_push_bounds_a_stuck_worker(state, tmp_path, monkeypatch):
    bare = _bare(tmp_path)
    pusher = BackupPusher(state.vault, str(bare), interval=3600)
    pusher.configure_remote_sync()
    release = threading.Event()
    monkeypatch.setattr(pusher, "_push_sync", lambda sha, branch: release.wait(5))

    async def scenario():
        started = time.monotonic()
        await pusher.final_push(timeout=0.2)
        elapsed = time.monotonic() - started
        release.set()  # let the worker thread finish so the loop can close
        return elapsed

    assert asyncio.run(scenario()) < 1.0


def test_status_survives_a_restart(state, tmp_path):
    bare = _bare(tmp_path)
    pusher = BackupPusher(state.vault, str(bare), interval=3600, state_dir=tmp_path)
    pusher.configure_remote_sync()
    assert asyncio.run(pusher.push())["pushed"] is True

    saved = json.loads((tmp_path / "backup-status.json").read_text())
    assert saved["last_pushed_sha"] == state.vault.repo.head.commit.hexsha

    fresh = BackupPusher(state.vault, str(bare), interval=3600, state_dir=tmp_path)
    assert fresh.last_pushed_sha == pusher.last_pushed_sha
    assert fresh.last_push == pusher.last_push
    assert asyncio.run(fresh.push()) == {"pushed": False, "reason": "nothing new to push"}

    # A different remote has none of that history, so it gets a full push.
    other = BackupPusher(state.vault, str(tmp_path / "elsewhere.git"), 3600, state_dir=tmp_path)
    assert other.last_pushed_sha is None


def test_corrupt_status_file_starts_fresh(state, tmp_path):
    (tmp_path / "backup-status.json").write_text("{not json")
    pusher = BackupPusher(state.vault, "unused", interval=3600, state_dir=tmp_path)
    assert pusher.failures == 0 and pusher.last_pushed_sha is None
    assert list(tmp_path.glob("backup-status.json.corrupt-*"))


def test_stale_counts_from_the_later_of_last_success_and_boot(state, tmp_path):
    """A restart after downtime is not a stale backup: the first push is still
    an interval away. Three silent intervals after boot are."""
    day_ago = datetime.now(timezone.utc) - timedelta(days=1)
    (tmp_path / "backup-status.json").write_text(
        json.dumps({"last_success": day_ago.strftime("%Y-%m-%dT%H:%M:%SZ")})
    )
    pusher = BackupPusher(state.vault, "unused", interval=3600, state_dir=tmp_path)
    assert pusher.last_success and not pusher.stale

    pusher._booted -= timedelta(hours=4)
    assert pusher.stale


def test_briefing_and_status_flag_a_failing_backup(tmp_path: Path, monkeypatch):
    """End to end through the app: the third failure queues an alert, and the
    status route and the morning briefing both report the backup stale."""
    from app import main as main_module

    bare = _bare(tmp_path)
    settings = Settings(
        data_dir=tmp_path / "app-data",
        frontend_dir=Path(__file__).resolve().parents[2] / "frontend" / "build",
        embedding_backend="none",
        autocommit_debounce=0.01,
        auth_password=None,
        anthropic_api_key=None,
        openai_api_key=None,
        google_api_key=None,
        backup_remote=str(bare),
    )
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)

    with TestClient(main_module.app) as client:
        briefing = client.post("/api/overseer/morning").json()["briefing"]
        assert briefing["backup_stale"] is False

        pusher = client.app.state.backup
        pusher._push_sync = _fail
        for _ in range(3):
            assert client.post("/api/backup/push").json()["pushed"] is False

        status = client.get("/api/backup/status").json()
        assert status["failures"] == 3 and status["stale"] is True
        assert status["failing_since"]
        briefing = client.post("/api/overseer/morning").json()["briefing"]
        assert briefing["backup_stale"] is True
        assert briefing["backup_failing_since"] == status["failing_since"]

        queued = client.get("/api/overseer/alerts").json()["alerts"]
        assert [a["key"][:15] for a in queued] == ["backup:failing:"]
        assert TOKEN not in json.dumps(queued)

        del pusher._push_sync
        assert client.post("/api/backup/push").json()["pushed"] is True
        assert client.get("/api/backup/status").json()["stale"] is False
