"""Gallery: the eighth section, pictures stored as notes.

A picture is a note under ``gallery/`` whose frontmatter carries ``gallery: 1``
(the integer, as recipes) and an ``image`` that is an attachment URL. The
folder directly under ``gallery/`` is the picture's board; a note directly in
``gallery/`` is on no board and shows under All only.

What the owner sees is theirs: the title (typed, or a saved page's own
title), the body (their description) and their tags. What OMNIA makes of
the picture is its *reading*, kept under ``ai`` and never shown: a title,
a description, what kind of picture it is, its era, the works it comes
from (a game, a film, a franchise, a platform) and its themes. The reading
is what search and related pictures run on (``gallery_search``).

    title, created, modified    the note's own keys; title may be ''
    gallery: 1                  the marker
    image                       /api/attachments/<name>, the stored picture
    width, height               its pixel size, so a grid lays out before it loads
    color                       its average colour, '#rrggbb', the placeholder
    source_url                  where it came from ('Visit site'); omitted when none
    gallery_tags                the owner's tags: lowercased, deduped
    ai                          the reading: title, description, kind, era,
                                works, themes; written by gallery_ai
    described                   when the reading was written; absent until then

A picture's file is named after its image's content digest
(``gallery/<board>/<digest>.md``), so the same picture saved to one board
twice is the same note, and the name never needs a title. The title lives in
frontmatter only (``gallery`` is a TITLE_EXEMPT_ROOT).

Imported by the indexer, so this module must not import from ``app.index``
at module level. The service functions take what ``app.state`` hands them;
the API routes and the OMNIA tools both call them.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import re
import unicodedata
from pathlib import PurePosixPath
from typing import Any

from pydantic import BaseModel

from . import media_lookup
from .media_items import media_slug
from .vault import fm
from .vault.notes import NoteError

log = logging.getLogger(__name__)

GALLERY_KEY = "gallery"
GALLERY_DIR = "gallery"
ATTACHMENT_PREFIX = "/api/attachments/"
THUMB_PREFIX = "/api/gallery/thumb/"
# The hidden reading OMNIA makes of each picture.
AI_KEY = "ai"

# Every frontmatter key the format owns besides title/created/modified.
RECORD_KEYS = (
    GALLERY_KEY,
    "image",
    "width",
    "height",
    "color",
    "source_url",
    "gallery_tags",
    "ai",
    "described",
)
# What the describer writes, set aside by Undo as a recipe drawing is.
READING_KEYS = (AI_KEY, "described")
# How much each part of a reading says about what a picture is "about", for
# related pictures: where it comes from and when say most.
THEME_WEIGHTS = {"works": 2.0, "era": 1.5, "kind": 1.2, "themes": 1.0}
MAX_THEMES = 16
MAX_WORKS = 6

MAX_TITLE = 200
MAX_TAGS = 24
MAX_TAG = 40
MAX_DESCRIPTION = 4000
MAX_BOARD = 80
MAX_SOURCE_URL = 2000
# Images are stored at most this long on the long edge (store_image_bytes).
# Reference boards hold tall collages (a strip of four image boards is about
# 1200 x 3400), so the cap sits above the cover and backdrop ones.
IMAGE_KIND = "gallery"

_DIGEST_RE = re.compile(r"-([0-9a-f]{16})\.[A-Za-z0-9]+$")
_NAME_RE = re.compile(r"^[^/\\]+\.(?:png|jpe?g|gif|webp)$", re.I)
_COLOR_RE = re.compile(r"^#[0-9a-f]{6}$")


# -- the wire models ----------------------------------------------------------------


class PinCard(BaseModel):
    """One picture in a grid."""

    path: str
    title: str
    board: str
    image: str  # /api/attachments/<name>
    thumb: str  # /api/gallery/thumb/<name>
    width: int | None = None
    height: int | None = None
    color: str | None = None
    created: str = ""


class Pin(PinCard):
    """One picture, whole."""

    description: str = ""
    tags: list[str] = []
    source_url: str | None = None
    described: str | None = None
    modified: str = ""
    revision: str = ""


class Board(BaseModel):
    name: str
    count: int
    updated: str | None = None
    # Up to three thumbnails, newest first: the card's collage.
    covers: list[str] = []


# -- the format -----------------------------------------------------------------------


def is_gallery(meta: dict) -> bool:
    """Strictly the integer 1, as recipes: `gallery: true` typed in the
    editor stays an ordinary note."""
    value = meta.get(GALLERY_KEY)
    return type(value) is int and value == 1


def in_gallery_folder(path: str) -> bool:
    """Under `gallery/`, spelled exactly so (production Linux is case
    sensitive; `Gallery/` is another folder)."""
    first, sep, _rest = path.partition("/")
    return bool(sep) and first == GALLERY_DIR


def is_gallery_note(path: str, meta: dict) -> bool:
    return in_gallery_folder(path) and is_gallery(meta)


def board_of(path: str) -> str:
    """The board a picture is on: the folder directly under gallery/, or ''."""
    parts = PurePosixPath(path).parts
    return parts[1] if len(parts) > 2 else ""


def board_dir(board: str) -> str:
    return f"{GALLERY_DIR}/{board}" if board else GALLERY_DIR


def clean_board(name: str | None) -> str:
    """A board name as its folder is spelled: '' for none, else one safe path
    segment (the media slug). Raises NoteError(400) for a name with nothing
    left in it."""
    raw = (name or "").strip()
    if not raw:
        return ""
    if len(raw) > MAX_BOARD:
        raise NoteError(400, f"A board name is at most {MAX_BOARD} characters")
    if not raw.replace("/", "").replace("\\", "").strip(" ._"):
        raise NoteError(400, "Not a usable board name")
    return media_slug(raw.replace("/", " ").replace("\\", " "))


def image_name(url: str | None) -> str | None:
    """The attachment file name an `image` value points at, or None when the
    value is not a plain attachment URL."""
    if not isinstance(url, str):
        return None
    value = url.strip()
    if not value.startswith(ATTACHMENT_PREFIX):
        return None
    name = value[len(ATTACHMENT_PREFIX):]
    return name if _NAME_RE.match(name) else None


def image_digest(name: str) -> str:
    """The content digest in a stored attachment's name. A name that carries
    none (a file placed by hand) is hashed instead, so it still has one."""
    match = _DIGEST_RE.search(name)
    if match:
        return match.group(1)
    return hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]


def thumb_url(name: str) -> str:
    return f"{THUMB_PREFIX}{name}"


def _fold(text: str) -> str:
    return unicodedata.normalize("NFC", text).casefold()


def _text(value: Any, limit: int) -> str:
    if value is None or isinstance(value, (dict, list, bool)):
        return ""
    return str(value).strip()[:limit]


def _int(value: Any) -> int | None:
    if type(value) is int and 0 < value < 1_000_000:
        return value
    return None


def _color(value: Any) -> str | None:
    if isinstance(value, str) and _COLOR_RE.match(value.strip().lower()):
        return value.strip().lower()
    return None


def clean_tags(value: Any) -> list[str]:
    """Lowercased, '#' and stray spaces gone, deduped in order, capped."""
    items = value if isinstance(value, list) else []
    seen: list[str] = []
    for item in items:
        if not isinstance(item, (str, int, float)) or isinstance(item, bool):
            continue
        tag = " ".join(str(item).replace("#", " ").split()).lower()[:MAX_TAG]
        if tag and tag not in seen:
            seen.append(tag)
        if len(seen) >= MAX_TAGS:
            break
    return seen


def _stamp(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _source(value: Any) -> str | None:
    text = _text(value, MAX_SOURCE_URL)
    return text if text.lower().startswith(("https://", "http://")) else None


def _body(text: str) -> str:
    # LF only, as recipes: a CR written here reads back as other text.
    value = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return f"{value[:MAX_DESCRIPTION]}\n" if value else ""


def _revision(content: str) -> str:
    # index.db.content_hash, which this module may not import.
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _theme(value: Any) -> str:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return ""
    return " ".join(str(value).replace("#", " ").split()).lower()[:MAX_TAG * 2]


def clean_reading(value: Any) -> dict[str, Any]:
    """A reading as stored: every part present, cleaned, capped. Anything
    that is not a mapping is an empty reading."""
    raw = value if isinstance(value, dict) else {}

    def many(key: str, cap: int) -> list[str]:
        items = raw.get(key) if isinstance(raw.get(key), list) else []
        out: list[str] = []
        for item in items:
            theme = _theme(item)
            if theme and theme not in out:
                out.append(theme)
        return out[:cap]

    return {
        "title": _text(raw.get("title"), MAX_TITLE),
        "description": _text(raw.get("description"), MAX_DESCRIPTION),
        "kind": _theme(raw.get("kind")),
        "era": _theme(raw.get("era")),
        "works": many("works", MAX_WORKS),
        "themes": many("themes", MAX_THEMES),
    }


def weighted_themes(reading: dict[str, Any]) -> list[tuple[str, float]]:
    """The reading's kind, era, works and themes, each with the weight its
    part carries; a theme named twice keeps the larger weight."""
    weights: dict[str, float] = {}

    def put(theme: str, weight: float) -> None:
        if theme:
            weights[theme] = max(weights.get(theme, 0.0), weight)

    for work in reading.get("works") or []:
        put(work, THEME_WEIGHTS["works"])
    put(reading.get("era") or "", THEME_WEIGHTS["era"])
    put(reading.get("kind") or "", THEME_WEIGHTS["kind"])
    for theme in reading.get("themes") or []:
        put(theme, THEME_WEIGHTS["themes"])
    return sorted(weights.items(), key=lambda kv: (-kv[1], kv[0]))


def meaning_text(reading: dict[str, Any]) -> str:
    """The text a reading is embedded as for "means the same": what it is,
    when, from where, its themes, then the description."""
    parts = [
        reading.get("kind") or "",
        reading.get("era") or "",
        ", ".join(reading.get("works") or []),
        ", ".join(reading.get("themes") or []),
        reading.get("description") or "",
    ]
    return ". ".join(p for p in parts if p)


def meaning_key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16] if text else ""


def pin_from_content(path: str, content: str) -> Pin:
    """The picture at `path`. Raises ValueError when the note is not one."""
    meta, body = fm.parse(content)
    if not is_gallery_note(path, meta):
        raise ValueError("not a gallery picture")
    name = image_name(meta.get("image"))
    if name is None:
        raise ValueError("a gallery picture needs an attachment image")
    described = meta.get("described")
    return Pin(
        path=path,
        title=_text(meta.get("title"), MAX_TITLE),
        board=board_of(path),
        image=f"{ATTACHMENT_PREFIX}{name}",
        thumb=thumb_url(name),
        width=_int(meta.get("width")),
        height=_int(meta.get("height")),
        color=_color(meta.get("color")),
        created=_stamp(meta.get("created")),
        description=body.strip(),
        tags=clean_tags(meta.get("gallery_tags")),
        source_url=_source(meta.get("source_url")),
        described=_stamp(described) if described else None,
        modified=_stamp(meta.get("modified")),
        revision=_revision(content),
    )


def pin_fields(path: str, meta: dict, body: str) -> dict | None:
    """Frontmatter projected to a `gallery` index row, or None when the note
    is not a usable picture. Never raises."""
    try:
        name = image_name(meta.get("image"))
        if name is None:
            return None
        title = _text(meta.get("title"), MAX_TITLE)
        board = board_of(path)
        tags = clean_tags(meta.get("gallery_tags"))
        source = _source(meta.get("source_url")) or ""
        reading = clean_reading(meta.get(AI_KEY))
        themes = weighted_themes(reading)
        meaning = meaning_text(reading)
        words = [
            title, board, *tags, body, source, reading["title"], reading["description"],
            *(theme for theme, _w in themes),
        ]
        return {
            "path": path,
            "board": board,
            "title": title,
            "image": name,
            "digest": image_digest(name),
            "width": _int(meta.get("width")),
            "height": _int(meta.get("height")),
            "color": _color(meta.get("color")),
            "tags": json.dumps(tags, ensure_ascii=False),
            "themes": json.dumps(themes, ensure_ascii=False),
            "meaning": meaning_key(meaning),
            "described": 1 if meta.get("described") else 0,
            "search": _fold(" ".join(w for w in words if w)),
            "created": _stamp(meta.get("created")),
            "modified": _stamp(meta.get("modified")),
        }
    except Exception:
        log.exception("failed reading gallery fields of %s", path)
        return None


def build_pin_content(
    *,
    title: str,
    image: str,
    width: int | None,
    height: int | None,
    color: str | None,
    source_url: str | None,
    description: str = "",
) -> str:
    now = fm.now_iso()
    meta: dict[str, Any] = {
        "title": _text(title, MAX_TITLE),
        "created": now,
        "modified": now,
        GALLERY_KEY: 1,
        "image": image,
    }
    if width and height:
        meta["width"] = width
        meta["height"] = height
    if color:
        meta["color"] = color
    if source_url:
        meta["source_url"] = source_url
    return fm.serialize(meta, _body(description))


def measure(data: bytes) -> tuple[int | None, int | None, str | None]:
    """(width, height, average colour) of an image; Nones when Pillow cannot
    read it. Blocking: run it in a thread."""
    try:
        from PIL import Image, ImageStat

        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            if width * height > media_lookup.MAX_ART_PIXELS:
                return width, height, None
            small = image.convert("RGB")
            small.thumbnail((48, 48))
            r, g, b = (int(round(v)) for v in ImageStat.Stat(small).mean[:3])
            return width, height, f"#{r:02x}{g:02x}{b:02x}"
    except Exception:
        return None, None, None


def sniff(data: bytes) -> str | None:
    """The real image type from the first bytes: the four the providers and
    every browser read. None for anything else."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


