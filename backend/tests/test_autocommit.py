"""AutoCommitter: the only thing that turns the user's saves into git history.

Version history, Undo, Trash recovery and the off-site backup all assume its
commits happen, and every fixture stubs it out. These run the real class
against real vaults in tmp_path, with timings scaled down.
"""

import asyncio
import hashlib
import time
from pathlib import Path
from types import SimpleNamespace

from git import Repo

from app.api import meta as meta_api
from app.api import paper as paper_api
from app.vault.repo import AutoCommitter, VaultRepo
from tests.test_paper_api import make_client, new_notebook, put_page

DEBOUNCE = 0.02
MAX_WAIT = 0.1


def make_vault(tmp_path: Path) -> VaultRepo:
    vault = VaultRepo(tmp_path / "vault", "user", "user@test")
    vault.ensure()
    return vault


def edit(vault: VaultRepo, text: str, name: str = "Note.md") -> None:
    (vault.dir / name).write_text(text, encoding="utf-8")


def autosaves(vault: VaultRepo) -> list[str]:
    """Autosave commit shas, newest first. A Repo of its own: the committer's
    thread may be using the vault's at the same moment."""
    repo = Repo(vault.dir)
    try:
        return [
            c.hexsha for c in repo.iter_commits() if c.message.strip() == "autosave"
        ]
    finally:
        repo.close()


def files_in(vault: VaultRepo, sha: str) -> set[str]:
    repo = Repo(vault.dir)
    try:
        return set(repo.commit(sha).stats.files)
    finally:
        repo.close()


async def until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out waiting"
        await asyncio.sleep(0.01)


def test_a_touch_makes_one_autosave_commit(tmp_path):
    vault = make_vault(tmp_path)

    async def run():
        committer = AutoCommitter(vault, DEBOUNCE, max_wait=MAX_WAIT)
        committer.start()
        edit(vault, "first")
        committer.touch()
        await until(lambda: len(autosaves(vault)) == 1)
        await asyncio.sleep(MAX_WAIT * 1.5)
        await committer.stop()
        return committer

    committer = asyncio.run(run())
    shas = autosaves(vault)
    assert len(shas) == 1
    assert "Note.md" in files_in(vault, shas[0])
    assert committer.last_commit_at
    assert committer.last_error is None and committer.failures == 0


def test_touches_inside_the_debounce_coalesce(tmp_path):
    vault = make_vault(tmp_path)

    async def run():
        committer = AutoCommitter(vault, 0.1, max_wait=1.0)
        committer.start()
        for n in range(5):
            edit(vault, f"version {n}")
            committer.touch()
            await asyncio.sleep(0.02)
        await until(lambda: len(autosaves(vault)) == 1)
        await asyncio.sleep(0.2)
        await committer.stop()

    asyncio.run(run())
    shas = autosaves(vault)
    assert len(shas) == 1
    assert vault.commit_content_sync(shas[0], "Note.md") == "version 4"


def test_continuous_touches_still_commit_every_max_wait(tmp_path):
    """Steady typing, or a touch per streamed OMNIA delta, used to hold the
    debounce open for as long as it lasted."""
    vault = make_vault(tmp_path)
    vault.gc_auto_sync = lambda: None  # only the commits' own time counts

    async def run():
        # The debounce alone would never elapse while touches come every 10 ms.
        committer = AutoCommitter(vault, 0.2, max_wait=MAX_WAIT)
        committer.start()
        started = time.monotonic()
        n = 0
        while time.monotonic() - started < 0.8:
            edit(vault, f"keystroke {n}")
            committer.touch()
            n += 1
            await asyncio.sleep(0.01)
        during = len(autosaves(vault))
        await committer.stop()
        return during

    assert asyncio.run(run()) >= 2


def test_min_delay_waits_longer_than_the_debounce(tmp_path):
    vault = make_vault(tmp_path)

    async def run():
        committer = AutoCommitter(vault, DEBOUNCE, max_wait=1.0)
        committer.start()
        edit(vault, "ink")
        started = time.monotonic()
        committer.touch(min_delay=0.2)
        await asyncio.sleep(0.1)
        assert autosaves(vault) == []
        await until(lambda: len(autosaves(vault)) == 1)
        elapsed = time.monotonic() - started
        await committer.stop()
        return elapsed

    assert asyncio.run(run()) >= 0.2


def test_a_plain_touch_pulls_a_long_delay_back_in(tmp_path):
    vault = make_vault(tmp_path)

    async def run():
        committer = AutoCommitter(vault, DEBOUNCE, max_wait=5.0)
        committer.start()
        edit(vault, "ink", name="Ink.md")
        committer.touch(min_delay=5.0)
        edit(vault, "typed")
        committer.touch()
        started = time.monotonic()
        await until(lambda: len(autosaves(vault)) == 1, timeout=2.0)
        elapsed = time.monotonic() - started
        await committer.stop()
        return elapsed

    assert asyncio.run(run()) < 1.0


