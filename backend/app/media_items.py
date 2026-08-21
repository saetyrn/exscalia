"""Media tracker items: books, movies, TV shows, and games.

A media item is an ordinary note in the hidden ``media/`` folder carrying a
``media: book|movie|tv|game`` frontmatter key. Frontmatter holds the
structured fields (creator, year, cover, rating, status, dates); the body is
the user's review plus a ``## Log`` section of dated progress lines:

    ## Log
    - 2026-08-03 — page 120
    - 2026-08-05 — S02E05
    - 2026-08-12 — finished, rated 8

The item is identified by frontmatter rather than by location so it survives
being moved, and the log lines are plain markdown so they stay readable and
editable by hand. Both are parsed into rebuildable SQLite tables by the
indexer, the same way tasks are parsed out of note bodies.

Imported by the indexer, so this module must not import from ``app.index``.
"""
import re
from dataclasses import dataclass
from datetime import date as date_cls
from typing import Any

from .vault import fm

MEDIA_KEY = "media"
STATUS_KEY = "media_status"
MEDIA_DIR = "media"
MEDIA_TYPES = ("book", "movie", "tv", "game")
STATUSES = ("planned", "active", "finished", "dropped")
ENTRY_KINDS = ("started", "progress", "watched", "finished", "note")

LOG_HEADING = "## Log"
# Any h2 ends the log section; deeper headings inside it are left alone.
_H2_RE = re.compile(r"^##(?!#)")
_LOG_HEADING_RE = re.compile(r"^##\s+log\s*$", re.IGNORECASE)
_FENCE_RE = re.compile(r"^(?:```|~~~)")
# Accept em dash, en dash, or hyphen between date and text; emit em dash.
_LOG_LINE_RE = re.compile(r"^-\s+(\d{4}-\d{2}-\d{2})\s+[—–-]\s+(.+?)\s*$")

_PAGE_RE = re.compile(r"^page\s+(\d+)$", re.IGNORECASE)
_EPISODE_RE = re.compile(r"^s(\d{1,2})\s*e(\d{1,3})$", re.IGNORECASE)
_EPISODE_WORD_RE = re.compile(r"^episode\s+(\d+)$", re.IGNORECASE)
_FINISHED_RE = re.compile(r"^finished(?:\s*,\s*rated\s+(\d{1,2}))?$", re.IGNORECASE)
_RATED_RE = re.compile(r"^rated\s+(\d{1,2})$", re.IGNORECASE)

# Windows is the strictest filesystem the vault syncs to.
_ILLEGAL_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_MAX_SLUG_LEN = 80

def is_media_item(meta: dict) -> bool:
    """True only for a literal, known media type.

    Strict membership, not truthiness: `media: yes` in the editor should get a
    visible ordinary note, not a silently mis-filed tracker item.
    """
    return meta.get(MEDIA_KEY) in MEDIA_TYPES


def media_slug(title: str) -> str:
    """A display title reduced to a filename stem safe on every synced OS."""
    slug = _ILLEGAL_FILENAME_CHARS.sub(" ", title)
    slug = re.sub(r"\s+", " ", slug).strip()
    slug = slug[:_MAX_SLUG_LEN].rstrip(". ")
    return slug or "Untitled"


def next_media_path(title: str, taken: set[str]) -> str:
    """`media/<slug>.md`, stepping past anything already there — the same
    numbering task boards use."""
    base = f"{MEDIA_DIR}/{media_slug(title)}"
    candidate = f"{base}.md"
    number = 1
    lowered = {path.lower() for path in taken}
    while candidate.lower() in lowered:
        number += 1
        candidate = f"{base} {number}.md"
    return candidate


# -- log parsing --------------------------------------------------------------


@dataclass
class LogEntry:
    line: int  # body-relative; the indexer adds the frontmatter offset
    at: str
    kind: str
    value: str | None
    text: str