# -- the service layer: API routes and OMNIA tools both call these ----------------


def _card(row) -> PinCard:
    name = row["image"]
    return PinCard(
        path=row["path"],
        title=row["title"] or "",
        board=row["board"] or "",
        image=f"{ATTACHMENT_PREFIX}{name}",
        thumb=thumb_url(name),
        width=row["width"],
        height=row["height"],
        color=row["color"],
        created=row["created"] or "",
    )


def _read_pin(state, path: str) -> tuple[str, Pin]:
    # One spelling per picture, as recipes: "gallery/./x.md" would index a
    # second row that outlives the note.
    if str(PurePosixPath(path)) != path:
        raise NoteError(404, f"Not a gallery picture: {path}")
    content = state.store.read(path)
    try:
        return content, pin_from_content(path, content)
    except ValueError:
        raise NoteError(404, f"Not a gallery picture: {path}") from None


async def get_pin(state, path: str) -> Pin:
    return _read_pin(state, path)[1]


def board_folders(state) -> list[str]:
    """Every board folder on disk, empty ones included."""
    found = []
    for folder in state.store.list_folders():
        parts = PurePosixPath(folder).parts
        if len(parts) == 2 and parts[0] == GALLERY_DIR:
            found.append(parts[1])
    return found


async def list_boards(state) -> list[Board]:
    """Boards with pictures, the one saved to most recently first; then
    empty boards by name."""
    rows = await state.db.execute_fetchall(
        "SELECT board, image, created FROM gallery WHERE board != '' ORDER BY created DESC, path"
    )
    boards: dict[str, Board] = {}
    for row in rows:
        board = boards.get(row["board"])
        if board is None:
            board = boards[row["board"]] = Board(
                name=row["board"], count=0, updated=row["created"] or None
            )
        board.count += 1
        if len(board.covers) < 3:
            board.covers.append(thumb_url(row["image"]))
    empty = sorted(
        (name for name in board_folders(state) if name not in boards), key=str.casefold
    )
    return [*boards.values(), *(Board(name=name, count=0) for name in empty)]


