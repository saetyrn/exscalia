"""Git-backed vault. The git repo IS the source of truth for note history.

User edits are autosaved to the working tree and committed quietly on a
debounced cadence. AI (overseer) changes never touch the working tree until
approved — see app.pending — and are committed with a distinct author.
"""
import asyncio
import logging
import os
import re
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from git import Actor, Repo
from git.exc import GitCommandError
from gitdb.exc import ODBError

from . import fm
from .internal import atomic_json, read_json
from .notes import NoteError

log = logging.getLogger(__name__)

OVERSEER_AUTHOR = Actor("OMNIA", "omnia@exscalia.local")

# The userinfo part of a remote URL, where a push token lives.
_USERINFO = re.compile(r"//[^@/\s]+@")

# Binary paths, per extension so the JSON files inside a .paper package still
# diff as text. Order is stable so the file only ever grows by what is new.
GITATTRIBUTES_LINES = [
    "attachments/** binary",
    "paper/**/*.pkdrawing binary",
    "paper/**/*.png binary",
    "paper/**/*.jpg binary",
    "paper/**/*.jpeg binary",
    "paper/**/*.webp binary",
    "paper/**/*.pdf binary",
]

# Never vault content: atomic_json's in-flight temp and read_json's quarantine
# of a corrupt file (the good version is already in history).
GIT_EXCLUDE_LINES = [
    "*.tmp",
    "*.corrupt-*",
]

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


def _append_missing_lines(path: Path, wanted: list[str]) -> None:
    existing = (
        path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    )
    present = {line.strip() for line in existing}
    missing = [line for line in wanted if line not in present]
    if not missing:
        return
    lines = [line for line in existing]
    if lines and lines[-1].strip() == "":
        lines = lines[:-1]
    lines.extend(missing)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _tree_blob(commit, rel_path: str) -> str | None:
    """Blob id of a path inside a commit's tree, or None if it is not there."""
    try:
        item = commit.tree / rel_path
    except KeyError:
        return None
    return item.hexsha if item.type == "blob" else None


