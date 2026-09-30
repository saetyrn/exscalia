"""Paper notebooks: Apple Pencil pages kept in the vault as package folders.

A notebook is a directory ``paper/<Folder>/…/<Title>.paper`` holding a small
``notebook.json`` manifest and one directory per page. The page directory is
the unit everything else keys on — index rows, OMNIA arguments, the change
feed, LiveBus events all name ``paper/Work/Ideas.paper/pages/<id>``.

Source and derived data are deliberately separated. ``drawing.pkdrawing`` is
the ink and it is sacred: only a write that carries the page's current
``revision`` may replace it, and a stale write is never merged or dropped —
it is parked as a conflict sibling next to the original so both halves of a
two-device edit survive. ``render.png`` and ``text.json`` are rebuildable, so
they never bump the revision; they record which revision they were made for
and are accepted (flagged stale) even when the page has moved on.

Nothing here talks to git, the index or the network. The API layer owns the
autocommit touch, the LiveBus event and the index hooks, so the store stays a
plain filesystem object that tests can drive directly.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .media_items import media_slug
from .vault.internal import (
    TrashStore,
    atomic_bytes,
    atomic_json,
    iso_utc,
    parse_utc,
    read_json,
    utc_now,
)
from .vault.notes import NoteError, NoteStore, safe_stem

PAPER_DIR = "paper"
PACKAGE_SUFFIX = ".paper"

# Size caps for the multipart parts. Module constants so a test can shrink
# them instead of building a 32 MiB body.
MAX_DRAWING_BYTES = 32 * 1024 * 1024
MAX_RENDER_BYTES = 8 * 1024 * 1024
MAX_TEXT_BYTES = 2 * 1024 * 1024
MAX_ASSET_BYTES = 50 * 1024 * 1024

# Deletes have to stay visible to a client that was offline; 60 days is twice
# the Trash retention, so nothing is purged before the feed stops mentioning it.
TOMBSTONE_DAYS = 60

TEMPLATES = ("blank", "ruled", "grid", "dotted")
DEFAULT_TEMPLATE = "ruled"

# Points, the unit PencilKit and PDFKit both work in.
# Keys are the iPad app's `PaperSize` raw values; input is matched
# case-insensitively so "A4"/"letter"/"ipad" all land on the canonical name.
PAPER_SIZES = {
    "a4": (595, 842),
    "letter": (612, 792),
    "iPad": (1024, 1366),
    # An infinite page starts at the iPad extent and grows on the device; the
    # current extent rides in `size_points`, so this is only the seed.
    "infinite": (1024, 1366),
}
_PAPER_SIZE_BY_FOLD = {name.casefold(): name for name in PAPER_SIZES}
DEFAULT_PAPER_SIZE = "a4"

BLOB_NAMES = ("drawing.pkdrawing", "render.png", "text.json")

# page.json keys holding the hash of each derived blob's bytes. A render or
# transcript can be rewritten at an unchanged revision (a correction, OMNIA's
# reading), so only the hash says which bytes a URL is for.
DERIVED_ETAG_KEYS = {"render.png": "render_etag", "text.json": "text_etag"}
ETAG_LENGTH = 12

ASSET_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".pdf", ".heic"}

# Client-generated page ids are UUID hex; a conflict sibling is the original id
# with a timestamped suffix. Anything else is rejected before it reaches a path.
_PAGE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}(\.conflict-\d{14})?$")

# Only these page fields come from the client. id/revision/seq and the derived
# revisions are server-owned, so a client cannot fake its way past a conflict.
# `size_points` is the iPad's optional exact page size (imported PDF pages,
# grown infinite pages); `size` keeps the dimensions so an older client and the
# web viewer still render sensibly; `size_name` is the preset the page is
# (`infinite` cannot be told from `iPad` by its dimensions).
PAGE_PATCH_FIELDS = (
    "title",
    "bookmarked",
    "template",
    "size",
    "size_name",
    "size_points",
    "objects",
    "pdf",
)
NOTEBOOK_PATCH_FIELDS = (
    "title",
    "cover",
    "tags",
    "favorite",
    "template",
    "paperSize",
    "pages",
)


class PaperConflict(NoteError):
    """A source write that lost the race. Both versions exist on disk."""

    def __init__(self, detail: str, original: dict, sibling: dict):
        super().__init__(409, detail)
        self.conflict = {"original": original, "sibling": sibling}


def page_url(
    page_dir: str, name: str, revision: int | None, etag: str | None = None
) -> str:
    """Blob URL for a page directory path (``paper/…/pages/<id>``).

    ``etag`` is the hash of the blob's bytes; only a URL carrying it may be
    cached forever, since the revision alone does not pin a derived blob.

    The path is percent-encoded: notebook directories keep spaces and
    parentheses (the iPad's first one is ``Notebook 1.paper``), and a markdown
    image link to a raw path with a space renders as literal text.
    """
    notebook, _, page_id = page_dir.rpartition("/pages/")
    query = [f"r={revision}"] if revision else []
    if etag:
        query.append(f"v={etag}")
    suffix = f"?{'&'.join(query)}" if query else ""
    return (
        f"/api/paper/blob/{quote(notebook, safe='/')}/{quote(page_id, safe='')}"
        f"/{name}{suffix}"
    )


def content_etag(data: bytes) -> str:
    """Names a blob's bytes: it changes exactly when they do."""
    return hashlib.sha256(data).hexdigest()[:ETAG_LENGTH]


def file_etag(path: Path) -> str:
    """``content_etag`` of a file on disk, streamed rather than read whole."""
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()[:ETAG_LENGTH]


def _text_bytes(value: dict) -> bytes:
    """text.json as ``atomic_json`` would write it. Serialised here so the
    hash stamped in page.json is the hash of the file."""
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def paper_page_url(notebook_id: str, page_id: str, region: str | None = None) -> str:
    """Stable web link for a page, independent of notebook renames."""
    from urllib.parse import quote

    url = f"/paper/id/{quote(str(notebook_id), safe='')}/{quote(str(page_id), safe='')}"
    return f"{url}?region={quote(region, safe=',.-_')}" if region else url


class PaperStore:
    """CRUD for notebooks, pages and assets under ``paper/``."""

    def __init__(self, vault_dir: Path, trash: TrashStore):
        self.dir = vault_dir
        self.trash = trash
        self._seq_lock = threading.Lock()

    # -- path handling -------------------------------------------------------

    @property
    def root(self) -> Path:
        return self.dir / PAPER_DIR

    def _internal(self) -> Path:
        return self.dir / NoteStore.INTERNAL_DIR / "paper"

    def notebook_path(self, rel_path: str) -> str:
        """Normalize a notebook reference to its vault-relative path.

        Accepts it with or without the ``paper/`` prefix, the same courtesy
        ``/api/attachments`` extends, because the iPad client stores the full
        vault path while a hand-written URL usually omits the root.
        """
        rel = (rel_path or "").strip().strip("/")
        if not rel:
            raise NoteError(400, "Notebook path is required")
        if not rel.startswith(f"{PAPER_DIR}/") and rel != PAPER_DIR:
            rel = f"{PAPER_DIR}/{rel}"
        parts = Path(rel).parts
        if len(parts) < 2:
            raise NoteError(400, "Invalid notebook path")
        if any(part in ("..", ".", ".git", NoteStore.INTERNAL_DIR) for part in parts):
            raise NoteError(400, "Invalid notebook path")
        if not parts[-1].endswith(PACKAGE_SUFFIX) or parts[-1] == PACKAGE_SUFFIX:
            raise NoteError(400, "Notebooks must end in .paper")
        # A notebook inside a notebook would make the page directory ambiguous.
        if any(part.endswith(PACKAGE_SUFFIX) for part in parts[1:-1]):
            raise NoteError(400, "Notebooks cannot be nested")
        return "/".join(parts)

    def resolve_notebook(self, rel_path: str) -> Path:
        rel = self.notebook_path(rel_path)
        abs_path = (self.dir / rel).resolve()
        if not abs_path.is_relative_to(self.root.resolve()):
            raise NoteError(400, "Invalid notebook path")
        return abs_path

    def find_notebook_by_id(self, notebook_id: str) -> tuple[str, dict]:
        """Resolve a stable manifest id to its current path and manifest."""
        wanted = str(notebook_id or "").strip()
        if not wanted or "/" in wanted or wanted in (".", ".."):
            raise NoteError(400, "Invalid notebook id")
        for rel in self.notebook_dirs():
            manifest = read_json(
                self.dir / rel / "notebook.json", None, skip_corrupt=True
            )
            if isinstance(manifest, dict) and str(manifest.get("id") or "") == wanted:
                return rel, manifest
        raise NoteError(404, f"Notebook not found: {wanted}")

    def resolve_page_by_ids(
        self, notebook_id: str, page_id: str
    ) -> tuple[str, dict, dict]:
        """Resolve stable notebook and page ids after any notebook rename."""
        rel, manifest = self.find_notebook_by_id(notebook_id)
        page_id = self.check_page_id(page_id)
        if page_id not in [p for p in manifest.get("pages") or [] if isinstance(p, str)]:
            raise NoteError(404, f"Page not found: {page_id}")
        return rel, manifest, self.read_page(rel, page_id)

    @staticmethod
    def check_page_id(page_id: str) -> str:
        if not page_id or not _PAGE_ID.match(page_id):
            raise NoteError(400, f"Invalid page id: {page_id or 'empty'}")
        return page_id

    def page_dir_path(self, notebook: str, page_id: str) -> str:
        return f"{self.notebook_path(notebook)}/pages/{self.check_page_id(page_id)}"

    def resolve_page(self, notebook: str, page_id: str) -> Path:
        return self.resolve_notebook(notebook) / "pages" / self.check_page_id(page_id)

    def folder_path(self, rel_path: str | None) -> str:
        """Normalize an optional library folder ('' / 'Work' / 'paper/Work')."""
        rel = (rel_path or "").strip().strip("/")
        if not rel or rel == PAPER_DIR:
            return PAPER_DIR
        if not rel.startswith(f"{PAPER_DIR}/"):
            rel = f"{PAPER_DIR}/{rel}"
        parts = Path(rel).parts
        if any(part in ("..", ".", ".git", NoteStore.INTERNAL_DIR) for part in parts):
            raise NoteError(400, "Invalid folder path")
        if any(part.endswith(PACKAGE_SUFFIX) for part in parts):
            raise NoteError(400, "Invalid folder path")
        return "/".join(parts)

    # -- change-feed sequence ------------------------------------------------

    def _seq_file(self) -> Path:
        return self._internal() / "seq.json"

    def seq_state(self) -> dict:
        """``{epoch, seq, pruned}``, created on first use.

        The epoch changes only when the counter is rebuilt from nothing, which
        is how a client learns its cursor means nothing any more.
        """
        with self._seq_lock:
            return self._seq_state_locked()

    def _seq_state_locked(self) -> dict:
        state = read_json(self._seq_file(), None, skip_corrupt=True)
        if not isinstance(state, dict) or not state.get("epoch"):
            state = {"epoch": uuid.uuid4().hex[:12], "seq": 0, "pruned": 0}
            atomic_json(self._seq_file(), state)
        state.setdefault("pruned", 0)
        return state

    def next_seq(self, *, fsync: bool = True) -> int:
        """Monotonic counter stamped into every JSON file the feed reports.

        Under a threading lock rather than an asyncio one: the writes happen
        inside ``asyncio.to_thread`` in places, and a plain counter must not
        depend on which loop is asking.
        """
        with self._seq_lock:
            state = self._seq_state_locked()
            state["seq"] = int(state.get("seq") or 0) + 1
            atomic_json(self._seq_file(), state, fsync=fsync)
            return state["seq"]

    def rotate_epoch(self, floor: int) -> dict:
        """Start a new epoch with the counter at no less than ``floor``.

        For a counter that went backwards under its clients: seq.json rides in
        the vault, so a ``git reset`` or a history restore rewinds ``seq`` while
        the epoch survives, and new writes would land at or below cursors
        already handed out. The new epoch sends every client back for a full
        reconcile; the floor keeps what follows above the old numbers.
        """
        with self._seq_lock:
            state = self._seq_state_locked()
            state["epoch"] = uuid.uuid4().hex[:12]
            state["seq"] = max(int(state.get("seq") or 0), int(floor or 0))
            atomic_json(self._seq_file(), state)
            return dict(state)

    # -- tombstones ----------------------------------------------------------

    def _tombstone_file(self) -> Path:
        return self._internal() / "tombstones.json"

    def tombstones(self) -> list[dict]:
        value = read_json(self._tombstone_file(), [], skip_corrupt=True)
        return value if isinstance(value, list) else []

    def record_tombstone(self, path: str, kind: str, seq: int) -> dict:
        entry = {"path": path, "kind": kind, "seq": seq, "at": iso_utc()}
        rows = [row for row in self.tombstones() if row.get("path") != path]
        rows.append(entry)
        atomic_json(self._tombstone_file(), rows)
        return entry

    def clear_tombstone(self, path: str) -> None:
        """A restore or a recreate must not leave the feed reporting a delete."""
        rows = self.tombstones()
        kept = [row for row in rows if row.get("path") != path]
        if len(kept) != len(rows):
            atomic_json(self._tombstone_file(), kept)

    def tombstones_since(self, since: int) -> list[dict]:
        """The deletes a cursor at ``since`` has not seen, oldest first.

        Both change feeds report exactly this. A path that is back on disk is
        left out whatever its tombstone says: the client applies deletes after
        upserts, so reporting it would drop what it had just received. The
        restores here clear their tombstone; the generic Trash restore does not.
        """
        rows = [
            row
            for row in self.tombstones()
            if int(row.get("seq") or 0) > since
            and not (self.dir / str(row.get("path") or "")).exists()
        ]
        return sorted(rows, key=lambda row: int(row.get("seq") or 0))

    def prune_tombstones(self, days: int = TOMBSTONE_DAYS) -> int:
        """Drop deletes older than the retention window, remembering the
        highest seq that went — a cursor below it can no longer be trusted to
        have seen every delete, so the feed answers ``full``."""
        cutoff = utc_now() - timedelta(days=days)
        rows = self.tombstones()
        kept: list[dict] = []
        pruned_seq = 0
        for row in rows:
            try:
                fresh = parse_utc(str(row.get("at"))) > cutoff
            except (TypeError, ValueError):
                fresh = False
            if fresh:
                kept.append(row)
            else:
                pruned_seq = max(pruned_seq, int(row.get("seq") or 0))
        if len(kept) == len(rows):
            return 0
        atomic_json(self._tombstone_file(), kept)
        with self._seq_lock:
            state = self._seq_state_locked()
            state["pruned"] = max(int(state.get("pruned") or 0), pruned_seq)
            atomic_json(self._seq_file(), state)
        return len(rows) - len(kept)

    # -- library -------------------------------------------------------------

    def notebook_dirs(self) -> list[str]:
        """Vault-relative paths of every notebook package, sorted."""
        found: list[str] = []
        if not self.root.is_dir():
            return found
        stack = [self.root]
        while stack:
            current = stack.pop()
            for child in sorted(current.iterdir()):
                if not child.is_dir():
                    continue
                if child.name.endswith(PACKAGE_SUFFIX):
                    found.append(str(child.relative_to(self.dir)))
                else:
                    stack.append(child)
        return sorted(found)

    def folders(self) -> list[str]:
        """Library folders (paper/ itself excluded), including empty ones."""
        names: list[str] = []
        if not self.root.is_dir():
            return names
        stack = [self.root]
        while stack:
            current = stack.pop()
            for child in sorted(current.iterdir()):
                if child.is_dir() and not child.name.endswith(PACKAGE_SUFFIX):
                    names.append(str(child.relative_to(self.dir)))
                    stack.append(child)
        return sorted(names)

    def summary(self, rel_path: str, manifest: dict | None = None) -> dict:
        """Manifest fields the library grid needs, plus the cover URL.

        Reads at most one extra page.json (the cover's) so the grid can point
        at a render that is safe to cache.
        """
        rel = self.notebook_path(rel_path)
        if manifest is None:
            manifest = self.read_notebook(rel)
        pages = [p for p in manifest.get("pages") or [] if isinstance(p, str)]
        cover = manifest.get("cover") or (pages[0] if pages else None)
        cover_url = None
        if cover:
            page = read_json(
                self.dir / rel / "pages" / cover / "page.json", None, skip_corrupt=True
            )
            if isinstance(page, dict) and page.get("render_revision"):
                cover_url = page_url(
                    f"{rel}/pages/{cover}",
                    "render.png",
                    page.get("render_revision"),
                    self.blob_etag(page, "render.png"),
                )
        folder = str(Path(rel).parent)
        return {
            "path": rel,
            "folder": folder,
            "id": manifest.get("id"),
            "title": manifest.get("title") or Path(rel).stem,
            "created": manifest.get("created"),
            "modified": manifest.get("modified"),
            "template": manifest.get("template"),
            "paperSize": manifest.get("paperSize"),
            "tags": manifest.get("tags") or [],
            "favorite": bool(manifest.get("favorite")),
            "cover": cover,
            "cover_url": cover_url,
            "page_count": len(pages),
            "seq": int(manifest.get("seq") or 0),
        }

    def list_library(self) -> dict:
        """``{folders, notebooks}``. A corrupt manifest costs its own notebook,
        never the whole listing — hence ``skip_corrupt``."""
        notebooks = []
        for rel in self.notebook_dirs():
            manifest = read_json(
                self.dir / rel / "notebook.json", None, skip_corrupt=True
            )
            if isinstance(manifest, dict):
                notebooks.append(self.summary(rel, manifest))
        notebooks.sort(key=lambda nb: str(nb.get("modified") or ""), reverse=True)
        return {"folders": self.folders(), "notebooks": notebooks}

    # -- notebooks -----------------------------------------------------------

    def read_notebook(self, rel_path: str) -> dict:
        rel = self.notebook_path(rel_path)
        manifest = read_json(self.dir / rel / "notebook.json", None)
        if not isinstance(manifest, dict):
            raise NoteError(404, f"Notebook not found: {rel}")
        manifest["path"] = rel
        return manifest

    def _write_notebook(self, rel: str, manifest: dict) -> dict:
        manifest = {k: v for k, v in manifest.items() if k != "path"}
        manifest["modified"] = iso_utc()
        manifest["seq"] = self.next_seq()
        atomic_json(self.dir / rel / "notebook.json", manifest)
        return {**manifest, "path": rel}

    def create_notebook(
        self,
        *,
        title: str,
        folder: str | None = None,
        template: str | None = None,
        paper_size: str | None = None,
        notebook_id: str | None = None,
    ) -> dict:
        title = (title or "").strip() or "Untitled"
        template = self._check_template(template)
        paper_size = self._check_size_name(paper_size)
        parent = self.folder_path(folder)
        base = media_slug(title)
        rel = f"{parent}/{base}{PACKAGE_SUFFIX}"
        number = 1
        while (self.dir / rel).exists():
            number += 1
            rel = f"{parent}/{base} {number}{PACKAGE_SUFFIX}"
        rel = self.notebook_path(rel)
        (self.dir / rel).mkdir(parents=True)
        now = iso_utc()
        manifest = self._write_notebook(
            rel,
            {
                "id": notebook_id or uuid.uuid4().hex,
                "title": title,
                "created": now,
                "modified": now,
                "template": template,
                "paperSize": paper_size,
                "cover": None,
                "tags": [],
                "favorite": False,
                "pages": [],
            },
        )
        self.clear_tombstone(rel)
        return manifest

    def update_notebook(self, rel_path: str, patch: dict) -> dict:
        """Apply the whitelisted manifest fields.

        ``pages`` is reconciled rather than trusted: a client that missed a page
        another device added would otherwise delete it from the notebook with a
        stale ordering.
        """
        rel = self.notebook_path(rel_path)
        manifest = self.read_notebook(rel)
        for key in NOTEBOOK_PATCH_FIELDS:
            if key not in patch:
                continue
            value = patch[key]
            if key == "pages":
                manifest["pages"] = self._reconcile_pages(rel, value)
            elif key == "template":
                manifest["template"] = self._check_template(value)
            elif key == "paperSize":
                manifest["paperSize"] = self._check_size_name(value)
            elif key == "favorite":
                manifest["favorite"] = bool(value)
            elif key == "tags":
                manifest["tags"] = [str(tag) for tag in (value or [])]
            elif key == "cover":
                manifest["cover"] = self.check_page_id(value) if value else None
            elif key == "title":
                manifest["title"] = str(value or "").strip() or manifest["title"]
        return self._write_notebook(rel, manifest)

    def _on_disk_pages(self, rel: str) -> list[str]:
        pages_dir = self.dir / rel / "pages"
        if not pages_dir.is_dir():
            return []
        return sorted(p.name for p in pages_dir.iterdir() if p.is_dir())

    def _reconcile_pages(self, rel: str, order: Any) -> list[str]:
        on_disk = set(self._on_disk_pages(rel))
        requested = [p for p in (order or []) if isinstance(p, str)]
        result = [p for p in requested if p in on_disk]
        seen = set(result)
        # Anything the client left out but that exists keeps its place at the
        # end rather than vanishing.
        for page_id in sorted(on_disk - seen):
            result.append(page_id)
        return result

    def rename_notebook(
        self, rel_path: str, new_path: str, title: str | None = None
    ) -> dict:
        """Rename, and move: the destination carries its own folder.

        The directory is only the slug; the title is the user's verbatim text
        and changes when ``title`` is passed. A move sends none, and a retitle
        whose slug is unchanged stays at the same path. A new name in the same
        folder with no title is a retitle from an iPad build that sent only the
        path; it gets the new name as its title, as it always did, or that
        device's next pull puts the old title back over its own.

        A notebook that moved answers with ``moved_pages``, each page's
        ``{id, seq, previous_seq}`` after the re-stamp: the device that asked
        for the move already has those pages, and a page it held at
        ``previous_seq`` it now holds at ``seq``, so the feed's echo of the
        move costs it no downloads.
        """
        src_rel = self.notebook_path(rel_path)
        dst_rel = self.notebook_path(new_path)
        src = self.dir / src_rel
        dst = self.dir / dst_rel
        title = (title or "").strip() or None
        if not src.is_dir():
            raise NoteError(404, f"Notebook not found: {src_rel}")
        if src_rel == dst_rel:
            manifest = self.read_notebook(src_rel)
            if title is None or manifest.get("title") == title:
                return manifest
            manifest["title"] = title
            return self._write_notebook(src_rel, manifest)
        if dst.exists():
            raise NoteError(409, f"Notebook already exists: {dst_rel}")
        manifest = self.read_notebook(src_rel)
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
        # The pages moved too: a device that knows them only at the old path
        # gets them again at the new one, or it is left an empty notebook.
        moved_pages = self._restamp_pages(dst_rel)
        if title is not None:
            manifest["title"] = title
        elif not str(manifest.get("title") or "").strip() or (
            Path(src_rel).parent == Path(dst_rel).parent
            and media_slug(str(manifest.get("title"))) != Path(dst_rel).stem
        ):
            manifest["title"] = Path(dst_rel).stem
        moved = self._write_notebook(dst_rel, manifest)
        self.record_tombstone(src_rel, "notebook", moved["seq"])
        self.clear_tombstone(dst_rel)
        return {**moved, "moved_pages": moved_pages}

    def delete_notebook(self, rel_path: str) -> dict:
        rel = self.notebook_path(rel_path)
        if not (self.dir / rel).is_dir():
            raise NoteError(404, f"Notebook not found: {rel}")
        manifest = self.trash.trash_paper("notebook", rel)
        self.record_tombstone(rel, "notebook", self.next_seq())
        return manifest

    # -- pages ---------------------------------------------------------------

    def _check_template(self, template: str | None) -> str:
        if template in (None, ""):
            return DEFAULT_TEMPLATE
        if template not in TEMPLATES:
            raise NoteError(400, f"Unknown template: {template}")
        return template

    def _check_size_name(self, name: str | None) -> str:
        if name in (None, ""):
            return DEFAULT_PAPER_SIZE
        canonical = _PAPER_SIZE_BY_FOLD.get(str(name).casefold())
        if canonical is None:
            raise NoteError(400, f"Unknown paper size: {name}")
        return canonical

    def _size_value(self, size: Any, paper_size: str) -> dict:
        if isinstance(size, dict) and size.get("width") and size.get("height"):
            return {"width": float(size["width"]), "height": float(size["height"])}
        if isinstance(size, str):
            paper_size = self._check_size_name(size)
        width, height = PAPER_SIZES[paper_size]
        return {"width": float(width), "height": float(height)}

    def read_page(self, notebook: str, page_id: str) -> dict:
        rel = self.page_dir_path(notebook, page_id)
        page = read_json(self.dir / rel / "page.json", None)
        if not isinstance(page, dict):
            raise NoteError(404, f"Page not found: {rel}")
        page["path"] = rel
        page["notebook"] = self.notebook_path(notebook)
        return page

    def _write_page(self, rel: str, page: dict) -> dict:
        page = {k: v for k, v in page.items() if k not in ("path", "notebook")}
        page["modified"] = iso_utc()
        page["seq"] = self.next_seq()
        atomic_json(self.dir / rel / "page.json", page)
        notebook, _, page_id = rel.rpartition("/pages/")
        return {**page, "path": rel, "notebook": notebook}

    def _restamp_page(self, rel: str, *, fsync: bool = True) -> dict | None:
        """A fresh ``seq`` for a page that came back or moved, so a client
        whose cursor passed the old one sees it. Nothing else changes: the
        revision belongs to the ink and ``modified`` to the last edit (the
        iPad's automatic cover is the latest-modified page), not to the feed.

        Returns ``{id, seq, previous_seq}``, or None for a page with no
        readable page.json."""
        page = read_json(self.dir / rel / "page.json", None, skip_corrupt=True)
        if not isinstance(page, dict):
            return None
        page = {k: v for k, v in page.items() if k not in ("path", "notebook")}
        previous = int(page.get("seq") or 0)
        page["seq"] = self.next_seq(fsync=fsync)
        atomic_json(self.dir / rel / "page.json", page, fsync=fsync)
        return {
            "id": str(page.get("id") or rel.rpartition("/")[2]),
            "seq": page["seq"],
            "previous_seq": previous,
        }

    def _restamp_pages(self, notebook_rel: str) -> list[dict]:
        """Every page of a notebook that moved or came back. Not fsynced one
        by one: this runs on the event loop for the whole notebook, and each
        caller writes the manifest, durably, right after."""
        stamped = []
        for page_id in self._on_disk_pages(notebook_rel):
            row = self._restamp_page(f"{notebook_rel}/pages/{page_id}", fsync=False)
            if row is not None:
                stamped.append(row)
        return stamped

    def _insert_page(self, notebook_rel: str, page_id: str, after: str | None) -> dict:
        manifest = self.read_notebook(notebook_rel)
        pages = [p for p in manifest.get("pages") or [] if isinstance(p, str)]
        if page_id in pages:
            return manifest
        if after and after in pages:
            pages.insert(pages.index(after) + 1, page_id)
        else:
            pages.append(page_id)
        manifest["pages"] = pages
        if not manifest.get("cover"):
            manifest["cover"] = page_id
        return self._write_notebook(notebook_rel, manifest)

    def create_page(
        self,
        notebook: str,
        *,
        page_id: str | None = None,
        after: str | None = None,
        template: str | None = None,
        size: Any = None,
        size_name: str | None = None,
        title: str | None = None,
        bookmarked: bool = False,
        client_id: str | None = None,
    ) -> dict:
        nb_rel = self.notebook_path(notebook)
        manifest = self.read_notebook(nb_rel)
        page_id = self.check_page_id(page_id or uuid.uuid4().hex)
        rel = f"{nb_rel}/pages/{page_id}"
        # The preset the page is: named outright, implied by a string size, or
        # the notebook's own when no size was given at all.
        if size_name in (None, ""):
            if isinstance(size, str):
                size_name = size
            elif size is None:
                size_name = manifest.get("paperSize")
        named = {} if size_name in (None, "") else {
            "size_name": self._check_size_name(size_name)
        }
        if (self.dir / rel).exists():
            raise NoteError(409, f"Page already exists: {rel}")
        (self.dir / rel).mkdir(parents=True)
        now = iso_utc()
        page = self._write_page(
            rel,
            {
                "id": page_id,
                "title": str(title).strip() if title is not None else None,
                "bookmarked": bool(bookmarked),
                "template": self._check_template(template or manifest.get("template")),
                "size": self._size_value(
                    size, self._check_size_name(manifest.get("paperSize"))
                ),
                **named,
                "created": now,
                "modified": now,
                "revision": 1,
                "render_revision": 0,
                "text_revision": 0,
                "objects": [],
                "pdf": None,
                "conflict_of": None,
                # A create is this client's first source write: when its
                # response is lost, the PUT that follows still names base 0,
                # and _own_retry takes it for the next revision, not a fork.
                "last_write": {
                    "client_id": client_id or None,
                    "base_revision": 0,
                    "drawing_sha256": None,
                },
            },
        )
        self.clear_tombstone(rel)
        self._insert_page(nb_rel, page_id, after)
        return page

    def _merge_text(self, existing: Any, incoming: dict, revision: int) -> dict:
        """Lines and transcript come from the writer; an AI transcript survives
        only while it still describes the current ink."""
        previous = existing if isinstance(existing, dict) else {}
        merged = {
            # These are the raw on-device OCR fields. Keep their names stable
            # so older native clients remain wire-compatible.
            "lines": incoming.get("lines") or [],
            "transcript": (
                incoming.get("transcript")
                if incoming.get("transcript") is not None
                else incoming.get("raw_transcript")
            )
            or "",
        }

        def number(value: Any) -> int | None:
            try:
                return int(value) if value is not None else None
            except (TypeError, ValueError):
                return None

        # Corrections are user-authored derived metadata. They are tied to a
        # page revision, so OCR from an older ink version cannot win a later
        # read. A client may send either a materialised corrected transcript or
        # correction spans (or both); explicit null and [] clear them.
        stale = incoming.get("stale_correction")
        if "corrected_transcript" in incoming or "corrections" in incoming:
            corrected = incoming.get("corrected_transcript")
            if corrected is not None:
                merged["corrected_transcript"] = str(corrected)
            corrections = incoming.get("corrections")
            if isinstance(corrections, list) and corrections:
                merged["corrections"] = corrections
            # A correction is filed with the reading it edits (`for_revision`
            # names that reading). One that names no reading, or an older one,
            # keeps the revision it was made for: an older client re-sends a
            # correction of ink that has since changed, and binding it to this
            # revision would hide the new words from every reader.
            claimed = number(incoming.get("corrections_revision"))
            read_for = number(incoming.get("for_revision"))
            correction_revision = (
                revision
                if claimed is None or (read_for is not None and claimed >= read_for)
                else claimed
            )
        else:
            correction_revision = number(previous.get("corrections_revision"))
            if correction_revision is None:
                correction_revision = revision
            earlier = {
                key: previous[key]
                for key, kind in (("corrected_transcript", str), ("corrections", list))
                if isinstance(previous.get(key), kind)
            }
            if correction_revision == revision:
                merged.update(earlier)
            elif stale is None and (
                earlier.get("corrected_transcript") or earlier.get("corrections")
            ):
                # Superseded, not deleted: the ink moved on under it.
                stale = {**earlier, "corrections_revision": correction_revision}
            if stale is None:
                stale = previous.get("stale_correction")
        if merged.get("corrected_transcript") is not None or merged.get("corrections"):
            merged["corrections_revision"] = correction_revision
        if isinstance(stale, dict):
            merged["stale_correction"] = stale
        merged["for_revision"] = revision
        kept = None
        if isinstance(existing, dict):
            kept = existing.get("transcript_ai")
        if isinstance(incoming.get("transcript_ai"), dict):
            kept = incoming["transcript_ai"]
        if isinstance(kept, dict) and int(kept.get("revision") or 0) == revision:
            merged["transcript_ai"] = kept
        return merged

    def _apply_page_patch(self, page: dict, patch: dict, manifest: dict) -> dict:
        for key in PAGE_PATCH_FIELDS:
            if key not in patch:
                continue
            if key == "title":
                value = patch[key]
                page["title"] = str(value).strip() if value is not None else None
            elif key == "bookmarked":
                page["bookmarked"] = bool(patch[key])
            elif key == "template":
                page["template"] = self._check_template(patch[key])
            elif key == "size":
                page["size"] = self._size_value(
                    patch[key], self._check_size_name(manifest.get("paperSize"))
                )
                if isinstance(patch[key], str):
                    page["size_name"] = self._check_size_name(patch[key])
            elif key == "size_name":
                # After `size`, so an explicit name wins over one implied by a
                # string size; null clears it.
                if patch[key] in (None, ""):
                    page.pop("size_name", None)
                else:
                    page["size_name"] = self._check_size_name(patch[key])
            elif key == "size_points":
                page["size_points"] = self._points_value(patch[key])
            else:
                page[key] = patch[key]
        return page

    @staticmethod
    def _points_value(value: Any) -> dict | None:
        """``{width, height}`` in points, or null; anything else is a 400."""
        if value is None:
            return None
        try:
            width, height = float(value["width"]), float(value["height"])
        except (TypeError, KeyError, ValueError) as exc:
            raise NoteError(400, "size_points must be {width, height}") from exc
        if not (0 < width < 20000 and 0 < height < 20000):
            raise NoteError(400, "size_points is out of range")
        return {"width": width, "height": height}

    def save_page(
        self,
        notebook: str,
        page_id: str,
        *,
        base_revision: int,
        page: dict | None = None,
        drawing: bytes | None = None,
        render: bytes | None = None,
        text: dict | None = None,
        for_revision: int | None = None,
        after: str | None = None,
        client_id: str | None = None,
    ) -> dict:
        """The one write path for page content.

        ``page``/``drawing`` are source: they need ``base_revision`` to equal
        the page's current revision (0 meaning "this is a create") and they
        bump it. ``render``/``text`` are derived: they never bump the revision
        and are accepted even when made for an older one, flagged ``stale`` so
        the client knows to redo them.

        Every check runs before the first byte is written, so a refused save
        leaves the page exactly as it was; the blobs land before page.json, so
        the revision never names ink that is not on disk yet.
        """
        nb_rel = self.notebook_path(notebook)
        manifest = self.read_notebook(nb_rel)
        page_id = self.check_page_id(page_id)
        rel = f"{nb_rel}/pages/{page_id}"
        current = read_json(self.dir / rel / "page.json", None, skip_corrupt=True)
        current = current if isinstance(current, dict) else None
        has_source = page is not None or drawing is not None
        base_revision = int(base_revision or 0)
        claimed_base = base_revision
        drawing_sha256 = (
            hashlib.sha256(drawing).hexdigest() if drawing is not None else None
        )

        if has_source:
            if base_revision and current is None:
                raise NoteError(404, f"Page not found: {rel}")
            current_revision = int(current.get("revision") or 0) if current else 0
            if current is not None and (
                base_revision == 0 or base_revision != current_revision
            ):
                retry = self._own_retry(
                    current, client_id, base_revision, drawing_sha256, page, manifest
                )
                if retry == "replay":
                    if render is None and text is None:
                        return {
                            "page": {**current, "path": rel, "notebook": nb_rel},
                            "revision": current_revision,
                            "stale": False,
                            "replayed": True,
                        }
                    # The ink is already on disk; a render or transcript made
                    # since the lost response still lands, as derived data.
                    replayed = self.save_page(
                        nb_rel, page_id, base_revision=current_revision,
                        render=render, text=text, for_revision=for_revision,
                    )
                    return {**replayed, "replayed": True}
                if retry == "continue":
                    # The derived parts were made from this ink, so they move
                    # with it to the revision it is about to become.
                    shift = current_revision - base_revision
                    if for_revision is not None and int(for_revision) > base_revision:
                        for_revision = int(for_revision) + shift
                    base_revision = current_revision
                else:
                    raise self._conflict(
                        nb_rel, page_id, current, page, drawing, render, text,
                        "A page already exists at this id"
                        if base_revision == 0
                        else "The page changed since this edit started",
                    )
        elif current is None:
            raise NoteError(404, f"Page not found: {rel}")

        is_create = current is None
        now = iso_utc()
        if is_create:
            record = {
                "id": page_id,
                "title": None,
                "bookmarked": False,
                "template": self._check_template(manifest.get("template")),
                "size": self._size_value(
                    None, self._check_size_name(manifest.get("paperSize"))
                ),
                "created": now,
                "modified": now,
                "revision": 0,
                "render_revision": 0,
                "text_revision": 0,
                "objects": [],
                "pdf": None,
                "conflict_of": None,
            }
        else:
            record = dict(current)
        record.pop("path", None)
        record.pop("notebook", None)

        if page is not None:
            record = self._apply_page_patch(record, page, manifest)
        if has_source:
            record["revision"] = int(record.get("revision") or 0) + 1
            # What a retry of this very request looks like; see _own_retry.
            record["last_write"] = {
                "client_id": client_id or None,
                "base_revision": claimed_base,
                "drawing_sha256": drawing_sha256,
            }
        revision = int(record["revision"])
        derived_for = int(for_revision) if for_revision is not None else revision
        stale = False

        if drawing is not None:
            self._check_size("drawing", drawing, MAX_DRAWING_BYTES)
        if render is not None:
            self._check_size("render", render, MAX_RENDER_BYTES)
        merged = None
        if text is not None:
            merged = self._merge_text(
                read_json(self.dir / rel / "text.json", None, skip_corrupt=True),
                text,
                derived_for,
            )
            self._check_size(
                "text", json.dumps(merged).encode("utf-8"), MAX_TEXT_BYTES
            )

        if is_create:
            (self.dir / rel).mkdir(parents=True, exist_ok=True)
        if drawing is not None:
            atomic_bytes(self.dir / rel / "drawing.pkdrawing", drawing)
        if render is not None:
            atomic_bytes(self.dir / rel / "render.png", render)
            record["render_revision"] = derived_for
            record["render_etag"] = content_etag(render)
            stale = stale or derived_for < revision
        if merged is not None:
            record["text_etag"] = self._write_text(rel, merged)
            record["text_revision"] = derived_for
            stale = stale or derived_for < revision

        saved = self._write_page(rel, record)
        if is_create:
            self.clear_tombstone(rel)
            self._insert_page(nb_rel, page_id, after)
        return {"page": saved, "revision": revision, "stale": stale}

    def _check_size(self, what: str, data: bytes, cap: int) -> None:
        if len(data) > cap:
            raise NoteError(413, f"Page {what} is larger than {cap // (1024 * 1024)} MB")

    def _own_retry(
        self,
        current: dict,
        client_id: str | None,
        base_revision: int,
        drawing_sha256: str | None,
        page: dict | None,
        manifest: dict,
    ) -> str | None:
        """Whether a source write that missed the current revision is this
        client re-sending a save whose response it never received.

        ``last_write`` is stamped by every source write, so while it still
        names this client and this base nobody else has written since.
        ``"replay"``: the same ink and metadata, already applied.
        ``"continue"``: the device kept writing before the retry went out, so
        this is simply its next revision. ``None``: a real conflict.
        """
        last = current.get("last_write")
        if not client_id or not isinstance(last, dict):
            return None
        if last.get("client_id") != client_id:
            return None
        if int(last.get("base_revision") or 0) != base_revision:
            return None
        same_ink = drawing_sha256 is None or drawing_sha256 == last.get(
            "drawing_sha256"
        )
        patched = self._apply_page_patch(dict(current), page or {}, manifest)
        same_meta = all(patched.get(key) == current.get(key) for key in PAGE_PATCH_FIELDS)
        return "replay" if same_ink and same_meta else "continue"

    def _conflict(
        self,
        nb_rel: str,
        page_id: str,
        current: dict,
        page: dict | None,
        drawing: bytes | None,
        render: bytes | None,
        text: dict | None,
        detail: str,
    ) -> PaperConflict:
        """Park the incoming version beside the original and report both.

        The original's bytes are not touched — the loser of the race gets a new
        page, and the notebook lists it directly after the page it forked from,
        so the two show up next to each other in the scroller.
        """
        base = page_id.split(".conflict-")[0]
        stamp = utc_now().strftime("%Y%m%d%H%M%S")
        sibling_id = f"{base}.conflict-{stamp}"
        counter = 1
        while (self.dir / nb_rel / "pages" / sibling_id).exists():
            counter += 1
            sibling_id = f"{base}.conflict-{stamp[:-1]}{counter % 10}"
        rel = f"{nb_rel}/pages/{sibling_id}"
        manifest = self.read_notebook(nb_rel)
        now = iso_utc()
        record = {
            "id": sibling_id,
            "title": current.get("title"),
            "bookmarked": bool(current.get("bookmarked")),
            "template": current.get("template")
            or self._check_template(manifest.get("template")),
            "size": current.get("size")
            or self._size_value(None, self._check_size_name(manifest.get("paperSize"))),
            "created": now,
            "modified": now,
            "revision": 1,
            "render_revision": 0,
            "text_revision": 0,
            "objects": [],
            "pdf": None,
            "conflict_of": page_id,
        }
        if page is not None:
            record = self._apply_page_patch(record, page, manifest)
            record["conflict_of"] = page_id
        if drawing is not None:
            self._check_size("drawing", drawing, MAX_DRAWING_BYTES)
        if render is not None:
            self._check_size("render", render, MAX_RENDER_BYTES)
        merged = None
        if text is not None:
            merged = self._merge_text(None, text, 1)
            self._check_size(
                "text", json.dumps(merged).encode("utf-8"), MAX_TEXT_BYTES
            )

        (self.dir / rel).mkdir(parents=True, exist_ok=True)
        if drawing is not None:
            atomic_bytes(self.dir / rel / "drawing.pkdrawing", drawing)
        if render is not None:
            atomic_bytes(self.dir / rel / "render.png", render)
            record["render_revision"] = 1
            record["render_etag"] = content_etag(render)
        if merged is not None:
            record["text_etag"] = self._write_text(rel, merged)
            record["text_revision"] = 1
        sibling = self._write_page(rel, record)
        self._insert_page(nb_rel, sibling_id, page_id)
        original = self.read_page(nb_rel, page_id)
        return PaperConflict(detail, original, sibling)

    def read_blob(self, notebook: str, page_id: str, name: str) -> tuple[Path, dict]:
        if name not in BLOB_NAMES:
            raise NoteError(404, f"Unknown page file: {name}")
        page = self.read_page(notebook, page_id)
        path = self.resolve_page(notebook, page_id) / name
        if not path.is_file():
            raise NoteError(404, f"Page file not found: {name}")
        return path, page

    def _write_text(self, rel: str, merged: dict) -> str:
        """Write text.json and return the etag of the bytes written."""
        data = _text_bytes(merged)
        atomic_bytes(self.dir / rel / "text.json", data, fsync=False)
        return content_etag(data)

    def blob_revision(self, page: dict, name: str) -> int:
        """Which revision a blob URL should carry to be cacheable."""
        if name == "render.png":
            return int(page.get("render_revision") or 0)
        if name == "text.json":
            return int(page.get("text_revision") or 0)
        return int(page.get("revision") or 0)

    def blob_etag(self, page: dict, name: str) -> str | None:
        """The stored hash of a derived blob's bytes. None for the drawing,
        whose bytes only change with the revision, and for a page written
        before derived blobs were hashed."""
        key = DERIVED_ETAG_KEYS.get(name)
        value = page.get(key) if key else None
        return str(value) if value else None

    def page_public(self, notebook: str, page_id: str) -> dict:
        """page.json plus the three blob URLs, revision-stamped."""
        page = self.read_page(notebook, page_id)
        rel = page["path"]
        urls = {}
        for name, key in (
            ("drawing.pkdrawing", "drawing_url"),
            ("render.png", "render_url"),
            ("text.json", "text_url"),
        ):
            if (self.dir / rel / name).is_file():
                urls[key] = page_url(
                    rel,
                    name,
                    self.blob_revision(page, name),
                    self.blob_etag(page, name),
                )
            else:
                urls[key] = None
        manifest = self.read_notebook(notebook)
        return {
            **page,
            "notebook_id": manifest.get("id"),
            "durable_url": paper_page_url(manifest.get("id") or "", page["id"]),
            **urls,
        }

    @staticmethod
    def effective_transcript(text: dict | None, revision: int | None = None) -> str:
        """Use a user correction in preference to raw OCR.

        Native clients may send only correction spans. Accept the common
        start/end, offset/length and before/after spellings and materialise them
        here for readers and search.
        """
        if not isinstance(text, dict):
            return ""
        correction_revision = text.get("corrections_revision")
        try:
            correction_revision = (
                int(correction_revision) if correction_revision is not None else None
            )
        except (TypeError, ValueError):
            correction_revision = None
        if (
            revision is not None
            and correction_revision is not None
            and correction_revision != int(revision)
        ):
            return str(text.get("transcript") or "").strip()
        corrected = text.get("corrected_transcript")
        if isinstance(corrected, str) and corrected.strip():
            return corrected.strip()
        raw = str(text.get("transcript") or "")
        corrections = text.get("corrections")
        if not isinstance(corrections, list) or not corrections:
            return raw.strip()
        out = raw
        spans: list[tuple[int, int, str]] = []
        for item in corrections:
            if not isinstance(item, dict):
                continue
            try:
                start = int(item.get("start", item.get("offset")))
                end = int(
                    item.get(
                        "end",
                        start + int(item.get("length", len(str(item.get("before") or "")))),
                    )
                )
            except (TypeError, ValueError):
                continue
            replacement = item.get(
                "replacement",
                item.get("corrected", item.get("after", item.get("text"))),
            )
            if start < 0 or end < start or end > len(raw) or replacement is None:
                continue
            spans.append((start, end, str(replacement)))
        for start, end, replacement in sorted(spans, reverse=True):
            out = out[:start] + replacement + out[end:]
        return out.strip()

    def delete_page(self, notebook: str, page_id: str) -> dict:
        nb_rel = self.notebook_path(notebook)
        rel = self.page_dir_path(nb_rel, page_id)
        if not (self.dir / rel).is_dir():
            raise NoteError(404, f"Page not found: {rel}")
        manifest = self.read_notebook(nb_rel)
        pages = [p for p in manifest.get("pages") or [] if isinstance(p, str)]
        position = pages.index(page_id) if page_id in pages else len(pages)
        item = self.trash.trash_paper(
            "page",
            rel,
            extra={
                "notebook": nb_rel,
                "notebook_id": manifest.get("id"),
                "page_id": page_id,
                "position": position,
            },
        )
        manifest["pages"] = [p for p in pages if p != page_id]
        if manifest.get("cover") == page_id:
            manifest["cover"] = manifest["pages"][0] if manifest["pages"] else None
        self._write_notebook(nb_rel, manifest)
        self.record_tombstone(rel, "page", self.next_seq())
        return item

    def _restore_notebook_for(self, item: dict) -> str:
        """The notebook a trashed page goes back into, decided before anything
        moves: by stable id, so the page follows a rename, else the path it was
        deleted from (entries trashed before the id was recorded).

        The path stands in for a missing id only when what is there now is
        that same notebook, or carries no id: a new notebook that took the
        old name is somebody else's, and the page's own is in the Trash."""
        notebook_id = str(item.get("notebook_id") or "")
        if notebook_id:
            try:
                return self.find_notebook_by_id(notebook_id)[0]
            except NoteError:
                pass
        original = str(item.get("original_path") or "")
        try:
            rel = self.notebook_path(
                item.get("notebook") or original.rpartition("/pages/")[0]
            )
        except NoteError:
            rel = ""
        if rel and (self.dir / rel / "notebook.json").is_file():
            there = read_json(self.dir / rel / "notebook.json", None, skip_corrupt=True)
            there_id = str(there.get("id") or "") if isinstance(there, dict) else ""
            if not notebook_id or not there_id or there_id == notebook_id:
                return rel
        raise NoteError(409, "Restore the notebook first")

    def restore_trash(self, item_id: str) -> dict:
        """Put a trashed notebook or page back, and re-list a page in its
        notebook at the position it was deleted from.

        The restored files still carry the ``seq`` they had before the delete,
        which a client's cursor has long passed, so everything that comes back
        is re-stamped — pages before the manifest that lists them.
        """
        item = self.trash.manifest(item_id)
        if item.get("kind") != "page":
            restored = self.trash.restore(item_id)
            path = restored["path"]
            self.clear_tombstone(path)
            if restored["kind"] == "notebook":
                self._restamp_pages(path)
                manifest = read_json(
                    self.dir / path / "notebook.json", None, skip_corrupt=True
                )
                if isinstance(manifest, dict):
                    self._write_notebook(path, manifest)
            return restored

        original = str(item.get("original_path") or "")
        page_id = self.check_page_id(item.get("page_id") or original.rpartition("/")[2])
        # Refused here, while the page is still in the Trash: moved first, it
        # would squat on the path its notebook's own restore needs.
        nb_rel = self._restore_notebook_for(item)
        restored = self.trash.restore(item_id, target=f"{nb_rel}/pages/{page_id}")
        path = restored["path"]
        restored["notebook"] = nb_rel
        self.clear_tombstone(path)
        self._restamp_page(path)
        manifest = self.read_notebook(nb_rel)
        pages = [p for p in manifest.get("pages") or [] if isinstance(p, str)]
        if page_id not in pages:
            position = int(item.get("position") or len(pages))
            pages.insert(min(position, len(pages)), page_id)
            manifest["pages"] = pages
            self._write_notebook(nb_rel, manifest)
        return restored

    def set_transcript_ai(
        self, notebook: str, page_id: str, text: str, model: str, *, made_for: int
    ) -> dict:
        """Record an AI reading of the render made for revision ``made_for``.

        Derived data: no revision bump. ``text_revision`` stays too — it says
        which ink the device's OCR lines describe, and this is not that OCR.
        The new ``text_etag`` is what tells a cached reader the file changed.
        """
        rel = self.page_dir_path(notebook, page_id)
        page = self.read_page(notebook, page_id)
        existing = read_json(self.dir / rel / "text.json", None, skip_corrupt=True)
        merged = existing if isinstance(existing, dict) else {"lines": [], "transcript": ""}
        merged["transcript_ai"] = {
            "text": text,
            "model": model,
            "at": iso_utc(),
            "revision": int(made_for),
        }
        page["text_etag"] = self._write_text(rel, merged)
        return self._write_page(rel, page)

    # -- assets --------------------------------------------------------------

    def store_asset(self, notebook: str, filename: str, data: bytes) -> dict:
        """Content-hashed, so the same image imported twice costs one blob and
        the URL can be cached forever."""
        nb_rel = self.notebook_path(notebook)
        if not (self.dir / nb_rel).is_dir():
            raise NoteError(404, f"Notebook not found: {nb_rel}")
        suffix = Path(filename or "").suffix.lower()
        if suffix not in ASSET_EXTENSIONS:
            raise NoteError(400, f"Unsupported asset type: {suffix or 'none'}")
        if not data:
            raise NoteError(400, "Empty file")
        self._check_size("asset", data, MAX_ASSET_BYTES)
        digest = hashlib.sha256(data).hexdigest()[:16]
        name = f"{safe_stem(filename)}-{digest}{suffix}"
        path = self.dir / nb_rel / "assets" / name
        # The name promises these bytes, so a file of another length is a torn
        # earlier write, not a match.
        if not path.is_file() or path.stat().st_size != len(data):
            atomic_bytes(path, data)
        return {
            "path": f"{nb_rel}/assets/{name}",
            "name": name,
            "size": len(data),
            "url": f"/api/paper/assets/{nb_rel}/{name}",
        }

    def resolve_asset(self, notebook: str, filename: str) -> Path:
        nb_rel = self.notebook_path(notebook)
        if "/" in filename or filename in ("", ".", ".."):
            raise NoteError(400, "Invalid asset name")
        path = (self.dir / nb_rel / "assets" / filename).resolve()
        if not path.is_relative_to((self.dir / nb_rel / "assets").resolve()):
            raise NoteError(400, "Invalid asset name")
        if not path.is_file():
            raise NoteError(404, f"Asset not found: {filename}")
        return path

    # -- history -------------------------------------------------------------

    def restore_page_version(self, vault, notebook: str, page_id: str, sha: str) -> dict:
        """Bring an old commit's page back as revision N+1.

        Never a history rewrite: the restore is an ordinary save, so the git
        log keeps every version including the one being replaced.
        """
        parts = self.read_page_version(vault, notebook, page_id, sha)
        return self.apply_page_version(notebook, page_id, parts)

    def read_page_version(self, vault, notebook: str, page_id: str, sha: str) -> dict:
        """The source and derived parts of a page as ``sha`` had them.

        Only git reads, nothing in the working tree, so it is safe to run in a
        worker thread; the write half is ``apply_page_version``.
        """
        rel = self.page_dir_path(notebook, page_id)
        raw_page = vault.commit_bytes_sync(sha, f"{rel}/page.json")
        if raw_page is None:
            raise NoteError(404, f"No version of this page at {sha[:8]}")
        try:
            old = json.loads(raw_page.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise NoteError(500, "That version's page.json is unreadable") from exc
        raw_text = vault.commit_bytes_sync(sha, f"{rel}/text.json")
        text = None
        if raw_text:
            try:
                text = json.loads(raw_text.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                text = None
        return {
            "page": {key: old[key] for key in PAGE_PATCH_FIELDS if key in old},
            "drawing": vault.commit_bytes_sync(sha, f"{rel}/drawing.pkdrawing"),
            "render": vault.commit_bytes_sync(sha, f"{rel}/render.png"),
            "text": text,
        }

    def apply_page_version(
        self, notebook: str, page_id: str, parts: dict, *, client_id: str | None = None
    ) -> dict:
        """Save ``read_page_version``'s parts on top of the current revision.

        The restore route leaves ``client_id`` unset on purpose: a restore is
        nobody's retry, so a device's pending save from before it must still
        come back as a conflict rather than be taken for that device's own
        next revision.
        """
        current = self.read_page(notebook, page_id)
        return self.save_page(
            notebook,
            page_id,
            base_revision=int(current.get("revision") or 0),
            page=parts.get("page"),
            drawing=parts.get("drawing"),
            render=parts.get("render"),
            text=parts.get("text"),
            client_id=client_id,
        )