async def count_pins(state) -> int:
    rows = await state.db.execute_fetchall("SELECT COUNT(*) AS n FROM gallery")
    return rows[0]["n"] if rows else 0


async def list_pins(
    state, board: str | None = None, *, offset: int = 0, limit: int = 60
) -> tuple[list[PinCard], int]:
    """Newest first. `board` None is every picture; '' those on no board."""
    where, args = ("", ()) if board is None else ("WHERE board = ?", (board,))
    total = (
        await state.db.execute_fetchall(f"SELECT COUNT(*) AS n FROM gallery {where}", args)
    )[0]["n"]
    rows = await state.db.execute_fetchall(
        f"SELECT * FROM gallery {where} ORDER BY created DESC, path LIMIT ? OFFSET ?",
        (*args, max(1, min(limit, 500)), max(0, offset)),
    )
    return [_card(row) for row in rows], total


async def rows_by_path(state) -> dict[str, Any]:
    rows = await state.db.execute_fetchall("SELECT * FROM gallery")
    return {row["path"]: row for row in rows}


async def cards_for(state, paths: list[str]) -> list[PinCard]:
    """Cards for `paths` in that order; paths that are not pictures drop out."""
    rows = await rows_by_path(state)
    return [_card(rows[p]) for p in paths if p in rows]


