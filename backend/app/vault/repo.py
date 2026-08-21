"""Git-backed vault. The git repo IS the source of truth for note history.

User edits are autosaved to the working tree and committed quietly on a
debounced cadence. AI (overseer) changes never touch the working tree until
approved — see app.pending — and are committed with a distinct author.
"""
import asyncio
import logging
import time
from pathlib import Path

from git import Actor, Repo
from git.exc import GitCommandError

from . import fm

log = logging.getLogger(__name__)

OVERSEER_AUTHOR = Actor("OMNIA", "omnia@exscalia.local")

WELCOME_BODY = """\
Welcome to **EXSCALIA** — your git-backed markdown notebook. #welcome

## How it works

- Every note is a plain `.md` file with YAML frontmatter, stored in a git repository.
- Your edits autosave and commit quietly in the background.
- Link notes with wikilinks: [[Daily Notes]] — backlinks are tracked automatically.
- Tag notes by typing #hashtags anywhere in the text, like the #welcome tag above.

## Tasks

Tasks are just markdown lines with a date — they show up on the calendar:

- [ ] Explore EXSCALIA 📅 {today}

## OMNIA, your AI overseer

Add an AI provider key in Settings to enable OMNIA. It can search, organize,
and operate your vault. Explicit requests run as individual Git commits with
Undo; suggestions stay in the review tray, and destructive changes ask for a
separate confirmation.
"""

NOTE_TEMPLATE = """\
---
title: "{{title}}"
created: "{{now}}"
modified: "{{now}}"
---

"""

DAILY_TEMPLATE = """\
---
title: "{{date}}"
created: "{{now}}"
modified: "{{now}}"
---

#daily

## Notes

## Tasks

- [ ]
"""


class VaultRepo:
    def __init__(self, vault_dir: Path, author_name: str, author_email: str):
        self.dir = vault_dir
        self.user_author = Actor(author_name, author_email)
        self.repo: Repo | None = None
        self._git_lock = asyncio.Lock()

    def ensure(self) -> None:
        """Init the vault repo and seed content on first boot."""
        if (self.dir / ".git").exists():
            self.repo = Repo(self.dir)
            self._ensure_gitattributes()
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        self.repo = Repo.init(self.dir, initial_branch="main")
        self._ensure_gitattributes()
        self._seed()

    def _ensure_gitattributes(self) -> None:
        """Attachments are binary; without saying so, git diffs 25 MB images
        on every autosave commit. Idempotent, so existing vaults pick it up
        on their next boot (and next autocommit sweeps it in)."""
        path = self.dir / ".gitattributes"
        if not path.exists():
            path.write_text("attachments/** binary\n", encoding="utf-8")

    def _seed(self) -> None:
        from datetime import date

        today = date.today().isoformat()
        welcome = fm.build_new_note("Welcome", WELCOME_BODY.format(today=today))
        (self.dir / "Welcome.md").write_text(welcome, encoding="utf-8")
        tpl = self.dir / "templates"
        tpl.mkdir(exist_ok=True)
        (tpl / "note.md").write_text(NOTE_TEMPLATE, encoding="utf-8")
        (tpl / "daily.md").write_text(DAILY_TEMPLATE, encoding="utf-8")
        self.commit_all_sync("Initialize vault", self.user_author)
        log.info("Seeded new vault at %s", self.dir)

    # -- sync git ops (call via asyncio.to_thread from async code) ----------

    def has_changes(self) -> bool:
        assert self.repo is not None
        return self.repo.is_dirty(untracked_files=True)

    def commit_all_sync(self, message: str, author: Actor) -> str | None:
        """Stage everything and commit. Returns the commit sha, or None if clean."""
        assert self.repo is not None
        self.repo.git.add(A=True)
        if not self.repo.is_dirty(index=True, working_tree=False, untracked_files=False) and (
            self.repo.head.is_valid()
        ):
            return None
        commit = self.repo.index.commit(message, author=author, committer=author)
        return commit.hexsha

    async def commit_all(self, message: str, author: Actor | None = None) -> str | None:
        async with self._git_lock:
            return await asyncio.to_thread(
                self.commit_all_sync, message, author or self.user_author
            )

    def commit_paths_sync(
        self, message: str, paths: list[str], author: Actor
    ) -> str | None:
        """Commit only ``paths`` while leaving unrelated user edits untouched."""
        assert self.repo is not None
        normalized = sorted({str(Path(path)) for path in paths if path})
        if not normalized:
            return None
        self.repo.git.add("-A", "--", *normalized)
        if not self.repo.is_dirty(
            index=True, working_tree=False, untracked_files=False
        ):
            return None
        return self.repo.index.commit(
            message, author=author, committer=author
        ).hexsha

    async def commit_paths(
        self, message: str, paths: list[str], author: Actor | None = None
    ) -> str | None:
        async with self._git_lock:
            return await asyncio.to_thread(
                self.commit_paths_sync,
                message,
                paths,
                author or self.user_author,
            )

    def revert_commit_sync(
        self, commit_sha: str, message: str, author: Actor
    ) -> str:
        """Revert one commit without forcing through conflicts."""
        assert self.repo is not None
        self.repo.commit(commit_sha)
        try:
            self.repo.git.merge_base("--is-ancestor", commit_sha, "HEAD")
        except GitCommandError:
            raise ValueError("That action is no longer in this vault history")
        try:
            self.repo.git.revert("--no-commit", commit_sha)
            if not self.repo.is_dirty(
                index=True, working_tree=False, untracked_files=False
            ):
                self.repo.git.revert("--abort")
                raise ValueError("That action has already been undone")
            return self.repo.index.commit(
                message, author=author, committer=author
            ).hexsha
        except GitCommandError as exc:
            try:
                self.repo.git.revert("--abort")
            except GitCommandError:
                self.repo.git.reset()
            raise ValueError(
                "Undo conflicts with newer vault changes; use History or Trash instead"
            ) from exc

    async def revert_commit(
        self, commit_sha: str, message: str, author: Actor
    ) -> str:
        async with self._git_lock:
            return await asyncio.to_thread(
                self.revert_commit_sync, commit_sha, message, author
            )

    def head_content_sync(self, rel_path: str) -> str | None:
        """Content of a file at HEAD, or None if it doesn't exist there."""
        assert self.repo is not None
        if not self.repo.head.is_valid():
            return None
        try:
            blob = self.repo.head.commit.tree / rel_path
            return blob.data_stream.read().decode("utf-8")
        except KeyError:
            return None

    def commit_content_sync(self, commit_sha: str, rel_path: str) -> str | None:
        """Return a path's UTF-8 content at a specific commit."""
        assert self.repo is not None
        try:
            commit = self.repo.commit(commit_sha)
            blob = commit.tree / rel_path
            return blob.data_stream.read().decode("utf-8")
        except (KeyError, ValueError):
            return None

    def history_sync(self, rel_path: str, limit: int = 100) -> list[dict]:
        assert self.repo is not None
        if not self.repo.head.is_valid():
            return []
        history = []
        version_path = rel_path
        # ``git rev-list`` (used by GitPython's iter_commits) does not accept
        # --follow; invoke log for its rename-aware path walk.
        output = self.repo.git.log(
            "--follow",
            f"--max-count={limit}",
            "--format=%H",
            "--",
            rel_path,
        )
        commits = [self.repo.commit(sha) for sha in output.splitlines() if sha]
        for commit in commits:
            content = self.commit_content_sync(commit.hexsha, version_path)
            history.append(
                {
                    "sha": commit.hexsha,
                    "short_sha": commit.hexsha[:8],
                    "message": commit.message.strip().splitlines()[0],
                    "author": commit.author.name,
                    "committed_at": commit.committed_datetime.isoformat(),
                    "size": len(content.encode("utf-8")) if content is not None else 0,
                    "preview": (
                        " ".join(content.splitlines())[:180] if content is not None else ""
                    ),
                    "deleted": content is None,
                    "version_path": version_path,
                }
            )
            # Walking newest → oldest: after recording the rename commit under
            # its new name, switch to the old path for earlier versions.
            if commit.parents:
                for diff in commit.parents[0].diff(commit, M=True):
                    if (
                        diff.change_type == "R"
                        and diff.b_path == version_path
                        and diff.a_path
                    ):
                        version_path = diff.a_path
                        break
        return history


