"""Note CRUD against the vault working tree. All paths are vault-relative."""
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import fm


class NoteError(Exception):
    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(detail)


@dataclass
class NoteInfo:
    path: str
    name: str


class NoteStore:
    INTERNAL_DIR = ".exscalia"
    # These app-managed files intentionally use semantic titles or template
    # placeholders instead of mirroring their filenames. media/ is exempt so a
    # display title like "Elon Musk: A Biography" survives its Windows-safe
    # filename ("Elon Musk A Biography.md").
    TITLE_EXEMPT_ROOTS = {"omnia", "templates", "media"}

    def __init__(self, vault_dir: Path):
        self.dir = vault_dir

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
        abs_path = (self.dir / p).resolve()
        if not abs_path.is_relative_to(self.dir.resolve()):
            raise NoteError(400, "Invalid note path")
        return abs_path

    # Binary attachments live in their own vault folder. They are versioned and
    # backed up like notes, but are never listed as notes or folders.
    ATTACHMENT_DIR = "attachments"
    ATTACHMENT_EXTENSIONS = {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".avif",
        ".svg",
        ".pdf",
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
        abs_path = (self.dir / p).resolve()
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
        meta, body = fm.parse(content)
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
        normalized = fm.prepare_for_save(
            content,
            existing_created=existing_created,
            default_title=self.title_from_path(rel_path),
        )
        return self.align_title(rel_path, normalized)

    # -- queries -------------------------------------------------------------

    def _walk(self, skip: set[str]):
        """Directory walk that prunes excluded trees instead of filtering them.

        rglob descended into `.git` and the internal directory in full and then
        discarded the results — on a 13-note vault that meant walking 449
        entries, 247 of them git objects, on every single listing.
        """
        for root, dirnames, filenames in os.walk(self.dir):
            dirnames[:] = sorted(d for d in dirnames if d not in skip)
            filenames.sort()
            yield Path(root), dirnames, filenames

    def list_notes(self) -> list[NoteInfo]:
        notes = []
        for root, _dirs, filenames in self._walk({".git", self.INTERNAL_DIR}):
            for name in filenames:
                if not name.endswith(".md"):
                    continue
                rel = (root / name).relative_to(self.dir)
                notes.append(NoteInfo(path=str(rel), name=Path(name).stem))
        notes.sort(key=lambda note: note.path)
        return notes

    def list_folders(self) -> list[str]:
        """All folders in the vault (including empty ones), vault-relative."""
        folders = []
        skip = {".git", self.INTERNAL_DIR, self.ATTACHMENT_DIR}
        for root, dirnames, _files in self._walk(skip):
            for name in dirnames:
                folders.append(str((root / name).relative_to(self.dir)))
        folders.sort()
        return folders

    def resolve_folder(self, rel_path: str) -> Path:
        p = Path(rel_path)
        if not rel_path or p.is_absolute() or any(
            part in ("..", ".git", self.INTERNAL_DIR) for part in p.parts
        ):
            raise NoteError(400, "Invalid folder path")
        abs_path = (self.dir / p).resolve()
        if not abs_path.is_relative_to(self.dir.resolve()):
            raise NoteError(400, "Invalid folder path")
        return abs_path

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
        src = self.resolve_folder(old_rel)
        dst = self.resolve_folder(new_rel)
        if not src.is_dir():
            raise NoteError(404, f"Folder not found: {old_rel}")
        if dst.exists():
            raise NoteError(409, f"Folder already exists: {new_rel}")
        if dst.is_relative_to(src):
            raise NoteError(400, "Cannot move a folder inside itself")
        moved = [
            (rel, f"{new_rel}/{rel[len(old_rel) + 1 :]}")
            for rel in self._contained_notes(src)
        ]
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
        return moved

    def delete_folder(self, rel_path: str) -> list[str]:
        """Delete a folder and everything in it. Returns the contained note
        paths so callers can update the index."""
        p = self.resolve_folder(rel_path)
        if not p.is_dir():
            raise NoteError(404, f"Folder not found: {rel_path}")
        deleted = self._contained_notes(p)
        shutil.rmtree(p)
        return deleted

    def exists(self, rel_path: str) -> bool:
        try:
            return self.resolve(rel_path).is_file()
        except NoteError:
            return False

    def read(self, rel_path: str) -> str:
        p = self.resolve(rel_path)
        if not p.is_file():
            raise NoteError(404, f"Note not found: {rel_path}")
        return p.read_text(encoding="utf-8")

    # -- mutations (working tree only; committing is the caller's concern) ---

    def write(self, rel_path: str, content: str) -> str:
        """Save note content, normalizing frontmatter. Returns what was written."""
        p = self.resolve(rel_path)
        if not p.is_file():
            raise NoteError(404, f"Note not found: {rel_path}")
        existing_meta, _ = fm.parse(p.read_text(encoding="utf-8"))
        normalized = self.prepare_content(
            rel_path,
            content,
            existing_created=existing_meta.get("created"),
        )
        p.write_text(normalized, encoding="utf-8")
        return normalized

    def create(self, rel_path: str, content: str | None = None) -> str:
        p = self.resolve(rel_path)
        if p.exists():
            raise NoteError(409, f"Note already exists: {rel_path}")
        p.parent.mkdir(parents=True, exist_ok=True)
        if content is None:
            content = fm.build_new_note(self.title_from_path(rel_path))
        else:
            content = self.prepare_content(rel_path, content, existing_created=None)
        content = self.align_title(rel_path, content)
        p.write_text(content, encoding="utf-8")
        return content

    def rename(self, old_rel: str, new_rel: str) -> None:
        src = self.resolve(old_rel)
        dst = self.resolve(new_rel)
        if not src.is_file():
            raise NoteError(404, f"Note not found: {old_rel}")
        if dst.exists():
            raise NoteError(409, f"Note already exists: {new_rel}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
        content = dst.read_text(encoding="utf-8")
        aligned = self.align_title(new_rel, content)
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
        content = p.read_text(encoding="utf-8")
        meta, body = fm.parse(content)
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
        meta, body = fm.parse(p.read_text(encoding="utf-8"))
        if pinned:
            meta["pinned"] = True
        else:
            meta.pop("pinned", None)
        if self.title_follows_path(rel_path):
            meta["title"] = self.title_from_path(rel_path)
        content = fm.serialize(meta, body)
        p.write_text(content, encoding="utf-8")
        return content