class VaultRepo:
    # Bounds a pathological repack; below git's thresholds `gc --auto` is a
    # quick no-op.
    GC_TIMEOUT = 300.0

    def __init__(self, vault_dir: Path, author_name: str, author_email: str):
        self.dir = vault_dir
        self.user_author = Actor(author_name, author_email)
        self.repo: Repo | None = None
        self._git_lock = asyncio.Lock()

    def ensure(self) -> None:
        """Init the vault repo and seed content on first boot."""
        if (self.dir / ".git").exists():
            self._clear_stale_locks()
            self.repo = Repo(self.dir)
            self._ensure_gitattributes()
            self._ensure_git_exclude()
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        self.repo = Repo.init(self.dir, initial_branch="main")
        self._ensure_gitattributes()
        self._ensure_git_exclude()
        self._seed()

    def _clear_stale_locks(self) -> None:
        """Remove the lock files a killed git leaves behind.

        A SIGKILL mid-commit (a deploy recreate, an OOM kill, a host reboot)
        leaves ``.git/index.lock`` in the volume, and every later ``git add``
        exits 128 on it: the boot-time commit crashed the app, and restart
        looped it behind Caddy until someone deleted the file by hand. This
        process is the sole owner of the vault repo and ensure() only runs at
        boot, so a fresh container cannot have a live git child holding one.
        """
        git_dir = self.dir / ".git"
        if not git_dir.is_dir():
            return
        candidates = [
            git_dir / name
            for name in ("index.lock", "HEAD.lock", "packed-refs.lock", "config.lock")
        ]
        refs = git_dir / "refs"
        if refs.is_dir():
            candidates.extend(sorted(refs.rglob("*.lock")))
        for lock in candidates:
            if not lock.is_file():
                continue
            name = lock.relative_to(self.dir)
            try:
                lock.unlink()
            except OSError:
                # Boot goes on regardless; the commits that need it log.
                log.exception("could not remove stale git lock %s", name)
            else:
                log.warning("removed stale git lock %s", name)

    def _ensure_gitattributes(self) -> None:
        """Attachments and Paper blobs are binary; without saying so, git diffs
        25 MB images on every autosave commit.

        Append-if-missing rather than write-if-absent: a vault created before
        Paper existed already has the file, and would otherwise never learn
        about ``.pkdrawing``. Per-extension so ``notebook.json``/``page.json``
        keep their readable textual diffs.
        """
        _append_missing_lines(self.dir / ".gitattributes", GITATTRIBUTES_LINES)

    def _ensure_git_exclude(self) -> None:
        """Keep temps and quarantines out of ``git add -A``.

        A ``*.tmp`` that atomic_json renames away between git's scan and its
        open fails the whole autosave commit. ``info/exclude`` rather than a
        ``.gitignore``: it is local to this repo and never committed, and
        ensure() rewrites it on every boot, so a vault cloned back from the
        backup remote gets it too.
        """
        assert self.repo is not None
        path = Path(self.repo.git_dir) / "info" / "exclude"
        path.parent.mkdir(parents=True, exist_ok=True)
        _append_missing_lines(path, GIT_EXCLUDE_LINES)

    def _seed(self) -> None:
        # Local imports: config and the task parser sit above this module.
        from ..config import get_settings
        from ..index.parse import normalize_task_metadata
        from ..tz import local_today

        # The user's day, not the machine's: in the evening the two differ, and
        # the seeded task then read as due yesterday to every UTC-keyed query.
        today = local_today(get_settings())
        welcome = fm.build_new_note("Welcome", WELCOME_BODY.format(today=today))
        # Mint the task's metadata now. A bare task line is refused by every
        # bulk task operation until the note is next saved from the editor.
        welcome, _ = normalize_task_metadata(welcome)
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

    def gc_auto_sync(self) -> None:
        """Pack loose objects once git's own thresholds say so.

        Nothing else here ever does: ``git add`` is not one of the porcelain
        commands that run ``gc --auto``, and GitPython's ``index.commit``
        writes its objects itself, so every autosave left loose objects for
        good. Foreground (``gc.autoDetach=false``) so the work stays inside
        this timeout rather than in a forked orphan; a process group rather
        than ``kill_after_timeout`` for the reason in
        ``BackupPusher._push_sync``.

        It runs outside the vault's git lock, beside commits, as git runs its
        own: a repack can take minutes, and that lock is also Paper's write
        lock and OMNIA's commit path. Repacking and pruning are safe beside a
        commit. Packing refs and expiring reflogs are not, here: they take
        the branch's ref lock, and GitPython fails a commit that finds it
        held instead of waiting as git does. Both stay off, as they were
        before any gc ran.
        """
        proc = subprocess.Popen(
            [
                "git",
                "-c",
                "gc.autoDetach=false",
                "-c",
                "gc.packRefs=false",
                # git skips the reflog step only when both are never.
                "-c",
                "gc.reflogExpire=never",
                "-c",
                "gc.reflogExpireUnreachable=never",
                "gc",
                "--auto",
                "--quiet",
            ],
            cwd=self.dir,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            _, err = proc.communicate(timeout=self.GC_TIMEOUT)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            proc.communicate()
            raise RuntimeError(
                f"git gc timed out after {int(self.GC_TIMEOUT)}s"
            ) from None
        if proc.returncode != 0:
            detail = err.decode("utf-8", "replace").strip()
            raise RuntimeError(f"git gc exited {proc.returncode}: {detail}")

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

    def changed_paths_sync(self, sha: str) -> list[tuple[str, str, str | None]]:
        """What one commit did, path by path: ``(status, path, new_path)``.

        Statuses are git's ``--name-status`` letters. Renames and copies
        (``R``/``C``, git's similarity score appended) carry the old path and
        then the new one; every other status has ``new_path`` None.
        """
        assert self.repo is not None
        output = self.repo.git.diff_tree(
            "--root", "-r", "-M", "--no-commit-id", "--name-status", "-z", sha
        )
        fields = iter(output.split("\0"))
        changes: list[tuple[str, str, str | None]] = []
        for status in fields:
            if not status:
                continue
            path = next(fields)
            new_path = next(fields) if status[0] in "RC" else None
            changes.append((status, path, new_path))
        return self._reconcile_note_renames(sha, changes)

    def _reconcile_note_renames(
        self, sha: str, changes: list[tuple[str, str, str | None]]
    ) -> list[tuple[str, str, str | None]]:
        """Git's rename detection is a similarity score, and on a short note it
        misjudges both ways: moving one rewrites about half of it (the title
        follows the filename, ``modified`` is restamped), so the move reads as
        a delete plus an add; and two unrelated notes stamped in the same
        second are mostly identical frontmatter, so they can read as a rename.

        Notes carry an identity git doesn't know about: ``created`` is written
        once and never changes. An inexact rename between notes whose stamps
        differ is split back apart, and a delete and an add that are the same
        note under their title and ``modified`` are paired into a move.
        """
        assert self.repo is not None
        parents = self.repo.commit(sha).parents
        if not parents:
            return changes
        parent = parents[0].hexsha

        def identity(rev: str, path: str):
            if not path.endswith(".md"):
                return None
            content = self.commit_content_sync(rev, path)
            if content is None:
                return None
            try:
                meta, body = fm.parse(content)
            except Exception:  # malformed YAML: no identity to go on
                return None
            rest = {k: v for k, v in meta.items() if k not in ("title", "modified")}
            return (rest, body) if rest.get("created") else None

        checked: list[tuple[str, str, str | None]] = []
        for status, path, new_path in changes:
            if status[:1] == "R" and status != "R100" and new_path:
                before, after = identity(parent, path), identity(sha, new_path)
                if before and after and before[0]["created"] != after[0]["created"]:
                    checked += [("D", path, None), ("A", new_path, None)]
                    continue
            checked.append((status, path, new_path))

        old = {p: identity(parent, p) for s, p, _ in checked if s == "D"}
        # Nothing deleted, nothing to pair: a bulk create reads no notes back.
        if not any(old.values()):
            return checked
        new = {p: identity(sha, p) for s, p, _ in checked if s == "A"}
        pairs: dict[str, str] = {}
        for old_path, key in old.items():
            if key is None:
                continue
            twins = [p for p, other in new.items() if other == key]
            # Only an unambiguous match is a move; two candidates on either
            # side stay a delete and an add.
            if len(twins) == 1 and sum(k == key for k in old.values()) == 1:
                pairs[old_path] = twins[0]
        paired_new = set(pairs.values())
        return [
            (status, path, new_path)
            for status, path, new_path in checked
            if not (status == "D" and path in pairs)
            and not (status == "A" and path in paired_new)
        ] + [("R", old_path, new_path) for old_path, new_path in pairs.items()]

    async def changed_paths(
        self, sha: str | None
    ) -> list[tuple[str, str, str | None]]:
        if not sha:
            return []
        async with self._git_lock:
            return await asyncio.to_thread(self.changed_paths_sync, sha)

    def revert_commit_sync(
        self,
        commit_sha: str,
        message: str,
        author: Actor,
        paths: list[str] | None = None,
    ) -> tuple[str, list[str]]:
        """Revert one commit without forcing through conflicts.

        Returns ``(sha, skipped)``. When the whole revert conflicts and
        ``paths`` are given, each file the commit changed under them is undone
        on its own if it still sits exactly as the commit left it; files with
        newer edits are kept and come back in ``skipped``. One newer edit to
        one note used to fail the undo of a whole multi-note action.
        """
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
            return (
                self.repo.index.commit(
                    message, author=author, committer=author
                ).hexsha,
                [],
            )
        except GitCommandError as exc:
            try:
                self.repo.git.revert("--abort")
            except GitCommandError:
                self.repo.git.reset()
            conflict = exc
        restored, skipped = (
            self._restore_unchanged_sync(commit_sha, paths) if paths else ([], [])
        )
        if not restored:
            raise ValueError(
                "Undo conflicts with newer vault changes; use History or Trash instead"
            ) from conflict
        if not self.repo.is_dirty(
            index=True, working_tree=False, untracked_files=False
        ):
            return self.repo.head.commit.hexsha, skipped
        return (
            self.repo.index.commit(
                f"{message} (partial)", author=author, committer=author
            ).hexsha,
            skipped,
        )

    def _restore_unchanged_sync(
        self, commit_sha: str, paths: list[str]
    ) -> tuple[list[str], list[str]]:
        """Put back the pre-commit version of every file ``commit_sha`` changed
        under ``paths`` whose working copy is still byte-for-byte what the
        commit wrote. Everything else is left alone and reported as skipped.

        The working tree, not HEAD, is the comparison: an autosaved but not yet
        committed edit is still the user's newer text. A rename is one unit —
        restoring its source while keeping an edited destination would leave
        the note twice. Staged, not committed.
        """
        assert self.repo is not None
        commit = self.repo.commit(commit_sha)
        if len(commit.parents) != 1:
            return [], []
        parent = commit.parents[0]
        scope = {str(Path(path)) for path in paths if path}
        fields = self.repo.git.diff_tree(
            "-r", "-M", "--name-status", "-z", parent.hexsha, commit.hexsha
        ).split("\0")
        groups: list[list[str]] = []
        i = 0
        while i < len(fields) and fields[i]:
            width = 2 if fields[i][0] == "R" else 1
            groups.append(fields[i + 1 : i + 1 + width])
            i += 1 + width
        restored: list[str] = []
        skipped: list[str] = []
        for group in groups:
            # A recorded path can be a folder (move_folder, delete_folder).
            if not any(
                rel == root or rel.startswith(root + "/")
                for rel in group
                for root in scope
            ):
                continue
            newer = [
                rel for rel in group
                if self._working_blob(rel) != _tree_blob(commit, rel)
            ]
            if newer:
                skipped.extend(newer)
                continue
            for rel in group:
                if _tree_blob(parent, rel) is not None:
                    self.repo.git.checkout(parent.hexsha, "--", f":(literal){rel}")
                else:
                    self.repo.git.rm(
                        "--cached", "--quiet", "--ignore-unmatch", "--",
                        f":(literal){rel}",
                    )
                    (self.dir / rel).unlink(missing_ok=True)
                restored.append(rel)
        return restored, skipped

    def _working_blob(self, rel_path: str) -> str | None:
        """Blob id git would store for a working-tree file; None when absent.
        A folder where a file was is a sentinel that never matches a blob."""
        assert self.repo is not None
        path = self.dir / rel_path
        if path.is_dir():
            return "<dir>"
        if not path.is_file():
            return None
        return self.repo.git.hash_object("--", rel_path)

    async def revert_commit(
        self,
        commit_sha: str,
        message: str,
        author: Actor,
        paths: list[str] | None = None,
    ) -> tuple[str, list[str]]:
        async with self._git_lock:
            return await asyncio.to_thread(
                self.revert_commit_sync, commit_sha, message, author, paths
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
        """Return a path's UTF-8 content at a specific commit. An unknown rev
        is None like a missing path: gitdb reports it as BadName (ODBError)."""
        assert self.repo is not None
        try:
            commit = self.repo.commit(commit_sha)
            blob = commit.tree / rel_path
            return blob.data_stream.read().decode("utf-8")
        except (KeyError, ValueError, ODBError):
            return None

    def commit_bytes_sync(self, commit_sha: str, rel_path: str) -> bytes | None:
        """Raw bytes of a path at a commit. The text variant decodes as UTF-8,
        which is wrong for ink and renders."""
        assert self.repo is not None
        try:
            blob = self.repo.commit(commit_sha).tree / rel_path
            return blob.data_stream.read()
        except (KeyError, ValueError, ODBError):
            return None

    def dir_history_sync(self, rel_dir: str, limit: int = 100) -> list[dict]:
        """Commits that touched anything under a directory.

        ``history_sync`` uses ``--follow``, which git only accepts for a single
        file, so a Paper page (a directory of blobs) needs its own walk. No
        rename tracking: a page directory is renamed only by a notebook move,
        and the feed reports that as a delete plus an add.
        """
        assert self.repo is not None
        if not self.repo.head.is_valid():
            return []
        output = self.repo.git.log(
            f"--max-count={limit}",
            "--format=%H",
            "--",
            rel_dir,
        )
        versions = []
        for sha in output.splitlines():
            if not sha:
                continue
            commit = self.repo.commit(sha)
            versions.append(
                {
                    "sha": commit.hexsha,
                    "short_sha": commit.hexsha[:8],
                    "message": commit.message.strip().splitlines()[0],
                    "author": commit.author.name,
                    "committed_at": commit.committed_datetime.isoformat(),
                }
            )
        return versions

    def history_sync(self, rel_path: str, limit: int = 100, skip: int = 0) -> list[dict]:
        """A note's versions, newest first, from one rename-following walk.

        ``git rev-list`` (used by GitPython's iter_commits) does not accept
        --follow; invoke log for its rename-aware path walk. Its name-status
        line per commit is all the listing needs, so there is no diff or blob
        read per version: content is fetched on selection (/history/content),
        and the sizes the list shows come from one ``cat-file --batch-check``
        over the page. ``-z`` keeps paths verbatim instead of quoting spaces
        and non-ASCII. Paging slices here rather than passing --skip: under
        --follow, git's --skip counts every commit in the repo, not just this
        note's, and the newer versions of an earlier page are still walked for
        their renames, so the page names its versions by the path they had.
        """
        assert self.repo is not None
        if not self.repo.head.is_valid():
            return []
        output = self.repo.git.log(
            "--follow",
            "-M",
            "--name-status",
            "-z",
            f"--max-count={skip + limit}",
            "--format=%x1e%H%x1f%an%x1f%ci%x1f%B",
            "--",
            rel_path,
        )
        history: list[dict] = []
        version_path = rel_path
        # NUL-separated: a header token (0x1e + fields) per commit, then its
        # status token ("\nM", "\nR100", ...) and that status's path(s).
        tokens = iter(output.split("\0"))
        for token in tokens:
            if token.startswith("\x1e"):
                sha, author, date, body = token[1:].split("\x1f", 3)
                lines = body.strip().splitlines()
                history.append(
                    {
                        "sha": sha,
                        "short_sha": sha[:8],
                        "message": lines[0] if lines else "",
                        "author": author,
                        # %ci, not %cI: git writes UTC as "Z", and this
                        # keeps the "+00:00" the listing always carried.
                        "committed_at": datetime.strptime(
                            date, "%Y-%m-%d %H:%M:%S %z"
                        ).isoformat(),
                        "deleted": False,
                        "version_path": version_path,
                    }
                )
                continue
            status = token.strip()
            if not status or not history:
                continue
            entry = history[-1]
            if status[0] in "RC":
                old, new = next(tokens, ""), next(tokens, "")
                # Walking newest → oldest: the rename commit is listed under
                # its new name; earlier versions live at the old path.
                if new == entry["version_path"] and old:
                    version_path = old
            else:
                path = next(tokens, "")
                if status[0] == "D" and path == entry["version_path"]:
                    entry["deleted"] = True
        page = history[skip:]
        self._attach_sizes(page)
        return page

    def _attach_sizes(self, versions: list[dict]) -> None:
        """Each version's byte size from one ``cat-file --batch-check`` (object
        headers only, no blob read), so the history list can show what a save
        added or removed. A version whose path the commit lacks reads 0."""
        for version in versions:
            version["size"] = 0
        present = [v for v in versions if not v["deleted"]]
        if not present:
            return
        names = "".join(f"{v['sha']}:{v['version_path']}\n" for v in present)
        try:
            proc = subprocess.run(
                ["git", "cat-file", "--batch-check=%(objectsize)"],
                cwd=self.dir,
                input=names.encode("utf-8"),
                capture_output=True,
                check=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            log.warning("could not size the versions of a note", exc_info=True)
            return
        for version, line in zip(present, proc.stdout.decode("utf-8", "replace").splitlines()):
            line = line.strip()
            if line.isdigit():  # "<name> missing" for a path the commit lacks
                version["size"] = int(line)


class BackupPusher:
    """Periodic push of the vault to an off-site git remote (cron-like).

    Pushes only when HEAD moved since the last successful push. Status is
    kept for the Settings page and, given a ``state_dir``, persisted there so
    a restart neither forgets the last success nor re-pushes an unchanged
    HEAD. Credentials stay in the remote URL (from the environment) and are
    never reported back out.
    """

    REMOTE_NAME = "backup"
    # Bounds a hung transport. The push no longer holds the vault lock, so
    # this only has to beat the interval, and stays generous enough for a
    # large catch-up push (attachments, Paper blobs) after an outage.
    PUSH_TIMEOUT = 300.0
    # Consecutive failures before the user is told, then at most once a day.
    ALERT_AFTER = 3
    STATUS_FILE = "backup-status.json"

    def __init__(
        self,
        vault: VaultRepo,
        url: str,
        interval: float,
        state_dir: Path | None = None,
    ):
        self.vault = vault
        self.url = url
        self.interval = max(60.0, interval)
        self.last_push: str | None = None
        self.last_success: str | None = None
        self.last_error: str | None = None
        self.last_pushed_sha: str | None = None
        self.failures = 0
        self.failing_since: str | None = None
        self.alerted_on: str | None = None
        # async (alert) -> None; main wires it to the alert queue + Web Push.
        self.on_persistent_failure: Callable[[dict], Awaitable[None]] | None = None
        self._status_path = state_dir / self.STATUS_FILE if state_dir else None
        self._push_lock = asyncio.Lock()
        self._proc: subprocess.Popen | None = None
        self._task: asyncio.Task | None = None
        # The clock for staleness starts no earlier than boot: after downtime
        # the first push is still an interval away, and that is not a failure.
        self._booted = datetime.now(timezone.utc)
        self._load()

    def _redact(self, text: str) -> str:
        # Never leak the remote URL (it may embed a token), nor any other
        # userinfo git may echo back.
        return _USERINFO.sub("//", text.replace(self.url, "<remote>"))

    def _load(self) -> None:
        if self._status_path is None:
            return
        try:
            saved = read_json(self._status_path, {})
        except NoteError:
            log.warning("backup status unreadable; starting fresh", exc_info=True)
            return
        if not isinstance(saved, dict):
            return

        def text(key: str) -> str | None:
            value = saved.get(key)
            return value if isinstance(value, str) else None

        self.last_push = text("last_push")
        self.last_success = text("last_success")
        self.last_error = text("last_error")
        failures = saved.get("failures")
        self.failures = failures if isinstance(failures, int) else 0
        self.failing_since = text("failing_since")
        self.alerted_on = text("alerted_on")
        # A different remote has none of this history yet.
        if saved.get("remote") == _USERINFO.sub("//", self.url):
            self.last_pushed_sha = text("last_pushed_sha")

    def _save(self) -> None:
        if self._status_path is None:
            return
        try:
            atomic_json(
                self._status_path,
                {
                    "remote": _USERINFO.sub("//", self.url),
                    "last_push": self.last_push,
                    "last_success": self.last_success,
                    "last_error": self.last_error,
                    "last_pushed_sha": self.last_pushed_sha,
                    "failures": self.failures,
                    "failing_since": self.failing_since,
                    "alerted_on": self.alerted_on,
                },
            )
        except OSError:
            log.warning("could not persist backup status", exc_info=True)

    @property
    def stale(self) -> bool:
        """Failing repeatedly, or not confirmed current for three intervals."""
        if self.failures >= self.ALERT_AFTER:
            return True
        last = self._booted
        if self.last_success:
            try:
                success = datetime.fromisoformat(self.last_success.replace("Z", "+00:00"))
            except ValueError:
                success = last
            last = max(last, success.replace(tzinfo=success.tzinfo or timezone.utc))
        return (datetime.now(timezone.utc) - last).total_seconds() > 3 * self.interval

    def configure_remote_sync(self) -> None:
        repo = self.vault.repo
        assert repo is not None
        if self.REMOTE_NAME in [r.name for r in repo.remotes]:
            repo.remote(self.REMOTE_NAME).set_url(self.url)
        else:
            repo.create_remote(self.REMOTE_NAME, self.url)

    def _head_sync(self) -> tuple[str, str] | None:
        repo = self.vault.repo
        assert repo is not None
        if not repo.head.is_valid():
            return None  # empty repo, nothing to push yet
        return repo.head.commit.hexsha, repo.active_branch.name

    def _push_sync(self, sha: str, branch: str) -> None:
        """Push an already-resolved commit, so no vault lock is needed.

        Plain subprocess in its own session rather than GitPython's
        ``kill_after_timeout``: that watchdog lists children with ``ps``,
        which the slim runtime image lacks, so it never killed anything. A
        process group lets the timeout take git's transport helper down too.
        """
        proc = subprocess.Popen(
            ["git", "push", self.REMOTE_NAME, f"{sha}:refs/heads/{branch}"],
            cwd=self.vault.dir,
            # An expired token must fail, not wait on a credential prompt.
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        self._proc = proc
        try:
            _, err = proc.communicate(timeout=self.PUSH_TIMEOUT)
        except subprocess.TimeoutExpired:
            self._kill(proc)
            proc.communicate()
            raise RuntimeError(
                f"git push timed out after {int(self.PUSH_TIMEOUT)}s"
            ) from None
        finally:
            # A cancelled push's thread can finish after the next push began.
            if self._proc is proc:
                self._proc = None
        if proc.returncode != 0:
            detail = err.decode("utf-8", "replace").strip()
            raise RuntimeError(f"git push exited {proc.returncode}: {detail}")

    @staticmethod
    def _kill(proc: subprocess.Popen | None) -> None:
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass

    async def push(self) -> dict:
        async with self._push_lock:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            try:
                # The lock covers only reading HEAD; a slow remote must never
                # stall autosave, OMNIA's scoped commits, Undo or History.
                async with self.vault._git_lock:
                    head = await asyncio.to_thread(self._head_sync)
                if head is None or head[0] == self.last_pushed_sha:
                    result = {"pushed": False, "reason": "nothing new to push"}
                else:
                    sha, branch = head
                    try:
                        await asyncio.to_thread(self._push_sync, sha, branch)
                    except asyncio.CancelledError:
                        self._kill(self._proc)  # shutdown or stop(): don't strand git
                        raise
                    self.last_pushed_sha = sha
                    self.last_push = now
                    log.info("backup push %s", sha[:8])
                    result = {"pushed": True, "commit": sha}
            except Exception as e:
                self.last_error = self._redact(str(e))[:400]
                self.failures += 1
                self.failing_since = self.failing_since or now
                log.warning(
                    "backup push failed (%d in a row): %s",
                    self.failures,
                    self.last_error,
                )
                self._save()
                await self._alert_if_persistent()
                return {"pushed": False, "error": self.last_error}
            self.last_success = now
            self.last_error = None
            self.failures = 0
            self.failing_since = None
            self._save()
            return result

    async def _alert_if_persistent(self) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        if (
            self.failures < self.ALERT_AFTER
            or self.alerted_on == today
            or self.on_persistent_failure is None
        ):
            return
        key = f"backup:failing:{today}"
        alert = {
            "key": key,
            "title": f"Vault backup failing since {(self.failing_since or today)[:10]}",
            "body": self.last_error,
            "source": "omnia",
            "url": "/settings#vault",
            "tag": key,
        }
        try:
            await self.on_persistent_failure(alert)
        except Exception:
            log.warning("backup failure alert could not be delivered", exc_info=True)
            return
        self.alerted_on = today
        self._save()

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def final_push(self, timeout: float = 20.0) -> None:
        """The shutdown push, bounded so a hung remote cannot eat the stop
        grace period. Timing out cancels the push, which kills its git."""
        try:
            await asyncio.wait_for(self.push(), timeout)
        except TimeoutError:
            log.warning("backup push at shutdown gave up after %ds", int(timeout))

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            await self.push()


class AutoCommitter:
    """Debounced background auto-commit of the user's own edits.

    A commit lands once the vault has been quiet for ``debounce`` seconds,
    and never later than ``max_wait`` after the first edit it covers: steady
    typing, or a touch per streamed OMNIA delta, used to postpone history for
    as long as it lasted. A failed commit is retried with backoff and
    reported (``last_error``, ``failures``) instead of waiting silently for
    the next edit.
    """

    # After a failed commit: 5, 10, 20, 40 s, then once a minute.
    RETRY_BASE = 5.0
    RETRY_MAX = 60.0
    # `git gc --auto` after the first commit of a run, then at most this often.
    GC_INTERVAL = 6 * 60 * 60

    def __init__(self, vault: VaultRepo, debounce: float, max_wait: float = 120.0):
        self.vault = vault
        self.debounce = debounce
        self.max_wait = max_wait
        self.last_commit_at: str | None = None
        self.last_error: str | None = None
        self.failures = 0
        self._first_touch: float | None = None
        self._deadline: float | None = None
        self._last_gc: float | None = None
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None

    def touch(self, min_delay: float | None = None) -> None:
        """Schedule a commit. ``min_delay`` asks for a longer quiet than the
        debounce (Paper: each pause in writing would otherwise commit a fresh
        drawing and render); ``max_wait`` still bounds it. The latest touch
        sets the deadline, so a note edit after ink pulls it back in."""
        now = time.monotonic()
        if self._first_touch is None:
            self._first_touch = now
        delay = self.debounce if min_delay is None else max(self.debounce, min_delay)
        self._deadline = now + delay
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
            # Quiet since the last touch, or max_wait since the first.
            while True:
                assert self._first_touch is not None and self._deadline is not None
                due = min(self._deadline, self._first_touch + self.max_wait)
                remaining = due - time.monotonic()
                if remaining <= 0:
                    break
                await asyncio.sleep(remaining)
            # A touch from here on opens the next window.
            first = self._first_touch
            self._first_touch = None
            self._wake.clear()
            try:
                sha = await self.vault.commit_all("autosave")
            except Exception as exc:
                self.failures += 1
                self.last_error = f"{type(exc).__name__}: {exc}"[:400]
                delay = self.retry_delay()
                log.exception(
                    "autosave commit failed (%d in a row); retrying in %ds",
                    self.failures,
                    delay,
                )
                # The edits are still uncommitted: keep the window open, and
                # already due, so the retry needs no new touch.
                self._first_touch = first
                self._wake.set()
                await asyncio.sleep(delay)
                continue
            self.failures = 0
            self.last_error = None
            if sha:
                self.last_commit_at = datetime.now(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
                log.info("autosave commit %s", sha[:8])
                await self._gc_if_due()

    def retry_delay(self) -> float:
        # The exponent is capped too: past ~1000 failures (17 hours) the
        # float overflow would end the loop this backoff keeps alive.
        return min(self.RETRY_MAX, self.RETRY_BASE * 2 ** min(self.failures - 1, 8))

    async def _gc_if_due(self) -> None:
        now = time.monotonic()
        if self._last_gc is not None and now - self._last_gc < self.GC_INTERVAL:
            return
        # Counted from the attempt, so a failing gc is not rerun every commit.
        self._last_gc = now
        try:
            # Not under the git lock: see gc_auto_sync.
            await asyncio.to_thread(self.vault.gc_auto_sync)
        except Exception:
            log.warning("git gc --auto failed", exc_info=True)