async def _announce(state, path: str, written: str, origin: str | None) -> None:
    await state.indexer.index_note(path, written)
    state.autocommit.touch()
    state.live.note_changed(path, _revision(written), origin=origin)


def _queue_work(state, path: str, *, describe: bool = True, force: bool = False) -> None:
    # Imported here: gallery_jobs imports this module.
    from . import gallery_jobs

    gallery_jobs.process_soon(state, path, describe=describe, force=force)


async def add_picture(
    state,
    data: bytes,
    *,
    board: str = "",
    title: str = "",
    source_url: str | None = None,
    description: str = "",
    name_hint: str = "",
    origin: str | None = None,
) -> tuple[Pin, bool]:
    """Store an image and its picture note. Answers (picture, created): the
    same image already on that board answers the picture there and False.
    The describer and the image embedding run after, in the background."""
    if not data:
        raise NoteError(400, "Empty image")
    ctype = sniff(data)
    if ctype is None:
        raise NoteError(400, "Not a PNG, JPEG, GIF or WebP image")
    board = clean_board(board)
    source = _source(source_url) if source_url else None
    if source_url and source is None:
        raise NoteError(400, "A source must be an http or https link")
    hint = media_slug(name_hint or title or "picture")
    stored = await media_lookup.store_image_bytes(state, data, ctype, hint, kind=IMAGE_KIND)
    name = stored["url"][len(ATTACHMENT_PREFIX):]
    path = f"{board_dir(board)}/{image_digest(name)}.md"
    if state.store.exists(path):
        try:
            return _read_pin(state, path)[1], False
        except NoteError:
            raise NoteError(409, f"A note that is not a picture is at {path}") from None
    abs_image = state.store.resolve_attachment(stored["path"])
    width, height, color = await asyncio.to_thread(measure, abs_image.read_bytes())
    content = build_pin_content(
        title=title,
        image=stored["url"],
        width=width,
        height=height,
        color=color,
        source_url=source,
        description=description,
    )
    written = state.store.create(path, content, check_name=False)
    await _announce(state, path, written, origin)
    _queue_work(state, path)
    return pin_from_content(path, written), True


