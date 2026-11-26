"""Repoint wikilinks that renames left dead.

Before renames rewrote inbound links (index/links.py rewrite_inbound_links),
retitling or moving a note from the editor or the sidebar left every link to
it naming a note that no longer existed. This finds the links that open
nothing today, follows the vault's git history of renames from the name each
one spells to the note that carries that content now, and repoints it,
keeping the link's form, anchor, alias and embed. Links inside code and links
whose history leads to more than one note are left alone; the latter are
listed as ambiguous.

    OMNIA_DATA_DIR=<data dir> uv run python -m app.tools.repair_links [--apply]

A dry run by default: it prints the plan and changes nothing. --apply
rewrites the notes and commits only them, as one vault commit; the index
catches up when the server next boots.

Stop the server first, or run this against a copy of the data directory: a
running server's autosave and index would race the rewrite. The user runs
this by hand. It must never be run by an agent against the live vault.
"""

import argparse
import re
import sys
from dataclasses import dataclass, field

from git import Repo

from ..config import Settings
from ..index.links import LinkResolver, names_note, relink, rewrite_links
from ..vault.notes import NoteError, NoteStore
from ..vault.repo import VaultRepo

COMMIT_MESSAGE = "Repair links left dead by renames"
_STATUS_RE = re.compile(r"R\d*")


def rename_history(repo: Repo) -> list[list[tuple[str, str]]]:
    """Each commit's note renames as (old, new) paths, oldest commit first."""
    # -z: paths arrive raw, never quoted or escaped; \x01 marks each commit.
    out = repo.git.log("-M", "--diff-filter=R", "--name-status", "-z", "--format=%x01")
    commits = []
    for chunk in reversed(out.split("\x01")):
        tokens = chunk.split("\0")
        renames = []
        i = 0
        while i < len(tokens):
            if _STATUS_RE.fullmatch(tokens[i].strip()) and i + 2 < len(tokens):
                old, new = tokens[i + 1], tokens[i + 2]
                if old.endswith(".md") and new.endswith(".md"):
                    renames.append((old, new))
                i += 3
            else:
                i += 1
        if renames:
            commits.append(renames)
    return commits


class History:
    def __init__(self, commits: list[list[tuple[str, str]]]):
        self.by_old: dict[str, list[tuple[int, str]]] = {}
        for index, renames in enumerate(commits):
            for old, new in renames:
                self.by_old.setdefault(old, []).append((index, new))

    def _follow(self, index: int, path: str) -> str:
        # Commit indexes only grow along a chain, so this ends.
        while True:
            later = next((hop for hop in self.by_old.get(path, ()) if hop[0] > index), None)
            if later is None:
                return path
            index, path = later

    def successors(self, target: str, notes: set[str]) -> set[str]:
        """The notes today that a rename chain leads to from any path
        `target` names."""
        ends = set()
        for old, hops in self.by_old.items():
            if not names_note(target, old):
                continue
            for index, new in hops:
                end = self._follow(index, new)
                if end in notes:
                    ends.add(end)
        return ends


@dataclass
class Repair:
    path: str
    content: str
    changes: list[tuple[str, str]] = field(default_factory=list)


def plan(store: NoteStore, repo: Repo) -> tuple[list[Repair], dict[str, tuple[list[str], list[str]]]]:
    """(repairs, ambiguous). A repair is one note's rewritten content and its
    (old, new) link targets; ambiguous maps a dead target to (the notes it
    could mean, the notes holding it)."""
    paths = [n.path for n in store.list_notes()]
    notes = set(paths)
    resolver = LinkResolver(paths)
    history = History(rename_history(repo))
    decided: dict[str, str | None] = {}
    ambiguous: dict[str, tuple[list[str], list[str]]] = {}

    def repoint(target: str) -> str | None:
        if resolver.resolve(target) is not None:
            return None
        if target not in decided:
            ends = sorted(history.successors(target, notes))
            decided[target] = relink(target, ends[0], paths) if len(ends) == 1 else None
            if len(ends) > 1:
                ambiguous[target] = (ends, [])
        return decided[target]

    repairs = []
    for path in paths:
        try:
            content = store.read(path)
        except NoteError:
            continue
        repair = Repair(path, content)

        def swap(target: str, repair=repair, path=path) -> str | None:
            new = repoint(target)
            if new is not None:
                repair.changes.append((target, new))
            elif target in ambiguous and path not in ambiguous[target][1]:
                ambiguous[target][1].append(path)
            return new

        repair.content, changed = rewrite_links(content, swap)
        if changed:
            repairs.append(repair)
    return repairs, ambiguous


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.tools.repair_links",
        description="Repoint wikilinks left dead by past renames, following the vault's git "
        "history. Stop the server first. Prints the plan unless --apply is given.",
    )
    parser.add_argument(
        "--apply", action="store_true", help="rewrite the notes and commit them"
    )
    args = parser.parse_args(argv)

    settings = Settings()
    vault_dir = settings.vault_dir
    if not (vault_dir / ".git").is_dir():
        print(f"No vault at {vault_dir}", file=sys.stderr)
        return 2
    repo = Repo(vault_dir)
    store = NoteStore(vault_dir)
    repairs, ambiguous = plan(store, repo)

    for repair in repairs:
        print(repair.path)
        for old, new in repair.changes:
            print(f"  [[{old}]] -> [[{new}]]")
    for target, (ends, holders) in sorted(ambiguous.items()):
        print(f"ambiguous: [[{target}]] in {', '.join(holders)} could be {', '.join(ends)}")
    count = sum(len(repair.changes) for repair in repairs)
    print(f"{count} link(s) to repoint in {len(repairs)} note(s)")
    if not args.apply or not repairs:
        return 0

    for repair in repairs:
        store.write(repair.path, repair.content)
    vault = VaultRepo(vault_dir, settings.git_author_name, settings.git_author_email)
    vault.repo = repo
    sha = vault.commit_paths_sync(
        COMMIT_MESSAGE, [repair.path for repair in repairs], vault.user_author
    )
    print(f"committed {sha[:10]}" if sha else "nothing to commit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