def test_max_wait_bounds_a_long_min_delay(tmp_path):
    vault = make_vault(tmp_path)

    async def run():
        committer = AutoCommitter(vault, DEBOUNCE, max_wait=MAX_WAIT)
        committer.start()
        edit(vault, "ink")
        committer.touch(min_delay=60.0)
        await until(lambda: len(autosaves(vault)) == 1, timeout=2.0)
        await committer.stop()

    asyncio.run(run())


def test_a_failed_commit_is_retried_and_reported(tmp_path):
    vault = make_vault(tmp_path)
    real_commit_all = vault.commit_all
    attempts: list[str] = []

    async def flaky(message, author=None):
        attempts.append(message)
        if len(attempts) == 1:
            raise RuntimeError("Unable to create '.git/index.lock': File exists")
        return await real_commit_all(message, author)

    vault.commit_all = flaky

    async def run():
        committer = AutoCommitter(vault, DEBOUNCE, max_wait=MAX_WAIT)
        committer.RETRY_BASE = 0.2
        committer.start()
        edit(vault, "unsaved")
        committer.touch()
        await until(lambda: committer.failures == 1)
        assert "index.lock" in committer.last_error
        assert committer.last_commit_at is None
        # No further touch: the retry must not wait for the next edit.
        await until(lambda: len(autosaves(vault)) == 1)
        await until(lambda: committer.failures == 0)
        await committer.stop()
        return committer

    committer = asyncio.run(run())
    assert attempts == ["autosave", "autosave"]
    assert committer.last_error is None
    assert committer.last_commit_at


def test_retry_backoff_doubles_then_holds_at_a_minute():
    committer = AutoCommitter(SimpleNamespace(), DEBOUNCE)
    delays = []
    for failures in (1, 2, 3, 4, 5, 6, 5000):
        committer.failures = failures
        delays.append(committer.retry_delay())
    assert delays == [5.0, 10.0, 20.0, 40.0, 60.0, 60.0, 60.0]


def test_stop_flushes_a_dirty_tree(tmp_path):
    vault = make_vault(tmp_path)

    async def run():
        committer = AutoCommitter(vault, 30.0)
        committer.start()
        edit(vault, "typed just before shutdown")
        committer.touch()
        await asyncio.sleep(0.05)
        await committer.stop()

    asyncio.run(run())
    shas = autosaves(vault)
    assert len(shas) == 1
    assert vault.commit_content_sync(shas[0], "Note.md") == "typed just before shutdown"


def test_stop_on_a_clean_tree_commits_nothing(tmp_path):
    vault = make_vault(tmp_path)
    head = vault.repo.head.commit.hexsha

    async def run():
        committer = AutoCommitter(vault, DEBOUNCE)
        committer.start()
        await asyncio.sleep(0.05)
        await committer.stop()

    asyncio.run(run())
    assert vault.repo.head.commit.hexsha == head
    assert autosaves(vault) == []


def test_temps_and_quarantines_are_never_committed(tmp_path):
    vault = make_vault(tmp_path)
    store = vault.dir / ".exscalia"
    store.mkdir(exist_ok=True)
    (store / "events.json").write_text("[]\n", encoding="utf-8")
    (store / "events.json.tmp").write_text("[half a wri", encoding="utf-8")
    (store / "events.json.corrupt-20260921T101500").write_text("{", encoding="utf-8")

    async def run():
        committer = AutoCommitter(vault, DEBOUNCE, max_wait=MAX_WAIT)
        committer.start()
        committer.touch()
        await until(lambda: len(autosaves(vault)) == 1)
        await committer.stop()

    asyncio.run(run())
    files = files_in(vault, autosaves(vault)[0])
    assert ".exscalia/events.json" in files
    assert not [f for f in files if ".tmp" in f or ".corrupt-" in f]
    assert not vault.has_changes()  # ignored, not merely left out


def test_ensure_adds_the_excludes_to_an_existing_vault_once(tmp_path):
    vault = make_vault(tmp_path)
    exclude = Path(vault.repo.git_dir) / "info" / "exclude"
    exclude.write_text("# a vault from before the excludes\n", encoding="utf-8")

    for _ in range(2):
        again = VaultRepo(vault.dir, "user", "user@test")
        again.ensure()

    lines = exclude.read_text(encoding="utf-8").splitlines()
    assert lines == ["# a vault from before the excludes", "*.tmp", "*.corrupt-*"]