async def update_pin(
    state,
    path: str,
    *,
    title: str | None = None,
    description: str | None = None,
    tags: list[str] | None = None,
    source_url: str | None = None,
    base_revision: str | None = None,
    origin: str | None = None,
) -> Pin:
    """Change the fields given; the rest stay. A stale `base_revision` is a
    409, after a 404 for a picture that is gone."""
    content, _pin = _read_pin(state, path)
    if base_revision is not None and base_revision != _revision(content):
        raise NoteError(409, "The picture changed elsewhere — reload and retry")
    meta, body = fm.parse(content, strict=True)
    if title is not None:
        meta["title"] = _text(title, MAX_TITLE)
    if tags is not None:
        cleaned = clean_tags(tags)
        if cleaned:
            meta["gallery_tags"] = cleaned
        else:
            meta.pop("gallery_tags", None)
    if source_url is not None:
        if source_url.strip():
            source = _source(source_url)
            if source is None:
                raise NoteError(400, "A source must be an http or https link")
            meta["source_url"] = source
        else:
            meta.pop("source_url", None)
    if description is not None:
        body = _body(description)
    written = state.store.write(path, fm.serialize(meta, body))
    await _announce(state, path, written, origin)
    return pin_from_content(path, written)


async def _moved(state, moves: list[tuple[str, str]], origin: str | None) -> None:
    """Everything a note move owes the rest of the app, as the notes API does
    it: index, inbound links, OMNIA threads, and the live events."""
    from .index.db import content_hash
    from .index.links import rewrite_inbound_links
    from .overseer.threads import carry_note_threads

    for old, new in moves:
        await state.indexer.rename_note(old, new)
    await rewrite_inbound_links(state, moves)
    carry_note_threads(state, moves)
    state.autocommit.touch()
    for old, new in moves:
        revision = content_hash(state.store.read(new))
        state.live.note_moved(old, new, revision, origin=origin)
        state.live.note_removed(old, origin=origin)
        state.live.note_changed(new, revision, origin=origin)


async def move_pin(state, path: str, board: str, *, origin: str | None = None) -> Pin:
    """Onto another board ('' for none). The same picture already there is a
    409: the owner deletes one or the other."""
    _read_pin(state, path)
    board = clean_board(board)
    target = f"{board_dir(board)}/{PurePosixPath(path).name}"
    if target == path:
        return _read_pin(state, path)[1]
    if state.store.exists(target):
        raise NoteError(409, f"That picture is already on {board or 'no board'}")
    state.store.rename(path, target)
    proofread = getattr(state, "proofread", None)
    if proofread is not None:
        proofread.rename(path, target)
    await _moved(state, [(path, target)], origin)
    return _read_pin(state, target)[1]


