"""Per-folder task boards.

A folder can own one task board: an ordinary note carrying `task_board: true`
in its frontmatter. Its open tasks stay off the universal Tasks board and the
calendar — that view means "everything on my plate" — but completed ones
rejoin both, because finished work is a record of the week wherever it was
tracked.

The board is identified by frontmatter rather than by filename so it survives
a rename and cannot be created by accident with a note called "Tasks".

Imported by the indexer, so this module must not import from ``app.index``.
"""

from pathlib import Path

from .vault import fm

BOARD_KEY = "task_board"
BOARD_STEM = "Tasks"
BOARD_TEMPLATE = f'---\ntitle: "{BOARD_STEM}"\n{BOARD_KEY}: true\n---\n\n# {BOARD_STEM}\n'


def is_board(meta: dict) -> bool:
    """True only for a literal `task_board: true`.

    Strict identity, not truthiness: someone typing `task_board: maybe` in the
    editor should get a note they can still see, not a silently hidden one.
    """
    return meta.get(BOARD_KEY) is True


def strip_board_flag(content: str) -> str:
    """The same note without its board flag. A copy of a board (a conflict
    copy, a duplicate) lands in the same folder and must not become a second
    board there."""
    meta, body = fm.parse(content)
    if BOARD_KEY not in meta:
        return content
    meta.pop(BOARD_KEY)
    return fm.serialize(meta, body)


def folder_of(path: str) -> str:
    """`a/b/c.md` -> `a/b`; a note at the vault root -> `""`."""
    parent = Path(path).parent
    return "" if str(parent) == "." else str(parent)


def parent_folder(folder: str) -> str:
    """`a/b/c` -> `a/b`; a top-level folder -> `""`."""
    if not folder:
        return ""
    parent = Path(folder).parent
    return "" if str(parent) == "." else str(parent)


async def board_map(db) -> dict[str, str]:
    """Folder -> its board note. A vault has a handful of boards, so the
    grouping is cheaper in Python than in SQLite string functions.

    Should a folder end up with two (a hand-added flag, a synced copy), the
    oldest wins. Unordered, the last row did, and every save re-inserts its
    row, so the board flipped to whichever note was saved last. A conflict
    copy or duplicate keeps the original's `created`, and its longer name
    (`Tasks copy.md`) sorts before `Tasks.md` as text, so on a tie the
    shorter path wins.
    """
    rows = await db.execute_fetchall(
        f"SELECT path, created FROM notes WHERE {BOARD_KEY} = 1"
        " ORDER BY created, length(path), path"
    )
    boards: dict[str, str] = {}
    for row in rows:
        boards.setdefault(folder_of(row["path"]), row["path"])
    return boards


async def board_for_folder(db, folder: str) -> str | None:
    return (await board_map(db)).get(folder.strip("/"))


async def nearest_board(db, path: str | None) -> str | None:
    """The board governing `path`: its own folder's, else the closest
    ancestor's. `Projects/2026/plan.md` -> Projects/2026, else Projects, else
    None."""
    if not path:
        return None
    boards = await board_map(db)
    folder = folder_of(path)
    while True:
        if folder in boards:
            return boards[folder]
        if not folder:
            return None
        folder = parent_folder(folder)


def next_board_path(folder: str, taken: set[str]) -> str:
    """`<folder>/Tasks.md`, stepping past anything already there — the same
    numbering the sidebar uses for untitled notes."""
    base = f"{folder}/{BOARD_STEM}"
    candidate = f"{base}.md"
    number = 1
    lowered = {path.lower() for path in taken}
    while candidate.lower() in lowered:
        number += 1
        candidate = f"{base} {number}.md"
    return candidate