class BackupPusher:
    """Periodic push of the vault to an off-site git remote (cron-like).

    Pushes only when HEAD moved since the last successful push. Status is
    kept in memory for the Settings page. Credentials stay in the remote URL
    (from the environment) and are never reported back out.
    """

    REMOTE_NAME = "backup"

    def __init__(self, vault: VaultRepo, url: str, interval: float):
        self.vault = vault
        self.url = url
        self.interval = max(60.0, interval)
        self.last_push: str | None = None
        self.last_error: str | None = None
        self.last_pushed_sha: str | None = None
        self._task: asyncio.Task | None = None

    def configure_remote_sync(self) -> None:
        repo = self.vault.repo
        assert repo is not None
        if self.REMOTE_NAME in [r.name for r in repo.remotes]:
            repo.remote(self.REMOTE_NAME).set_url(self.url)
        else:
            repo.create_remote(self.REMOTE_NAME, self.url)

    def _push_sync(self) -> str | None:
        repo = self.vault.repo
        assert repo is not None
        if not repo.head.is_valid():
            return None  # empty repo, nothing to push yet
        sha = repo.head.commit.hexsha
        if sha == self.last_pushed_sha:
            return None
        branch = repo.active_branch.name
        repo.git.push(self.REMOTE_NAME, f"{branch}:{branch}")
        self.last_pushed_sha = sha
        return sha

    async def push(self) -> dict:
        from datetime import datetime, timezone

        try:
            async with self.vault._git_lock:
                sha = await asyncio.to_thread(self._push_sync)
            self.last_error = None
            if sha:
                self.last_push = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                log.info("backup push %s", sha[:8])
                return {"pushed": True, "commit": sha}
            return {"pushed": False, "reason": "nothing new to push"}
        except Exception as e:
            # Never leak the remote URL (it may embed a token).
            self.last_error = str(e).replace(self.url, "<remote>")[:400]
            log.warning("backup push failed: %s", self.last_error)
            return {"pushed": False, "error": self.last_error}

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            await self.push()


class AutoCommitter:
    """Debounced background auto-commit of the user's own edits."""

    def __init__(self, vault: VaultRepo, debounce: float):
        self.vault = vault
        self.debounce = debounce
        self._last_touch: float | None = None
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None

    def touch(self) -> None:
        self._last_touch = time.monotonic()
        self._wake.set()

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        # Flush any straggling edits on shutdown.
        if self.vault.repo and self.vault.has_changes():
            await self.vault.commit_all("autosave")

    async def _run(self) -> None:
        while True:
            await self._wake.wait()
            # Wait until the vault has been quiet for `debounce` seconds.
            while True:
                assert self._last_touch is not None
                remaining = self.debounce - (time.monotonic() - self._last_touch)
                if remaining <= 0:
                    break
                await asyncio.sleep(remaining)
            self._wake.clear()
            try:
                sha = await self.vault.commit_all("autosave")
                if sha:
                    log.info("autosave commit %s", sha[:8])
            except Exception:
                log.exception("autosave commit failed")