async def delete_pin(state, path: str, *, origin: str | None = None) -> None:
    """To Trash, as the notes API deletes a note. The image stays in
    attachments (it may be on another board too) and in git."""
    _read_pin(state, path)
    state.trash.trash_note(path)
    await state.indexer.remove_note(path)
    proofread = getattr(state, "proofread", None)
    if proofread is not None:
        proofread.clear(path)
    state.autocommit.touch()
    state.live.note_removed(path, origin=origin)


async def create_board(state, name: str) -> str:
    board = clean_board(name)
    if not board:
        raise NoteError(400, "A board needs a name")
    for existing in board_folders(state):
        if existing.casefold() == board.casefold():
            return existing
    state.store.create_folder(board_dir(board))
    state.autocommit.touch()
    return board


async def rename_board(state, name: str, new_name: str, *, origin: str | None = None) -> str:
    old = clean_board(name)
    new = clean_board(new_name)
    if not old or not new:
        raise NoteError(400, "A board needs a name")
    if old == new:
        return new
    moves = state.store.rename_folder(board_dir(old), board_dir(new))
    from . import sidebar_order

    sidebar_order.rename_paths(
        state.settings.vault_dir, folders=[(board_dir(old), board_dir(new))], notes=moves
    )
    await _moved(state, moves, origin)
    return new


async def delete_board(state, name: str, *, origin: str | None = None) -> int:
    """The board and every picture on it, to Trash as one folder."""
    board = clean_board(name)
    if not board:
        raise NoteError(400, "A board needs a name")
    _manifest, deleted = state.trash.trash_folder(board_dir(board))
    for rel in deleted:
        await state.indexer.remove_note(rel)
    state.autocommit.touch()
    for rel in deleted:
        state.live.note_removed(rel, origin=origin)
    return len(deleted)


def reading_of(content: str) -> dict[str, Any]:
    """The reading a picture note holds (empty parts when it has none)."""
    meta, _body = fm.parse(content)
    return clean_reading(meta.get(AI_KEY))


async def get_reading(state, path: str) -> dict[str, Any]:
    return reading_of(_read_pin(state, path)[0])


async def store_description(
    state,
    path: str,
    reading: dict[str, Any],
    *,
    force: bool = False,
) -> Pin | None:
    """Write what the describer made of the picture, as its hidden reading.
    None when the picture is gone. Without `force` a picture already read
    is left alone. The owner's title, description and tags are never
    touched, and `modified` stays as it was: a reading is not an edit."""
    try:
        content, pin = _read_pin(state, path)
    except NoteError as exc:
        if exc.status == 404:
            return None
        raise
    if pin.described and not force:
        return pin
    meta, body = fm.parse(content, strict=True)
    meta[AI_KEY] = clean_reading(reading)
    meta["described"] = fm.now_iso()
    written = state.store.write(path, fm.serialize(meta, body), preserve_modified=True)
    # No origin: an editor open on the note merges the reading in.
    await _announce(state, path, written, None)
    return pin_from_content(path, written)


# -- Undo: a description landing is the app's write, not OMNIA's -----------------


def carry_description(content: str, source: str) -> str:
    """`content` with the reading `source` holds (READING_KEYS, in the order
    store_description writes them: a key new to the block is appended, so
    another order is other text). Raises ValueError for a block that does
    not load."""
    meta, body = fm.parse(content, strict=True)
    theirs, _their_body = fm.parse(source, strict=True)
    for key in READING_KEYS:
        meta.pop(key, None)
    for key in READING_KEYS:
        if key in theirs:
            meta[key] = theirs[key]
    return fm.serialize(meta, body)


def only_description_changed(path: str, recorded: str, current: str) -> bool:
    """Whether the picture at `path` is `recorded` with nothing changed since
    but its reading. The reading lands by itself seconds after a picture is
    saved (gallery_jobs), so Undo of the save sets it aside; any other
    change, or a note that is no picture, is False."""
    try:
        meta, _body = fm.parse(recorded, strict=True)
        return is_gallery_note(path, meta) and (
            current == recorded or carry_description(recorded, current) == current
        )
    except ValueError:
        return False
