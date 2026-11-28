"""Paper folders: a directory under ``paper/`` with its record inside it.

A folder used to be nothing but the path segments of the notebooks in it. It
is now a thing of its own: ``paper/<…>/<Folder>/folder.json`` holds a stable
id, the verbatim name, a colour, an icon and the favourite flag. The record
lives inside the directory it describes, so a rename, a git restore or a copy
carries it along and no registry can disagree with the tree.

A directory that predates the record (or that git brought back without one)
gets an identity on first contact, derived from its path alone, so the server
and a device that meet the same directory agree on the id without talking:
``legacy_folder_id``. GETs never write; they synthesise that record with
``seq`` 0, and :meth:`PaperFolderStore.ensure_folder_records` writes it at
boot and whenever a write creates a directory implicitly.

Renames and moves are one directory rename, one ``folder.json`` write and one
move record in ``.exscalia/paper/moves.json``. Nothing inside is restamped:
another device applies the move as one local directory rename, with no page
downloaded. A delete moves the whole directory into the Trash as one item of
kind ``paper_folder`` and tombstones every notebook and folder inside at one
seq.

Within one parent no two entries may share a name after NFC and casefold —
folder directories by name, notebook packages without ``.paper`` — so a vault
written on case-sensitive Linux still checks out on case-insensitive macOS.

Like the rest of :mod:`app.paper_store`, nothing here talks to git, the index
or the network; ``api/paper.py`` owns those.
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
import uuid
from pathlib import Path
from typing import Any

from .media_items import media_slug
from .vault.internal import atomic_json, iso_utc, read_json
from .vault.notes import NoteError, NoteStore

log = logging.getLogger(__name__)

PAPER_DIR = NoteStore.PAPER_DIR
PACKAGE_SUFFIX = ".paper"

FOLDER_JSON = "folder.json"
# The Trash kind of a trashed Paper folder. Not "folder": that already means a
# folder of notes, which restores through a different path.
FOLDER_TRASH_KIND = "paper_folder"

FOLDER_COLORS = (
    "red", "orange", "yellow", "green", "mint", "teal",
    "blue", "indigo", "purple", "pink", "brown", "gray",
)
DEFAULT_FOLDER_COLOR = "blue"
MAX_FOLDER_NAME = 200
MAX_SYMBOL_NAME = 64
MAX_EMOJI_SCALARS = 16
# So `/pages/<id>.conflict-<stamp>/drawing.pkdrawing` under any notebook in the
# deepest folder still fits a 1,024-byte PATH_MAX.
MAX_VAULT_PATH_BYTES = 900

# folder.json's own keys; anything else in the file is kept on rewrite.
FOLDER_RECORD_KEYS = (
    "id", "name", "color", "icon", "favorite", "created", "modified", "seq",
)

_FOLDER_ID = re.compile(r"^[0-9a-f]{32}$")
_SYMBOL_NAME = re.compile(r"^[a-z0-9]+(\.[a-z0-9]+)*$")
RENAME_TEMP_PREFIX = ".rename-"


# -- names, ids and values ---------------------------------------------------


def fold(name: str) -> str:
    """The form two entry names are compared in: NFC, then casefold."""
    return unicodedata.normalize("NFC", str(name)).casefold()


def folder_slug(name: str) -> str:
    """The directory name for a folder called ``name``.

    ``media_slug`` (the iPad's ``PaperPaths.slug``) plus a trailing ``_`` when
    the result ends in ``.paper`` or is ``folder.json``, ignoring case: the
    first would read as a notebook package, the second would collide with
    its parent's record. ``tests/fixtures/folder_slug_vectors.json`` is the
    contract both sides test against.
    """
    slug = media_slug(str(name or ""))
    folded = slug.casefold()
    if folded.endswith(PACKAGE_SUFFIX) or folded == FOLDER_JSON:
        slug += "_"
    return slug


def legacy_folder_id(path: str) -> str:
    """The id of a folder met without a record: a hash of its vault path, so
    everyone who meets the same directory derives the same id."""
    data = ("paper-folder:" + unicodedata.normalize("NFC", path)).encode("utf-8")
    return hashlib.sha256(data).hexdigest()[:32]


def new_folder_id() -> str:
    return uuid.uuid4().hex


def parent_of(path: str) -> str:
    """``paper`` for a top-level folder or notebook."""
    return path.rpartition("/")[0] or PAPER_DIR


def is_under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def check_folder_id(value: Any) -> str:
    text = str(value or "")
    if not _FOLDER_ID.match(text):
        raise NoteError(400, "A folder id is 32 lowercase hex characters")
    return text


def check_folder_name(value: Any) -> str:
    """Verbatim and trimmed; empty becomes Untitled."""
    name = str(value or "").strip() or "Untitled"
    if len(name) > MAX_FOLDER_NAME:
        raise NoteError(400, f"A folder name is at most {MAX_FOLDER_NAME} characters")
    return name


def check_folder_color(value: Any) -> str:
    if value in (None, ""):
        return DEFAULT_FOLDER_COLOR
    color = str(value).strip().lower()
    if color not in FOLDER_COLORS:
        raise NoteError(400, f"Unknown folder colour: {value}")
    return color


def _valid_icon(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    kind, _, body = value.partition(":")
    if kind == "symbol":
        return len(body) <= MAX_SYMBOL_NAME and bool(_SYMBOL_NAME.match(body))
    if kind == "emoji":
        return 1 <= len(body) <= MAX_EMOJI_SCALARS and all(
            ord(char) > 0x7F
            and not char.isspace()
            and unicodedata.category(char) not in ("Cc", "Cs", "Zs", "Zl", "Zp")
            for char in body
        )
    return False


def check_folder_icon(value: Any) -> str | None:
    """``"symbol:<name>"``, ``"emoji:<text>"`` or none (null or ``""``)."""
    if value in (None, ""):
        return None
    if not _valid_icon(value):
        raise NoteError(400, f"Invalid folder icon: {value}")
    return str(value)


def normalize_record(stored: Any, path: str, now: str | None = None) -> dict:
    """folder.json's fields as the wire carries them, for a record that may
    be missing, partial or hand-edited. Missing means the legacy record: the
    directory's name, blue, no icon, not a favourite, ``seq`` 0. An unknown
    colour reads as blue and an icon that does not parse as none."""
    now = now or iso_utc()
    if not isinstance(stored, dict):
        stored = {}
    folder_id = stored.get("id")
    if not isinstance(folder_id, str) or not _FOLDER_ID.match(folder_id):
        folder_id = legacy_folder_id(path)
    name = stored.get("name")
    name = name.strip() if isinstance(name, str) else ""
    color = stored.get("color")
    created = stored.get("created") if isinstance(stored.get("created"), str) else None
    modified = stored.get("modified") if isinstance(stored.get("modified"), str) else None
    try:
        seq = int(stored.get("seq") or 0)
    except (TypeError, ValueError):
        seq = 0
    return {
        "id": folder_id,
        "name": name or Path(path).name,
        "color": color if color in FOLDER_COLORS else DEFAULT_FOLDER_COLOR,
        "icon": stored.get("icon") if _valid_icon(stored.get("icon")) else None,
        "favorite": stored.get("favorite") is True,
        "created": created or modified or now,
        "modified": modified or created or now,
        "seq": seq,
    }


def has_record(stored: Any) -> bool:
    """Whether folder.json holds an id this folder can keep."""
    return (
        isinstance(stored, dict)
        and isinstance(stored.get("id"), str)
        and bool(_FOLDER_ID.match(stored["id"]))
    )


# -- the store half ----------------------------------------------------------


class PaperFolderStore:
    """Folder records, mixed into :class:`app.paper_store.PaperStore`.

    Uses the store's path checks, seq counter, tombstones and Trash. Every
    method that writes takes the change's seq itself, so the API layer only
    has to hold its locks around the call.
    """

    # -- paths ---------------------------------------------------------------

    def _child_dirs(self, rel: str) -> list[Path]:
        base = self.dir / rel
        if not base.is_dir():
            return []
        return sorted(child for child in base.iterdir() if child.is_dir())

    def canonical_folder(self, rel: str) -> str:
        """``rel`` spelled as the directories on disk spell it.

        Each segment takes the name of the existing folder it matches exactly,
        else the one it matches after NFC and casefold; from the first segment
        that matches nothing, the rest is kept as given. Clients send the path
        they know, and on case-sensitive Linux "paper/work" must still find
        "paper/Work" rather than becoming its twin.
        """
        parts = rel.split("/")
        out = [parts[0]]
        for index, part in enumerate(parts[1:], start=1):
            candidates = [
                child.name
                for child in self._child_dirs("/".join(out))
                if not child.name.endswith(PACKAGE_SUFFIX)
            ]
            if part in candidates:
                out.append(part)
                continue
            wanted = fold(part)
            match = next((name for name in candidates if fold(name) == wanted), None)
            if match is None:
                out.extend(parts[index:])
                break
            out.append(match)
        return "/".join(out)

    def folder_rel(self, rel_path: str | None) -> str:
        """A folder route's path, normalized and canonical. Never ``paper``."""
        rel = self.folder_path(rel_path)
        if rel == PAPER_DIR:
            raise NoteError(400, "A folder path is required")
        return self.canonical_folder(rel)

    def find_folder(self, rel_path: str | None) -> str:
        rel = self.folder_rel(rel_path)
        if not (self.dir / rel).is_dir():
            raise NoteError(404, f"Folder not found: {rel}")
        return rel

    def _taken(self, parent_rel: str, exclude: str | None = None) -> set[str]:
        """The folded entry names in a parent: folders by name, notebook
        packages without ``.paper``."""
        taken = set()
        for child in self._child_dirs(parent_rel):
            if child.name == exclude:
                continue
            name = child.name
            if name.endswith(PACKAGE_SUFFIX):
                name = name[: -len(PACKAGE_SUFFIX)]
            taken.add(fold(name))
        return taken

    def free_entry_name(
        self, parent_rel: str, base: str, exclude: str | None = None
    ) -> str:
        """``base``, or ``base 2``, ``base 3``… — the first no sibling takes."""
        taken = self._taken(parent_rel, exclude)
        if fold(base) not in taken:
            return base
        number = 2
        while fold(f"{base} {number}") in taken:
            number += 1
        return f"{base} {number}"

    @staticmethod
    def _check_length(rel: str, extra: int = 0) -> None:
        if len(rel.encode("utf-8", "surrogatepass")) + extra > MAX_VAULT_PATH_BYTES:
            raise NoteError(
                400, f"A Paper path is at most {MAX_VAULT_PATH_BYTES} bytes long"
            )

    def folders_under(self, rel: str) -> list[str]:
        """``rel`` and every folder inside it, sorted."""
        return [path for path in self.folders() if is_under(path, rel)]

    def notebooks_under(self, rel: str) -> list[str]:
        return [path for path in self.notebook_dirs() if is_under(path, rel)]

    # -- records -------------------------------------------------------------

    def _folder_file(self, rel: str) -> Path:
        return self.dir / rel / FOLDER_JSON

    def _stored_folder(self, rel: str) -> dict | None:
        """folder.json as written, or None. A corrupt file is set aside as
        ``folder.json.corrupt-<stamp>`` and reads as missing."""
        value = read_json(self._folder_file(rel), None, skip_corrupt=True)
        return value if isinstance(value, dict) else None

    def folder_record(self, rel: str, stored: dict | None = None) -> dict:
        """The wire FolderRecord: folder.json's fields plus ``path`` and
        ``parent``; synthesised, with seq 0, for a folder without one."""
        if stored is None:
            stored = self._stored_folder(rel)
        return {**normalize_record(stored, rel), "path": rel, "parent": parent_of(rel)}

    def folder_records(self) -> list[dict]:
        """Every folder's record, sorted by path. Reads only.

        A second folder carrying an id another already holds (a directory
        copied by hand) is reported under its legacy id, as the boot pass
        will write it; the earlier path keeps the id.
        """
        records = []
        seen: set[str] = set()
        for rel in self.folders():
            record = self.folder_record(rel)
            if record["id"] in seen:
                record["id"] = legacy_folder_id(rel)
            seen.add(record["id"])
            records.append(record)
        return records

    def folder_ids(self) -> dict[str, str]:
        """``{path: id}`` for every folder, as :meth:`folder_records` says."""
        return {record["path"]: record["id"] for record in self.folder_records()}

    def find_folder_by_id(self, folder_id: str) -> dict | None:
        return next(
            (record for record in self.folder_records() if record["id"] == folder_id),
            None,
        )

    def _write_folder(
        self, rel: str, record: dict, *, seq: int | None = None, touch: bool = True
    ) -> dict:
        """Write folder.json: the record's fields over whatever else the file
        holds. ``touch`` restamps ``modified``, as every user edit does; a
        re-stamp for the feed passes False and keeps it."""
        stored = self._stored_folder(rel) or {}
        out = {
            key: value
            for key, value in stored.items()
            if key not in FOLDER_RECORD_KEYS and key not in ("path", "parent")
        }
        for key in FOLDER_RECORD_KEYS:
            if key in record:
                out[key] = record[key]
        if touch:
            out["modified"] = iso_utc()
        out["seq"] = int(seq) if seq is not None else self.next_seq()
        atomic_json(self._folder_file(rel), out)
        return self.folder_record(rel, out)

    def ensure_folder_records(
        self, paths: list[str] | None = None, *, seq: int | None = None
    ) -> list[str]:
        """Give every folder that lacks one its record. Returns the paths
        written.

        With no ``paths`` this is the boot pass over the whole tree, which
        first puts back any directory a case-only rename left half way
        (:meth:`recover_interrupted_renames`), and also re-ids a folder whose
        id an earlier path already holds. Each record written takes a new
        seq, or ``seq`` when the caller hands one over: the write that
        created a directory implicitly shares its own.
        """
        written = []
        seen: set[str] = set()
        whole_tree = paths is None
        if whole_tree:
            self.recover_interrupted_renames()
        for rel in sorted(self.folders() if whole_tree else paths):
            if not (self.dir / rel).is_dir() or rel == PAPER_DIR:
                continue
            stored = self._stored_folder(rel)
            if has_record(stored) and not (whole_tree and stored["id"] in seen):
                seen.add(stored["id"])
                continue
            record = normalize_record(stored, rel)
            if record["id"] in seen:
                # A copy of a folder an earlier path holds the id of.
                record["id"] = legacy_folder_id(rel)
            if record["id"] in seen:
                record["id"] = new_folder_id()
            seen.add(record["id"])
            self._write_folder(rel, record, seq=seq, touch=False)
            written.append(rel)
        return written

    def recover_interrupted_renames(self) -> list[str]:
        """A rename that only changes case goes through a sibling called
        ``.rename-<id>`` (a case-insensitive disk sees the target as the
        source itself). A process stopped between its two renames leaves the
        directory there, where it reads as a folder of that name and nothing
        finds its notebooks. Each one goes back to the name its record carries
        -- a folder's ``folder.json`` name, a notebook's title, which the
        rename had not rewritten yet -- or beside it as "<name> 2" when that
        is taken. Returns the paths it put back. Never raises: a directory it
        cannot place is logged and left, and the boot goes on."""
        recovered: list[str] = []
        if not self.root.is_dir():
            return recovered
        stack = [self.root]
        while stack:
            current = stack.pop()
            try:
                children = sorted(current.iterdir())
            except OSError:
                continue
            for child in children:
                if not child.is_dir() or child.name.endswith(PACKAGE_SUFFIX):
                    continue
                if not child.name.startswith(RENAME_TEMP_PREFIX):
                    stack.append(child)
                    continue
                try:
                    target = child.parent / self._interrupted_rename_target(child)
                    child.rename(target)
                except Exception:  # never stop the boot
                    log.exception("could not put back %s", child)
                    continue
                recovered.append(str(target.relative_to(self.dir)))
                log.info("put back an interrupted rename as %s", recovered[-1])
                if not target.name.endswith(PACKAGE_SUFFIX):
                    stack.append(target)
        return recovered

    def _interrupted_rename_target(self, temp: Path) -> str:
        parent = str(temp.parent.relative_to(self.dir))
        manifest = temp / "notebook.json"
        if manifest.is_file():
            value = read_json(manifest, {}, skip_corrupt=True)
            title = str(value.get("title") or "") if isinstance(value, dict) else ""
            base = media_slug(title or "Untitled")
            return self.free_entry_name(parent, base, exclude=temp.name) + PACKAGE_SUFFIX
        stored = self._stored_folder(str(temp.relative_to(self.dir)))
        name = stored.get("name") if isinstance(stored, dict) else None
        base = folder_slug(str(name or "Untitled"))
        return self.free_entry_name(parent, base, exclude=temp.name)

    def ensure_ancestor_records(self, rel: str, *, seq: int) -> list[str]:
        """Records for every folder above ``rel`` (a notebook or a folder)
        that has none, at ``seq``. A directory created where a trashed folder
        used to be is a new folder: its tombstone is cleared, and the trashed
        one restores beside it under a suffix."""
        ancestors = []
        current = parent_of(rel)
        while current != PAPER_DIR:
            ancestors.append(current)
            current = parent_of(current)
        written = self.ensure_folder_records(ancestors, seq=seq)
        self.clear_tombstones(written)
        return written

    # -- create --------------------------------------------------------------

    def create_folder(
        self,
        *,
        parent: str | None,
        name: Any,
        folder_id: str | None = None,
        color: Any = None,
        icon: Any = None,
        favorite: bool = False,
    ) -> dict:
        """A new folder, or the one that is already there.

        Idempotent by id: a retry finds the folder it made, wherever it has
        moved since. A folder already holding the name's slug wins over the
        request, which is never suffixed (``existing: true``); two devices that
        each made "Work" offline end with one folder. A directory with no
        record is claimed with the request's id. A notebook holding the name
        pushes the new folder to "<name> 2".
        """
        name = check_folder_name(name)
        folder_id = check_folder_id(folder_id) if folder_id else new_folder_id()
        color = check_folder_color(color)
        icon = check_folder_icon(icon)
        parent_rel = self.canonical_folder(self.folder_path(parent))

        now = iso_utc()
        record = {
            "id": folder_id,
            "name": name,
            "color": color,
            "icon": icon,
            "favorite": bool(favorite),
            "created": now,
            "modified": now,
        }
        found = self.find_folder_by_id(folder_id)
        if found is not None:
            if has_record(self._stored_folder(found["path"])):
                return {**found, "existing": False}
            # The id is the legacy one of a directory still without a record.
            written = self._write_folder(found["path"], record, touch=False)
            return {**written, "existing": False}

        slug = folder_slug(name)
        wanted = fold(slug)
        match = next(
            (
                child.name
                for child in self._child_dirs(parent_rel)
                if not child.name.endswith(PACKAGE_SUFFIX) and fold(child.name) == wanted
            ),
            None,
        )
        if match is not None:
            rel = f"{parent_rel}/{match}"
            stored = self._stored_folder(rel)
            if has_record(stored) and stored["id"] != folder_id:
                return {**self.folder_record(rel, stored), "existing": True}
            # A directory nobody has given a record: it is this folder now.
            written = self._write_folder(rel, record, touch=False)
            self.clear_tombstones([rel])
            return {**written, "existing": False}

        rel = f"{parent_rel}/{self.free_entry_name(parent_rel, slug)}"
        self._check_length(rel)
        seq = self.next_seq()
        (self.dir / rel).mkdir(parents=True)
        self.ensure_ancestor_records(rel, seq=seq)
        written = self._write_folder(rel, record, seq=seq, touch=False)
        self.clear_tombstones([rel])
        return {**written, "existing": False}

    # -- rename and move -----------------------------------------------------

    def rename_folder(
        self, rel_path: str, new_path: str, name: Any = None
    ) -> dict:
        """Rename, move, or both: ``new_path`` names the directory wanted.

        With ``name`` the folder is retitled too; without it a move keeps its
        name. ``new_path`` is a wish: a sibling already holding the name
        pushes this one to "<name> 2", and ``path`` in the answer is where it
        went. One directory rename, one folder.json write and one move record;
        nothing inside is restamped, so other devices follow with one local
        directory rename. A rename that only changes case passes through a
        temporary sibling, which a case-insensitive disk needs.
        """
        src = self.find_folder(rel_path)
        wish = self.folder_path(new_path)
        if wish == PAPER_DIR:
            raise NoteError(400, "A folder path is required")
        leaf = wish.rpartition("/")[2]
        if (
            fold(leaf) == FOLDER_JSON
            or fold(leaf).endswith(PACKAGE_SUFFIX)
            or leaf.startswith(RENAME_TEMP_PREFIX)
        ):
            raise NoteError(400, f"Invalid folder name: {leaf}")
        title = check_folder_name(name) if name is not None else None
        dst_parent = self.canonical_folder(parent_of(wish))
        if is_under(fold(dst_parent), fold(src)):
            raise NoteError(400, "A folder cannot move into itself or a folder inside it")

        stored = self._stored_folder(src)
        record = self.folder_record(src, stored)
        if title is not None:
            record["name"] = title
        same_parent = dst_parent == parent_of(src)
        src_name = src.rpartition("/")[2]
        dst = f"{dst_parent}/" + self.free_entry_name(
            dst_parent, leaf, exclude=src_name if same_parent else None
        )
        if dst == src:
            if title is None or (has_record(stored) and stored.get("name") == title):
                return {"folder": record, "from": src, "path": src}
            return {"folder": self._write_folder(src, record), "from": src, "path": src}

        under = self.folders_under(src)[1:] + self.notebooks_under(src)
        self._check_length(
            dst,
            max((len(path[len(src):].encode("utf-8", "surrogatepass")) for path in under), default=0),
        )
        seq = self.next_seq()
        # Folders inside that have no record yet would change id with their
        # path; they get the one they have now, before it moves.
        self.ensure_folder_records(self.folders_under(src)[1:], seq=seq)
        source = self.dir / src
        target = self.dir / dst
        if not same_parent:
            target.parent.mkdir(parents=True, exist_ok=True)
            self.ensure_ancestor_records(dst, seq=seq)
        if fold(dst) == fold(src):
            temp = source.parent / f"{RENAME_TEMP_PREFIX}{record['id']}"
            source.rename(temp)
            temp.rename(target)
        else:
            if target.exists():
                raise NoteError(409, f"Folder already exists: {dst}")
            source.rename(target)
        moved = self._write_folder(dst, record, seq=seq)
        self.record_move(record["id"], src, dst, seq)
        return {"folder": moved, "from": src, "path": dst}

    # -- appearance ----------------------------------------------------------

    def patch_folder(self, rel_path: str, patch: dict) -> dict:
        """Colour, icon (``""`` clears) and favourite. Never the name. A patch
        that changes nothing writes nothing."""
        rel = self.find_folder(rel_path)
        stored = self._stored_folder(rel)
        record = self.folder_record(rel, stored)
        changed = not has_record(stored)
        if patch.get("color") is not None:
            color = check_folder_color(patch["color"])
            changed = changed or color != record["color"]
            record["color"] = color
        if "icon" in patch:
            icon = check_folder_icon(patch["icon"])
            changed = changed or icon != record["icon"]
            record["icon"] = icon
        if patch.get("favorite") is not None:
            favorite = bool(patch["favorite"])
            changed = changed or favorite != record["favorite"]
            record["favorite"] = favorite
        if not changed:
            return record
        return self._write_folder(rel, record)

    # -- delete and restore --------------------------------------------------

    def delete_folder(self, rel_path: str) -> dict:
        """Move the folder, with everything in it, into the Trash as one item.

        At one seq, every notebook inside is tombstoned (``notebook``) and
        every folder inside, this one included (``folder``, with its id).
        Folders without a record get one first, so a restore brings back the
        ids devices knew.
        """
        rel = self.find_folder(rel_path)
        seq = self.next_seq()
        folders = self.folders_under(rel)
        self.ensure_folder_records(folders, seq=seq)
        records = [self.folder_record(path) for path in folders]
        notebooks = self.notebooks_under(rel)
        item = self.trash.trash_paper(
            FOLDER_TRASH_KIND,
            rel,
            extra={
                "folder_id": self.folder_record(rel)["id"],
                "notebook_count": len(notebooks),
            },
        )
        self.record_tombstones(
            [{"path": path, "kind": "notebook"} for path in notebooks]
            + [
                {"path": record["path"], "kind": "folder", "id": record["id"]}
                for record in records
            ],
            seq,
        )
        return item

    def restore_folder(self, item_id: str, item: dict) -> dict:
        """Put a trashed folder back where it was, or beside it as "<name> 2"
        when something took the name since. Devices threw their copies away,
        so every page, manifest and folder record inside is re-stamped."""
        original = self.folder_path(str(item.get("original_path") or ""))
        if original == PAPER_DIR:
            raise NoteError(400, "Invalid folder path")
        parent = self.canonical_folder(parent_of(original))
        target = f"{parent}/" + self.free_entry_name(parent, original.rpartition("/")[2])
        seq = self.next_seq()
        (self.dir / parent).mkdir(parents=True, exist_ok=True)
        self.ensure_ancestor_records(target, seq=seq)
        restored = self.trash.restore(item_id, target=target)
        path = restored["path"]
        self.clear_tombstones_under(path)

        live_ids = {
            record["id"]: record["path"]
            for record in self.folder_records()
            if not is_under(record["path"], path)
        }
        for notebook in self.notebooks_under(path):
            self._restamp_pages(notebook)
            self._restamp_manifest(notebook)
        for rel in self.folders_under(path):
            stored = self._stored_folder(rel)
            record = normalize_record(stored, rel)
            if record["id"] in live_ids:
                # Another folder took this id while it was in the Trash (a
                # legacy directory recreated at the same path).
                record["id"] = legacy_folder_id(rel)
                if record["id"] in live_ids:
                    record["id"] = new_folder_id()
            self._write_folder(rel, record, touch=False)
        restored["folder_id"] = self.folder_record(path)["id"]
        return restored

    # -- move records --------------------------------------------------------

    def _moves_file(self) -> Path:
        return self._internal() / "moves.json"

    def folder_moves(self) -> list[dict]:
        value = read_json(self._moves_file(), [], skip_corrupt=True)
        return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []

    def record_move(self, folder_id: str, src: str, dst: str, seq: int) -> dict:
        entry = {"id": folder_id, "from": src, "to": dst, "seq": seq, "at": iso_utc()}
        atomic_json(self._moves_file(), [*self.folder_moves(), entry])
        return entry

    def moves_since(self, since: int) -> list[dict]:
        """Moves a cursor at ``since`` has not seen, oldest first. Unlike a
        tombstone, a move is reported even when its ``from`` exists again."""
        rows = [row for row in self.folder_moves() if int(row.get("seq") or 0) > since]
        return sorted(rows, key=lambda row: int(row.get("seq") or 0))

    def folder_changes(self, since: int, full: bool, wanted: set[str] | None = None) -> dict:
        """The feed's ``folders`` and ``folder_moves``. ``wanted`` is the
        index's selection of changed paths; without it the seq decides."""
        records = self.folder_records()
        if full:
            return {"folders": records, "folder_moves": []}
        if wanted is None:
            changed = [record for record in records if record["seq"] > since]
        else:
            changed = [record for record in records if record["path"] in wanted]
        return {"folders": changed, "folder_moves": self.moves_since(since)}