def test_gc_runs_after_the_first_commit_then_once_per_interval(tmp_path):
    vault = make_vault(tmp_path)
    gcs: list[bool] = []
    vault.gc_auto_sync = lambda: gcs.append(True)

    async def commit_once(committer, text):
        before = len(autosaves(vault))
        edit(vault, text)
        committer.touch()
        await until(lambda: len(autosaves(vault)) == before + 1)
        await asyncio.sleep(0.05)  # the gc step follows the commit

    async def run():
        committer = AutoCommitter(vault, DEBOUNCE, max_wait=MAX_WAIT)
        committer.start()
        await commit_once(committer, "one")
        assert gcs == [True]
        await commit_once(committer, "two")
        assert gcs == [True]
        committer._last_gc -= committer.GC_INTERVAL
        await commit_once(committer, "three")
        assert gcs == [True, True]
        await committer.stop()

    asyncio.run(run())


def test_a_failing_gc_does_not_stop_autosave(tmp_path):
    vault = make_vault(tmp_path)

    def broken():
        raise RuntimeError("git gc exited 128")

    vault.gc_auto_sync = broken

    async def run():
        committer = AutoCommitter(vault, DEBOUNCE, max_wait=MAX_WAIT)
        committer.start()
        edit(vault, "one")
        committer.touch()
        await until(lambda: len(autosaves(vault)) == 1)
        await asyncio.sleep(0.05)
        edit(vault, "two")
        committer.touch()
        await until(lambda: len(autosaves(vault)) == 2)
        await committer.stop()
        return committer

    committer = asyncio.run(run())
    assert committer.failures == 0 and committer.last_error is None


def _count_objects(vault: VaultRepo) -> dict[str, int]:
    out = vault.repo.git.count_objects("-v")
    pairs = (line.split(": ", 1) for line in out.splitlines())
    return {key: int(value) for key, value in pairs if value.isdigit()}


def test_gc_auto_packs_loose_objects_before_it_returns(tmp_path, monkeypatch):
    """Foreground: with git's default autoDetach the pack would appear later,
    outside the git lock, from a forked orphan."""
    vault = make_vault(tmp_path)
    # `gc --auto` estimates the loose count from objects/17 alone; with
    # gc.auto=1 two blobs there are over the threshold.
    bodies: list[bytes] = []
    n = 0
    while len(bodies) < 2:
        body = f"sample {n}\n".encode()
        if hashlib.sha1(b"blob %d\0" % len(body) + body).hexdigest().startswith("17"):
            bodies.append(body)
        n += 1
    for i, body in enumerate(bodies):
        (vault.dir / f"sample-{i}.md").write_bytes(body)
    vault.commit_all_sync("samples", vault.user_author)
    vault.repo.git.config("gc.auto", "1")

    before = _count_objects(vault)
    assert before["count"] > 0 and before["in-pack"] == 0
    trace = tmp_path / "git-trace"
    monkeypatch.setenv("GIT_TRACE", str(trace))
    vault.gc_auto_sync()
    monkeypatch.delenv("GIT_TRACE")
    after = _count_objects(vault)
    assert after["in-pack"] > 0
    assert after["count"] < before["count"]
    # It runs beside commits, so it leaves the ref locks alone.
    steps = trace.read_text()
    assert "git repack" in steps
    assert "pack-refs" not in steps and "reflog expire" not in steps


def test_gc_runs_outside_the_git_lock(tmp_path):
    """A repack can take minutes; Paper saves, OMNIA commits, Undo and History
    wait on that lock."""
    vault = make_vault(tmp_path)
    held: list[bool] = []
    vault.gc_auto_sync = lambda: held.append(vault._git_lock.locked())

    async def run():
        committer = AutoCommitter(vault, DEBOUNCE, max_wait=MAX_WAIT)
        committer.start()
        edit(vault, "one")
        committer.touch()
        await until(lambda: held)
        await committer.stop()

    asyncio.run(run())
    assert held == [False]


def test_backup_status_reports_the_autocommitter(tmp_path):
    committer = AutoCommitter(make_vault(tmp_path), DEBOUNCE)
    committer.failures = 2
    committer.last_error = "RuntimeError: No space left on device"
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(autocommit=committer, backup=None))
    )
    status = asyncio.run(meta_api.backup_status(request))
    assert status == {
        "configured": False,
        "autocommit": {
            "last_commit_at": None,
            "last_error": "RuntimeError: No space left on device",
            "failures": 2,
        },
    }


def test_paper_saves_ask_for_the_long_commit_delay(state):
    """Each pause in handwriting used to commit a fresh drawing and render."""
    delays: list[float | None] = []
    state.autocommit = SimpleNamespace(touch=lambda min_delay=None: delays.append(min_delay))
    client = make_client(state)
    nb = new_notebook(client)
    saved = put_page(client, nb["path"], "a" * 32, 0, drawing=b"ink")
    assert saved.status_code == 200, saved.text
    assert len(delays) == 2
    assert set(delays) == {paper_api.PAPER_COMMIT_DELAY}
    assert paper_api.PAPER_COMMIT_DELAY > AutoCommitter(SimpleNamespace(), 10.0).max_wait