def _classify(text: str) -> tuple[str, str | None]:
    t = text.strip()
    low = t.lower()
    if low == "started":
        return "started", None
    if low == "watched":
        return "watched", None
    m = _FINISHED_RE.match(t)
    if m:
        return "finished", m.group(1)
    m = _PAGE_RE.match(t)
    if m:
        return "progress", f"page {m.group(1)}"
    m = _EPISODE_RE.match(t)
    if m:
        return "progress", f"S{int(m.group(1)):02d}E{int(m.group(2)):02d}"
    m = _EPISODE_WORD_RE.match(t)
    if m:
        return "progress", f"episode {m.group(1)}"
    m = _RATED_RE.match(t)
    if m:
        return "rated", m.group(1)
    return "note", None


def parse_log(body: str) -> list[LogEntry]:
    """Dated entries from the ``## Log`` section, skipping fenced code."""
    entries: list[LogEntry] = []
    in_fence = False
    in_log = False
    for i, line in enumerate(body.splitlines()):
        stripped = line.strip()
        if _FENCE_RE.match(stripped):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if _H2_RE.match(stripped):
            in_log = bool(_LOG_HEADING_RE.match(stripped))
            continue
        if not in_log:
            continue
        m = _LOG_LINE_RE.match(line)
        if not m:
            continue
        at, text = m.group(1), m.group(2)
        try:
            date_cls.fromisoformat(at)
        except ValueError:
            continue
        kind, value = _classify(text)
        entries.append(LogEntry(line=i, at=at, kind=kind, value=value, text=text))
    return entries


def entry_text(media_type: str, kind: str, *, value: Any = None,
               text: str | None = None, rating: int | None = None) -> str:
    """The canonical text part of a log line."""
    if kind == "started":
        return "started"
    if kind == "watched":
        return "watched"
    if kind == "finished":
        return f"finished, rated {rating}" if rating else "finished"
    if kind == "progress":
        raw = str(value or "").strip()
        if not raw:
            raise ValueError("progress entries need a value (page or episode)")
        m = _EPISODE_RE.match(raw)
        if m:
            return f"S{int(m.group(1)):02d}E{int(m.group(2)):02d}"
        if raw.isdigit():
            return f"episode {raw}" if media_type == "tv" else f"page {raw}"
        return raw
    if kind == "note":
        note = (text or "").strip()
        if not note:
            raise ValueError("note entries need text")
        return note
    raise ValueError(f"unknown log entry kind: {kind}")


def format_log_line(at: str, text: str) -> str:
    return f"- {at} — {text}"


def _log_heading_line(lines: list[str]) -> int | None:
    """Index of the ``## Log`` heading, skipping fenced code; None if absent."""
    in_fence = False
    for i, raw in enumerate(lines):
        stripped = raw.strip()
        if _FENCE_RE.match(stripped):
            in_fence = not in_fence
            continue
        if not in_fence and _LOG_HEADING_RE.match(stripped):
            return i
    return None


def set_review(content: str, review: str) -> str:
    """Replace the review — everything before ``## Log`` — keeping the log
    (and anything after it) byte-identical."""
    meta, body = fm.parse(content)
    lines = body.splitlines()
    heading = _log_heading_line(lines)
    review_text = review.rstrip()
    if heading is None:
        new_body = f"{review_text}\n" if review_text else ""
    else:
        preserved = "\n".join(lines[heading:])
        if body.endswith("\n"):
            preserved += "\n"
        prefix = f"{review_text}\n\n" if review_text else ""
        new_body = prefix + preserved
    return fm.serialize(meta, new_body)


def remove_log_entry(content: str, at: str, text: str) -> tuple[str, str]:
    """Delete the first ``## Log`` line matching (date, text) — e.g. a stray
    watch. Returns (new_content, summary); raises ValueError when the note is
    not a media item or no such entry exists."""
    meta, body = fm.parse(content)
    if not is_media_item(meta):
        raise ValueError("Not a media note (missing or invalid `media:` frontmatter)")
    target = next(
        (e.line for e in parse_log(body) if e.at == at and e.text == text), None
    )
    if target is None:
        raise ValueError(f"No log entry found for {at} — {text}")
    lines = body.splitlines()
    del lines[target]
    new_body = "\n".join(lines)
    if body.endswith("\n"):
        new_body += "\n"
    return fm.serialize(meta, new_body), f"Removed “{text}”"


