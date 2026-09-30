"""Note CRUD against the vault working tree. All paths are vault-relative."""
import errno
import logging
import os
import re
import stat
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import fm, names

log = logging.getLogger(__name__)


class NoteError(Exception):
    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(detail)


# Called with each note path a create, move or restore is about to bring into
# being. OMNIA's tool runner sets it for the length of a call, so a tool that
# computes where it writes (a folder move, a note named after a heading)
# cannot put a note where the app reads instructions from. It raises NoteError
# to refuse. A ContextVar, so it binds only that task and the tasks it starts;
# unset, nothing is checked.
LANDING_GUARD: ContextVar[Callable[[str], None] | None] = ContextVar(
    "note_landing_guard", default=None
)


def check_landing(rel_path: str) -> None:
    guard = LANDING_GUARD.get()
    if guard is not None:
        guard(rel_path)


def atomic_write_text(path: Path, text: str) -> None:
    """Replace `path` with `text` so a reader sees the old file or the new one,
    never a partial one.

    Path.write_text truncates before it writes: ENOSPC or a kill in between
    left an empty note, and the autocommit thread's `git add -A` could record
    the truncated blob. The temp name is unique per call, so two writers of
    one file never share it; the leading dot keeps it out of every *.md
    listing and the `.tmp` suffix out of vault commits.
    """
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:12]}.tmp")
    # mkstemp's exclusive create without its 0600 mode: a new file gets the
    # umask's permissions, as write_text gave it.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        try:
            # A rewrite in place kept the file's mode; the replacement is a
            # new inode, so carry it over.
            os.chmod(tmp, stat.S_IMODE(path.stat().st_mode))
        except FileNotFoundError:
            pass
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _read_text(p: Path, rel_path: str) -> str:
    """The one decode every note read goes through.

    utf-8-sig drops the BOM a Windows editor leaves: fm.parse needs `---` at
    position 0, so a BOM hid the whole frontmatter and the first save buried
    it in the body under a fresh block. Writes are plain UTF-8, so the BOM is
    never written back. read_text keeps the universal-newline reading notes
    have always had. Bytes that are not UTF-8 are a 422 naming the file, not
    a 500.
    """
    try:
        return p.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise NoteError(422, f"{rel_path} is not UTF-8 text") from exc


def _utf8_path(rel_path: str) -> bool:
    """False for a name Linux os.walk surrogate-escaped (non-UTF-8 bytes,
    e.g. a Latin-1 tree restored by hand): sqlite and JSON both refuse it."""
    try:
        rel_path.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


@contextmanager
def _name_os_errors(rel_path: str):
    """ENAMETOOLONG (a name or path past the filesystem's limit) and EINVAL (a
    name it cannot store) are the request's fault: a 400 naming the path
    instead of a bare 500."""
    try:
        yield
    except OSError as exc:
        if exc.errno == errno.ENAMETOOLONG:
            raise NoteError(400, f"Path is too long: {rel_path}") from exc
        if exc.errno == errno.EINVAL:
            raise NoteError(400, f"Invalid name: {rel_path}") from exc
        raise


def _invalid_frontmatter(exc: fm.FrontmatterError) -> NoteError:
    return NoteError(422, f"Frontmatter is not valid YAML: {exc}")


def _parse_for_rewrite(content: str) -> tuple[dict, str]:
    """fm.parse for paths that serialize the block back to disk.

    A lenient parse there would bury the user's unloadable block in the body
    under a fresh one, so refuse with a 422 the client can show; the editor
    keeps the text and the user repairs it in Raw mode.
    """
    try:
        return fm.parse(content, strict=True)
    except fm.FrontmatterError as exc:
        raise _invalid_frontmatter(exc) from exc


# C0 controls and DEL. A NUL makes Path.resolve() raise (a 500 rather than a
# 400), and none of them belong in a name the app mints.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def resolve_under(root: Path, rel_path: str, detail: str) -> Path:
    """``(root / rel_path).resolve()``, with bad input as NoteError(400).

    A control character is refused unless that entry already exists: the app
    never creates such a name, but a file that arrived carrying one stays
    reachable — to read, rename or delete — and the boot index sync, which
    resolves every listed note, cannot trip over it. The scan is one regex
    pass; the extra lstat only runs when it finds something.
    """
    candidate = root / rel_path
    if _CONTROL_CHARS.search(rel_path) and not os.path.lexists(candidate):
        raise NoteError(400, detail)
    try:
        return candidate.resolve()
    except (OSError, ValueError):
        raise NoteError(400, detail) from None


