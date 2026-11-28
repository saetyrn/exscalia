"""The sidebar's saved arrangement follows renames.

Manual order is a list of paths in the vault config (`note_order` and
`folder_order`, served by api/meta.py). The sidebar sorts a path the list does
not know after the arranged ones, so a rename that left the list alone sent
the note, or a folder and every note inside it, to the bottom of its group.
"""

from pathlib import Path
from typing import Sequence

from .vault.internal import atomic_json, read_json
from .vault.notes import NoteError, NoteStore

CONFIG_PATH = f"{NoteStore.INTERNAL_DIR}/config.json"


def _clean(path) -> str:
    # The normalization api/meta.py's _folder_order gives saved entries.
    return str(path).strip().strip("/").strip()


def rename_paths(
    vault_dir: Path,
    *,
    notes: Sequence[tuple[str, str]] = (),
    folders: Sequence[tuple[str, str]] = (),
) -> bool:
    """Carry renamed notes and folders to their new paths in the saved order.

    `notes` are (old, new) note paths; `folders` are (old, new) folder paths
    and also move every entry beneath them. A move's destination did not exist
    before it, so an entry already naming one is stale and gives way to the
    moved entry instead of outranking it. Returns whether the config changed,
    so a caller that commits by path can include it.
    """
    note_moves = {_clean(old): _clean(new) for old, new in notes}
    folder_moves = [(_clean(old), _clean(new)) for old, new in folders]
    note_moves.pop("", None)
    folder_moves = [(old, new) for old, new in folder_moves if old and new]
    if not note_moves and not folder_moves:
        return False
    path = vault_dir / CONFIG_PATH
    try:
        config = read_json(path, {})
    except NoteError:
        # read_json already set the unreadable file aside. The arrangement is
        # a preference; a rename must not fail over it.
        return False
    if not isinstance(config, dict):
        return False

    def under(entry: str, folder: str) -> bool:
        return entry == folder or entry.startswith(folder + "/")

    def rewrite(raw: list, moves: dict[str, str]) -> list[str] | None:
        order: list[str] = []
        seen: set[str] = set()
        touched = False
        for item in raw:
            entry = _clean(item)
            new = moves.get(entry)
            if new is None:
                new = next(
                    (dst + entry[len(src) :] for src, dst in folder_moves if under(entry, src)),
                    None,
                )
            if new is not None:
                entry = new
                touched = True
            elif entry in moves.values() or any(under(entry, dst) for _, dst in folder_moves):
                touched = True
                continue
            if entry and entry not in seen:
                seen.add(entry)
                order.append(entry)
        return order if touched else None

    changed = False
    # recipe_order is the Recipes menu (app/recipes.py), note paths as well.
    for key, moves in (
        ("note_order", note_moves),
        ("folder_order", {}),
        ("recipe_order", note_moves),
    ):
        raw = config.get(key)
        if not isinstance(raw, list):
            continue
        order = rewrite(raw, moves)
        if order is not None and order != raw:
            config[key] = order
            changed = True
    if changed:
        atomic_json(path, config)
    return changed
