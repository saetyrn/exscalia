"""Name rules for notes and folders.

One canonical spelling for a folder path, a narrow check on a new note name,
and the identity two names collide on. Only new names are checked (create, a
rename that changes the name); reads stay permissive so a file that predates
a rule stays reachable.

Plain functions raising ValueError: NoteStore turns them into NoteErrors, so
this module imports nothing from the store.
"""
import unicodedata
from pathlib import PurePosixPath

# Folders the app itself relies on (daily notes, OMNIA memory, templates,
# media tracker items, Paper notebooks). The one definition: NoteStore and
# TrashStore enforce it, so the API routes and the OMNIA tools cannot drift.
PROTECTED_FOLDERS = frozenset({"daily", "omnia", "templates", "media", "paper"})

# ext4 and APFS allow 255 bytes per name, which is 85 CJK characters. The
# headroom keeps a conflict or restored copy's suffix inside that limit.
MAX_COMPONENT_BYTES = 200

# The wikilink grammar ends a target at '#', '|' or ']' (index/parse.py
# WIKILINK_RE, frontend wikilink.ts): a note named with one can never be
# linked, and [[C# tips]] opens or creates "C".
_LINK_BREAKING = "[]#|"


def identity_key(name: str) -> str:
    """What two names collide on: the same text once NFC-normalized and
    case-folded. APFS treats such names as one file; ext4 keeps both."""
    return unicodedata.normalize("NFC", name).casefold()


def normalize_folder(rel_path: str) -> str:
    """The canonical spelling of a vault-relative folder: 'Work/', './Work'
    and 'Work' are one folder. Refuses the vault root itself ('' or '.'), an
    absolute path and '..'."""
    p = PurePosixPath(rel_path or "")
    if p.is_absolute() or ".." in p.parts:
        raise ValueError("Invalid folder path")
    rel = p.as_posix()
    if rel == ".":
        raise ValueError("Invalid folder path")
    return rel


def is_protected_folder(rel: str) -> bool:
    """True for a built-in root itself, in any case: on a case-insensitive
    filesystem 'Daily' is the daily folder. `rel` is already normalized."""
    return "/" not in rel and rel.casefold() in PROTECTED_FOLDERS


def validate_note_stem(stem: str) -> None:
    """Refuse a new note name that could not be linked or stored cleanly.

    Deliberately narrow: the filename is the title, so ordinary punctuation
    (? : " * < >) stays allowed.
    """
    if not stem:
        raise ValueError("Note name cannot be empty")
    if any(unicodedata.category(c) == "Cc" for c in stem):
        raise ValueError("Note names cannot contain control characters")
    if any(c in _LINK_BREAKING for c in stem):
        raise ValueError("Note names cannot contain [ ] # |")
    # A wikilink target is trimmed, so [[ Plan]] could never reach " Plan".
    if stem != stem.strip():
        raise ValueError("Note names cannot start or end with a space")
    size = len(stem.encode("utf-8"))
    if size > MAX_COMPONENT_BYTES:
        raise ValueError(
            f"Note name is too long ({size} bytes; the limit is {MAX_COMPONENT_BYTES})"
        )


def truncate_stem_bytes(stem: str, budget: int) -> str:
    """`stem` cut to at most `budget` UTF-8 bytes, on a character boundary."""
    raw = stem.encode("utf-8")
    if len(raw) <= budget:
        return stem
    return raw[:budget].decode("utf-8", "ignore").rstrip()