def append_log_entry(body: str, line: str) -> str:
    """Insert a log line at the end of the ``## Log`` section, creating the
    section at the end of the body when it is missing."""
    lines = body.splitlines()
    in_fence = False
    log_start = None
    section_end = None
    for i, raw in enumerate(lines):
        stripped = raw.strip()
        if _FENCE_RE.match(stripped):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if _H2_RE.match(stripped):
            if log_start is None:
                if _LOG_HEADING_RE.match(stripped):
                    log_start = i
            else:
                section_end = i
                break
    if log_start is None:
        base = body.rstrip("\n")
        prefix = f"{base}\n\n" if base else ""
        return f"{prefix}{LOG_HEADING}\n{line}\n"
    insert = section_end if section_end is not None else len(lines)
    # Land right after the last entry, keeping any blank gap before the next
    # heading where it was.
    while insert > log_start + 1 and not lines[insert - 1].strip():
        insert -= 1
    lines.insert(insert, line)
    out = "\n".join(lines)
    if body.endswith("\n") or section_end is None:
        out += "\n"
    return out


# -- index projection ---------------------------------------------------------


def _valid_int(value: Any, lo: int, hi: int) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None  # ratings are whole numbers; 8.5 is not "close enough"
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if lo <= n <= hi else None


def _valid_date(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    try:
        date_cls.fromisoformat(text)
    except ValueError:
        return None
    return text


def item_fields(meta: dict, fallback_title: str) -> dict[str, Any]:
    """Frontmatter projected to index-row values. Garbage degrades to NULL or
    a safe default — a single bad note must never take indexing down."""
    status = meta.get(STATUS_KEY)
    return {
        "title": str(meta.get("title") or fallback_title),
        "alias": str(meta["alias"]) if meta.get("alias") else None,
        "media_type": meta.get(MEDIA_KEY),
        "status": status if status in STATUSES else "planned",
        "creator": str(meta["creator"]) if meta.get("creator") else None,
        "year": _valid_int(meta.get("year"), 0, 3000),
        "cover": str(meta["cover"]) if meta.get("cover") else None,
        "rating": _valid_int(meta.get("rating"), 1, 10),
        "started": _valid_date(meta.get("started")),
        "finished": _valid_date(meta.get("finished")),
        "total": _valid_int(meta.get("total"), 1, 1_000_000),
        "modified": str(meta.get("modified") or ""),
    }


# -- shared mutation logic (API routes and the OMNIA tool) --------------------


def apply_update(
    content: str,
    *,
    rating: int | None = None,
    status: str | None = None,
    entry: dict | None = None,
    alias: str | None = None,
    today: str,
) -> tuple[str, str]:
    """Apply a rating / status / log-entry update to note content.

    Returns (new_content, human summary). Raises ValueError on bad input; the
    caller translates that into its own error shape.
    """
    meta, body = fm.parse(content)
    if not is_media_item(meta):
        raise ValueError("Not a media note (missing or invalid `media:` frontmatter)")
    media_type = str(meta.get(MEDIA_KEY))
    summaries: list[str] = []

    if rating is not None:
        if _valid_int(rating, 1, 10) is None:
            raise ValueError("rating must be a whole number from 1 to 10")
        rating = int(rating)
    if status is not None and status not in STATUSES:
        raise ValueError(f"status must be one of: {', '.join(STATUSES)}")

    entry_kind = None
    if entry is not None:
        entry_kind = entry.get("kind")
        if entry_kind not in ENTRY_KINDS:
            raise ValueError(f"entry kind must be one of: {', '.join(ENTRY_KINDS)}")
        at = str(entry.get("date") or today)
        try:
            date_cls.fromisoformat(at)
        except ValueError:
            raise ValueError(f"Invalid entry date: {at}")
        text = entry_text(
            media_type,
            entry_kind,
            value=entry.get("value"),
            text=entry.get("text"),
            rating=rating if entry_kind == "finished" else None,
        )
        body = append_log_entry(body, format_log_line(at, text))
        if entry_kind == "started":
            meta.setdefault("started", at)
            if status is None and meta.get(STATUS_KEY) in (None, "planned"):
                meta[STATUS_KEY] = "active"
            summaries.append("Logged started")
        elif entry_kind in ("watched", "finished"):
            meta.setdefault("finished", at)
            if status is None:
                meta[STATUS_KEY] = "finished"
            summaries.append("Logged watch" if entry_kind == "watched" else "Marked finished")
        elif entry_kind == "progress":
            summaries.append(f"Logged {text}")
        else:
            summaries.append("Added log note")

    if status is not None:
        meta[STATUS_KEY] = status
        if status == "finished":
            meta.setdefault("finished", today)
            if entry_kind not in ("watched", "finished"):
                text = entry_text(media_type, "finished", rating=rating)
                body = append_log_entry(body, format_log_line(today, text))
            summaries.append("Marked finished")
        else:
            summaries.append(f"Status: {status}")

    if rating is not None:
        meta["rating"] = rating
        summaries.append(f"Rated {rating}/10")

    if alias is not None:
        alias = alias.strip()
        if alias:
            meta["alias"] = alias
            summaries.append(f"Aliased to “{alias}”")
        else:
            meta.pop("alias", None)
            summaries.append("Cleared alias")

    if not summaries:
        raise ValueError("Nothing to update: pass a rating, status, alias, or log entry")
    return fm.serialize(meta, body), "; ".join(dict.fromkeys(summaries))


def build_item_content(
    *,
    title: str,
    media_type: str,
    status: str = "planned",
    creator: str | None = None,
    year: int | None = None,
    total: int | None = None,
    cover: str | None = None,
    rating: int | None = None,
    source: str | None = None,
    review: str | None = None,
    today: str,
) -> str:
    """Full note content for a new media item.

    Built programmatically rather than via vault templates: the template
    pipeline strips or leaks non-managed frontmatter keys (see
    ``templates.strip_managed_frontmatter``), and these keys ARE the item.
    """
    if media_type not in MEDIA_TYPES:
        raise ValueError(f"media type must be one of: {', '.join(MEDIA_TYPES)}")
    if status not in STATUSES:
        raise ValueError(f"status must be one of: {', '.join(STATUSES)}")
    if rating is not None and _valid_int(rating, 1, 10) is None:
        raise ValueError("rating must be a whole number from 1 to 10")
    ts = fm.now_iso()
    meta: dict[str, Any] = {
        "title": title,
        "created": ts,
        "modified": ts,
        MEDIA_KEY: media_type,
        STATUS_KEY: status,
    }
    if creator:
        meta["creator"] = str(creator)
    year = _valid_int(year, 0, 3000)
    if year is not None:
        meta["year"] = year
    if cover:
        meta["cover"] = str(cover)
    if rating is not None:
        meta["rating"] = int(rating)
    if status == "active":
        meta["started"] = today
    elif status == "finished":
        meta["finished"] = today
    total = _valid_int(total, 1, 1_000_000)
    if total is not None:
        meta["total"] = total
    if source:
        meta["source"] = str(source)
    log = ""
    if status == "active":
        log = f"{LOG_HEADING}\n{format_log_line(today, 'started')}\n"
    elif status == "finished":
        text = entry_text(media_type, "finished", rating=rating)
        log = f"{LOG_HEADING}\n{format_log_line(today, text)}\n"
    # The review is the note body, sitting above the ## Log section (set_review
    # replaces exactly this region later), so it can be captured at creation.
    review_body = f"{review.strip()}\n\n" if review and review.strip() else ""
    return fm.serialize(meta, review_body + log)