@dataclass
class NoteInfo:
    path: str
    name: str


def safe_stem(name: str) -> str:
    """Keep a readable hint of the original filename without trusting it.

    Shared by every content-hashed writer — attachments and Paper assets — so
    a file dropped in from either side is named the same way.
    """
    stem = Path(name or "").stem
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-._")
    return (cleaned[:60] or "file").lower()


class NoteStore:
    INTERNAL_DIR = ".exscalia"
    # These app-managed files intentionally use semantic titles or template
    # placeholders instead of mirroring their filenames. media/ is exempt so a
    # display title like "Elon Musk: A Biography" survives its Windows-safe
    # filename ("Elon Musk A Biography.md").
    # paper/ holds Apple Pencil notebook packages, whose titles are verbatim
    # user text rather than the Windows-safe slug in the directory name.
    TITLE_EXEMPT_ROOTS = {"omnia", "templates", "media", "paper"}

    def __init__(self, vault_dir: Path):
        self.dir = vault_dir
        self._unlistable: set[str] = set()

    # -- path handling -------------------------------------------------------

    def resolve(self, rel_path: str) -> Path:
        """Validate a vault-relative markdown path and return its absolute Path."""
        if not rel_path.endswith(".md"):
            raise NoteError(400, "Note paths must end in .md")
        p = Path(rel_path)
        if p.is_absolute() or any(
            part in ("..", ".git", self.INTERNAL_DIR) for part in p.parts
        ):
            raise NoteError(400, "Invalid note path")
        self._refuse_paper_root(p)
        abs_path = resolve_under(self.dir, rel_path, "Invalid note path")
        if not abs_path.is_relative_to(self.dir.resolve()):
            raise NoteError(400, "Invalid note path")
        return abs_path

    def _refuse_paper_root(self, p: Path) -> None:
        """The root paper/ belongs to PaperStore and is never listed, so a note
        or folder written there would exist on disk but vanish from every
        listing. Only the exact root segment is reserved (as in PaperStore); a
        user folder like School/paper/ is ordinary notes."""
        if p.parts and p.parts[0] == self.PAPER_DIR:
            raise NoteError(400, "paper/ holds Paper notebooks")

    # Binary attachments live in their own vault folder. They are versioned and
    # backed up like notes, but are never listed as notes or folders.
    ATTACHMENT_DIR = "attachments"
    # Paper notebooks are package directories of JSON and binary blobs. Like
    # attachments they are versioned and backed up, and like attachments they
    # are never a note or a folder as far as the note tree is concerned.
    PAPER_DIR = "paper"
    ATTACHMENT_EXTENSIONS = {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".avif",
        ".svg",
        ".pdf",
        ".docx",
    }

    def resolve_attachment(self, rel_path: str) -> Path:
        """Validate a vault-relative attachment path. Deliberately separate from
        resolve() so notes keep their strict markdown-only rule."""
        p = Path(rel_path)
        if p.is_absolute() or any(
            part in ("..", ".git", self.INTERNAL_DIR) for part in p.parts
        ):
            raise NoteError(400, "Invalid attachment path")
        if not p.parts or p.parts[0] != self.ATTACHMENT_DIR:
            raise NoteError(400, "Attachments must live under attachments/")
        if p.suffix.lower() not in self.ATTACHMENT_EXTENSIONS:
            raise NoteError(400, f"Unsupported attachment type: {p.suffix or 'none'}")
        abs_path = resolve_under(self.dir, rel_path, "Invalid attachment path")
        if not abs_path.is_relative_to((self.dir / self.ATTACHMENT_DIR).resolve()):
            raise NoteError(400, "Invalid attachment path")
        return abs_path

    @staticmethod
    def title_from_path(rel_path: str) -> str:
        return Path(rel_path).stem

    @classmethod
    def title_follows_path(cls, rel_path: str) -> bool:
        parts = Path(rel_path).parts
        return bool(parts) and parts[0] not in cls.TITLE_EXEMPT_ROOTS

    def align_title(self, rel_path: str, content: str) -> str:
        """Keep ordinary note frontmatter titles identical to the filename."""
        if not self.title_follows_path(rel_path):
            return content
        meta, body = _parse_for_rewrite(content)
        expected = self.title_from_path(rel_path)
        if meta.get("title") == expected:
            return content
        meta["title"] = expected
        return fm.serialize(meta, body)

    def prepare_content(
        self,
        rel_path: str,
        content: str,
        *,
        existing_created: str | None,
    ) -> str:
        try:
            normalized = fm.prepare_for_save(
                content,
                existing_created=existing_created,
                default_title=self.title_from_path(rel_path),
            )
        except fm.FrontmatterError as exc:
            raise _invalid_frontmatter(exc) from exc
        return self.align_title(rel_path, normalized)

    # -- queries -------------------------------------------------------------

    def _walk(
        self, skip_everywhere: set[str], skip_root: set[str] | frozenset[str] = frozenset()
    ):
        """Directory walk that prunes excluded trees instead of filtering them.

        rglob descended into `.git` and the internal directory in full and then
        discarded the results — on a 13-note vault that meant walking 449
        entries, 247 of them git objects, on every single listing.

        `skip_root` names are pruned only directly under the vault root: they
        are app-managed roots (paper/), and a user folder of the same name
        deeper down (School/paper/) holds ordinary notes.
        """
        for root, dirnames, filenames in os.walk(self.dir):
            root = Path(root)
            skip = (skip_everywhere | skip_root) if root == self.dir else skip_everywhere
            dirnames[:] = sorted(d for d in dirnames if d not in skip)
            filenames.sort()
            yield root, dirnames, filenames

    def _listable(self, rel_path: str) -> bool:
        if _utf8_path(rel_path):
            return True
        # Warned once per name: the sidebar lists on every change.
        if rel_path not in self._unlistable:
            self._unlistable.add(rel_path)
            # %r escapes the surrogates, which a log stream cannot encode.
            log.warning("skipping %r: its name is not UTF-8", rel_path)
        return False

    def list_notes(self) -> list[NoteInfo]:
        notes = []
        for root, _dirs, filenames in self._walk(
            {".git", self.INTERNAL_DIR}, {self.PAPER_DIR}
        ):
            for name in filenames:
                if not name.endswith(".md"):
                    continue
                rel = str((root / name).relative_to(self.dir))
                if not self._listable(rel):
                    continue
                notes.append(NoteInfo(path=rel, name=Path(name).stem))
        notes.sort(key=lambda note: note.path)
        return notes

    def list_folders(self) -> list[str]:
        """All folders in the vault (including empty ones), vault-relative."""
        folders = []
        skip = {".git", self.INTERNAL_DIR, self.ATTACHMENT_DIR}
        for root, dirnames, _files in self._walk(skip, {self.PAPER_DIR}):
            for name in dirnames:
                rel = str((root / name).relative_to(self.dir))
                if self._listable(rel):
                    folders.append(rel)
        folders.sort()
        return folders

    def normalize_folder(self, rel_path: str) -> str:
        """The canonical spelling of a folder path ('Work/', './Work' and
        'Work' are one folder). Every folder operation goes through it, so a
        stray slash can neither dodge a guard nor skew a moved note's path."""
        try:
            rel = names.normalize_folder(rel_path)
        except ValueError as exc:
            raise NoteError(400, str(exc)) from exc
        p = Path(rel)
        if any(part in (".git", self.INTERNAL_DIR) for part in p.parts):
            raise NoteError(400, "Invalid folder path")
        self._refuse_paper_root(p)
        return rel

    def resolve_folder(self, rel_path: str) -> Path:
        rel = self.normalize_folder(rel_path)
        root = self.dir.resolve()
        abs_path = resolve_under(self.dir, rel, "Invalid folder path")
        if abs_path == root or not abs_path.is_relative_to(root):
            raise NoteError(400, "Invalid folder path")
        return abs_path

    def refuse_protected_folder(self, rel: str, verb: str) -> None:
        """The app's own roots (names.PROTECTED_FOLDERS) are never renamed or
        trashed. `rel` is a normalize_folder result."""
        if names.is_protected_folder(rel):
            raise NoteError(400, f"'{rel}' is a built-in folder and cannot be {verb}")

    def create_folder(self, rel_path: str) -> None:
        """Folders are implicit in git, so drop a .gitkeep to make them real."""
        p = self.resolve_folder(rel_path)
        if p.exists() and not p.is_dir():
            raise NoteError(409, f"A file exists at {rel_path}")
        p.mkdir(parents=True, exist_ok=True)
        keep = p / ".gitkeep"
        if not keep.exists():
            keep.write_text("", encoding="utf-8")

    def _contained_notes(self, root: Path) -> list[str]:
        return [str(p.relative_to(self.dir)) for p in sorted(root.rglob("*.md"))]

    def rename_folder(self, old_rel: str, new_rel: str) -> list[tuple[str, str]]:
        """Rename/move a folder. Returns (old, new) pairs for every contained
        note so callers can update the index."""
        old_rel = self.normalize_folder(old_rel)
        new_rel = self.normalize_folder(new_rel)
        self.refuse_protected_folder(old_rel, "renamed")
        src = self.resolve_folder(old_rel)
        dst = self.resolve_folder(new_rel)
        if not src.is_dir():
            raise NoteError(404, f"Folder not found: {old_rel}")
        if dst.exists():
            raise NoteError(409, f"Folder already exists: {new_rel}")
        if dst.is_relative_to(src):
            raise NoteError(400, "Cannot move a folder inside itself")
        moved = [
            (rel, (Path(new_rel) / Path(rel).relative_to(old_rel)).as_posix())
            for rel in self._contained_notes(src)
        ]
        for _, landing in moved:
            check_landing(landing)
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
        return moved

    def exists(self, rel_path: str) -> bool:
        try:
            return self.resolve(rel_path).is_file()
        except NoteError:
            return False

    def read(self, rel_path: str) -> str:
        p = self.resolve(rel_path)
        if not p.is_file():
            raise NoteError(404, f"Note not found: {rel_path}")
        return _read_text(p, rel_path)

    # -- mutations (working tree only; committing is the caller's concern) ---

    def write(self, rel_path: str, content: str, *, preserve_modified: bool = False) -> str:
        """Save note content, normalizing frontmatter. Returns what was written.

        ``preserve_modified`` keeps the on-disk `modified` stamp, for rewrites
        that are bookkeeping rather than edits (a task's column rank), the way
        set_pinned does."""
        p = self.resolve(rel_path)
        if not p.is_file():
            raise NoteError(404, f"Note not found: {rel_path}")
        existing_meta, _ = fm.parse(_read_text(p, rel_path))
        normalized = self.prepare_content(
            rel_path,
            content,
            existing_created=existing_meta.get("created"),
        )
        if preserve_modified and existing_meta.get("modified"):
            meta, body = fm.parse(normalized)
            meta["modified"] = existing_meta["modified"]
            normalized = fm.serialize(meta, body)
        atomic_write_text(p, normalized)
        return normalized

    def write_raw(self, rel_path: str, content: str) -> None:
        """Write `content` exactly as given, creating parent folders.

        For callers whose content is already final (task-metadata passes, an
        approved OMNIA change) and must not be normalized again. They used to
        write the resolved path themselves, which skipped the atomic write.
        """
        p = self.resolve(rel_path)
        if not p.exists():
            check_landing(rel_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(p, content)

    @staticmethod
    def _check_new_name(rel_path: str) -> None:
        # Not Path.stem: for a bare '.md' that is '.md' itself, not ''.
        try:
            names.validate_note_stem(Path(rel_path).name[: -len(".md")])
        except ValueError as exc:
            raise NoteError(400, str(exc)) from exc

    @staticmethod
    def _refuse_sibling(p: Path, rel_path: str, moving: Path | None = None) -> None:
        """409 when the folder already holds this name up to case and Unicode
        form. APFS treats 'Ideas.md' and 'ideas.md', or an NFC and an NFD
        'Café.md', as one file; ext4 keeps both side by side, looking
        identical, with links split between them. `moving` is the note being
        renamed: a case-only rename of it is not a collision."""
        key = names.identity_key(p.name)
        try:
            siblings = os.listdir(p.parent)
        except (FileNotFoundError, NotADirectoryError):
            return
        for name in siblings:
            if names.identity_key(name) != key:
                continue
            if moving is not None and os.path.samefile(p.parent / name, moving):
                continue
            raise NoteError(409, f"Note already exists: {Path(rel_path).with_name(name)}")

    def create(
        self, rel_path: str, content: str | None = None, *, check_name: bool = True
    ) -> str:
        """`check_name=False` is for a copy named after an existing note (a
        conflict or restored copy), which must not fail because that note's
        name predates the rules for new ones, and for a media item's path,
        which media_slug has already made safe to store."""
        p = self.resolve(rel_path)
        if check_name:
            self._check_new_name(rel_path)
        check_landing(rel_path)
        with _name_os_errors(rel_path):
            self._refuse_sibling(p, rel_path)
            if p.exists():
                raise NoteError(409, f"Note already exists: {rel_path}")
            p.parent.mkdir(parents=True, exist_ok=True)
            if content is None:
                content = fm.build_new_note(self.title_from_path(rel_path))
            else:
                content = self.prepare_content(rel_path, content, existing_created=None)
            content = self.align_title(rel_path, content)
            atomic_write_text(p, content)
        return content

    def rename(self, old_rel: str, new_rel: str) -> None:
        src = self.resolve(old_rel)
        dst = self.resolve(new_rel)
        if not src.is_file():
            raise NoteError(404, f"Note not found: {old_rel}")
        # A move keeps the name it already has; only a new name is checked.
        if Path(new_rel).name != Path(old_rel).name:
            self._check_new_name(new_rel)
        if new_rel != old_rel:
            check_landing(new_rel)
        # Renaming a note onto its own path stays a conflict, as it always was.
        moving = src if new_rel != old_rel else None
        with _name_os_errors(new_rel):
            self._refuse_sibling(dst, new_rel, moving)
            if dst.exists() and not (moving and dst.samefile(moving)):
                raise NoteError(409, f"Note already exists: {new_rel}")
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dst)
        try:
            content = _read_text(dst, new_rel)
            aligned = self.align_title(new_rel, content)
        except NoteError:
            # Unloadable frontmatter, or bytes that are not UTF-8. The move
            # has happened and must stand; the title is realigned by the first
            # save after the file is fixed.
            return
        if aligned != content:
            # A filename change is also a title change, so it should receive a
            # normal modified timestamp.
            self.write(new_rel, aligned)

    def set_title(self, rel_path: str, title: str) -> str:
        """Update the visible title while preserving the note body and metadata."""
        expected = self.title_from_path(rel_path)
        if self.title_follows_path(rel_path) and title != expected:
            raise NoteError(
                400,
                f'Note title must match its filename: "{expected}"',
            )
        p = self.resolve(rel_path)
        if not p.is_file():
            raise NoteError(404, f"Note not found: {rel_path}")
        content = _read_text(p, rel_path)
        meta, body = _parse_for_rewrite(content)
        meta["title"] = title
        return self.write(rel_path, fm.serialize(meta, body))

    def delete(self, rel_path: str) -> None:
        p = self.resolve(rel_path)
        if not p.is_file():
            raise NoteError(404, f"Note not found: {rel_path}")
        p.unlink()

    def set_pinned(self, rel_path: str, pinned: bool) -> str:
        """Toggle the frontmatter `pinned` flag without touching `modified`
        (pinning is bookkeeping, not an edit)."""
        p = self.resolve(rel_path)
        if not p.is_file():
            raise NoteError(404, f"Note not found: {rel_path}")
        meta, body = _parse_for_rewrite(_read_text(p, rel_path))
        if pinned:
            meta["pinned"] = True
        else:
            meta.pop("pinned", None)
        if self.title_follows_path(rel_path):
            meta["title"] = self.title_from_path(rel_path)
        content = fm.serialize(meta, body)
        atomic_write_text(p, content)
        return content
