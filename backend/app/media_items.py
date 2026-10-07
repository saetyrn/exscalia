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
import unicodedata
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
# A code fence: a run of 3+ backticks or tildes, then the info string.
_FENCE_RE = re.compile(r"^(`{3,}|~{3,})(.*)$")
# Accept em dash, en dash, or hyphen between date and text; emit em dash.
_LOG_LINE_RE = re.compile(r"^-\s+(\d{4}-\d{2}-\d{2})\s+[—–-]\s+(.+?)\s*$")

_PAGE_RE = re.compile(r"^page\s+(\d+)$", re.IGNORECASE)
_EPISODE_RE = re.compile(r"^s(\d{1,2})\s*e(\d{1,3})$", re.IGNORECASE)
_EPISODE_WORD_RE = re.compile(r"^episode\s+(\d+)$", re.IGNORECASE)
_FINISHED_RE = re.compile(r"^finished(?:\s*,\s*rated\s+(\d{1,2}))?$", re.IGNORECASE)
_RATED_RE = re.compile(r"^rated\s+(\d{1,2})$", re.IGNORECASE)
_MAX_ENTRY_TEXT = 300

# Windows is the strictest filesystem the vault syncs to.
_ILLEGAL_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_MAX_SLUG_LEN = 80
# NAME_MAX is 255 bytes; the rest is headroom for ".md"/".paper" and " N".
_MAX_SLUG_BYTES = 200
# Windows refuses these as a stem whatever the extension.
_RESERVED_DEVICE_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{n}" for n in range(1, 10)]
    + [f"LPT{n}" for n in range(1, 10)]
)

def is_media_item(meta: dict) -> bool:
    """True only for a literal, known media type.

    Strict membership, not truthiness: `media: yes` in the editor should get a
    visible ordinary note, not a silently mis-filed tracker item.
    """
    return meta.get(MEDIA_KEY) in MEDIA_TYPES


def media_slug(title: str) -> str:
    """A display title reduced to a filename stem safe on every synced OS.

    The iPad's ``PaperPaths.slug`` implements the same spec, and
    ``tests/fixtures/slug_vectors.json`` is the contract both are tested
    against: NFC; each illegal character and C0 control becomes a space;
    whitespace runs fold to one space, trimmed; at most 80 code points, then
    at most 200 UTF-8 bytes, and only then no trailing dots or spaces; empty
    is "Untitled"; a reserved device name gets a leading "_".

    Media lookups recompute this to find ``media/<slug>.md``. Plain ASCII
    titles slug exactly as they always have; only a reserved device name, a
    title that is not NFC, and one past the byte cap land on a new stem.
    """
    slug = unicodedata.normalize("NFC", title)
    slug = _ILLEGAL_FILENAME_CHARS.sub(" ", slug)
    slug = re.sub(r"\s+", " ", slug).strip()
    slug = slug[:_MAX_SLUG_LEN]
    # surrogatepass: a lone surrogate from a JSON body must not make the
    # slug itself raise.
    while len(slug.encode("utf-8", "surrogatepass")) > _MAX_SLUG_BYTES:
        slug = slug[:-1]
    slug = slug.rstrip(". ") or "Untitled"
    if slug.upper() in _RESERVED_DEVICE_NAMES:
        slug = f"_{slug}"
    return slug


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


def _fence_step(fence: str | None, stripped: str) -> tuple[str | None, bool]:
    """Advance past one stripped line: returns the open fence marker (None
    outside code) and whether the line is a fence or code.

    CommonMark rules, so every scan agrees with the renderer: only a run of the
    opener's character, at least as long and with nothing after it, closes a
    fence — a ~~~ line inside a ``` block is code, not a toggle."""
    m = _FENCE_RE.match(stripped)
    if fence is None:
        # A backtick info string may not hold a backtick: ```x``` is inline code.
        if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
            return m.group(1), True
        return None, False
    run, rest = m.groups() if m else ("", "")
    if run[:1] == fence[0] and len(run) >= len(fence) and not rest.strip():
        return None, True
    return fence, True


def parse_log(body: str) -> list[LogEntry]:
    """Dated entries from the ``## Log`` section, skipping fenced code."""
    entries: list[LogEntry] = []
    fence = None
    in_log = False
    for i, line in enumerate(body.splitlines()):
        stripped = line.strip()
        fence, in_code = _fence_step(fence, stripped)
        if in_code:
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
    # Free text collapses to one line: a newline would split the entry, and a
    # "## " after it would end the Log section.
    if kind == "progress":
        raw = " ".join(str(value or "").split())
        if not raw:
            raise ValueError("progress entries need a value (page or episode)")
        m = _EPISODE_RE.match(raw)
        if m:
            return f"S{int(m.group(1)):02d}E{int(m.group(2)):02d}"
        if raw.isdigit():
            return f"episode {raw}" if media_type == "tv" else f"page {raw}"
        return raw[:_MAX_ENTRY_TEXT].rstrip()
    if kind == "note":
        note = " ".join((text or "").split())
        if not note:
            raise ValueError("note entries need text")
        return note[:_MAX_ENTRY_TEXT].rstrip()
    raise ValueError(f"unknown log entry kind: {kind}")


def format_log_line(at: str, text: str) -> str:
    return f"- {at} — {text}"


def _last_log_heading(lines: list[str]) -> int | None:
    """Index of the last ``## Log`` heading, fences ignored — the log of a note
    saved with an unclosed fence reads as code to a fence-aware scan."""
    return next(
        (i for i in range(len(lines) - 1, -1, -1) if _LOG_HEADING_RE.match(lines[i].strip())),
        None,
    )


def _log_heading_line(lines: list[str]) -> int | None:
    """Index of the ``## Log`` heading, skipping fenced code; None if absent."""
    fence = None
    for i, raw in enumerate(lines):
        stripped = raw.strip()
        fence, in_code = _fence_step(fence, stripped)
        if not in_code and _LOG_HEADING_RE.match(stripped):
            return i
    return _last_log_heading(lines)


def _log_line_count(lines: list[str]) -> int:
    """Entry-shaped lines inside ``## Log`` sections, fences ignored."""
    count = 0
    in_log = False
    for raw in lines:
        stripped = raw.strip()
        if _H2_RE.match(stripped):
            in_log = bool(_LOG_HEADING_RE.match(stripped))
        elif in_log and _LOG_LINE_RE.match(raw):
            count += 1
    return count


def _clean_review(review: str) -> str:
    """The review as stored above the log: no ``## Log`` heading of its own,
    and any unclosed code fence closed so the log after it is not code."""
    text = review.rstrip()
    lines = text.splitlines()
    if any(_LOG_HEADING_RE.match(line.strip()) for line in lines):
        raise ValueError(
            'The review cannot contain a "## Log" heading — that section holds the log'
        )
    fence = None
    for line in lines:
        fence, _ = _fence_step(fence, line.strip())
    return f"{text}\n{fence}" if fence else text


def set_review(content: str, review: str) -> str:
    """Replace the review — everything before ``## Log`` — keeping the log
    (and anything after it) byte-identical."""
    meta, body = fm.parse(content)
    lines = body.splitlines()
    heading = _log_heading_line(lines)
    review_text = _clean_review(review)
    if heading is None:
        new_body = f"{review_text}\n" if review_text else ""
    else:
        preserved = "\n".join(lines[heading:])
        if body.endswith("\n"):
            preserved += "\n"
        prefix = f"{review_text}\n\n" if review_text else ""
        new_body = prefix + preserved
    if _log_line_count(new_body.splitlines()) < _log_line_count(lines):
        raise ValueError("Review edit would drop log entries")
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
    fence = None
    log_start = None
    section_end = None
    for i, raw in enumerate(lines):
        stripped = raw.strip()
        fence, in_code = _fence_step(fence, stripped)
        if in_code:
            continue
        if _H2_RE.match(stripped):
            if log_start is None:
                if _LOG_HEADING_RE.match(stripped):
                    log_start = i
            else:
                section_end = i
                break
    if log_start is None:
        # An unclosed fence above hides the heading; use the section anyway
        # rather than starting a second one.
        log_start = _last_log_heading(lines)
        if log_start is not None:
            section_end = next(
                (i for i in range(log_start + 1, len(lines)) if _H2_RE.match(lines[i].strip())),
                None,
            )
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
    try:
        return date_cls.fromisoformat(str(value)).isoformat()
    except ValueError:
        return None


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
    total: int | None = None,
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

    # Read before the entry block, which can itself flip the status to finished.
    previous_status = meta.get(STATUS_KEY)
    entry_kind = None
    if entry is not None:
        entry_kind = entry.get("kind")
        if entry_kind not in ENTRY_KINDS:
            raise ValueError(f"entry kind must be one of: {', '.join(ENTRY_KINDS)}")
        at = str(entry.get("date") or today)
        try:
            # Stored canonical: fromisoformat also takes 20260803 and 2026-W32-1,
            # which the log grammar and the year filters never match.
            at = date_cls.fromisoformat(at).isoformat()
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
            # Only a real transition logs a finish; finished -> finished is a
            # no-op, and a rewatch is logged explicitly as a watched/finished entry.
            if entry_kind not in ("watched", "finished") and previous_status != "finished":
                text = entry_text(media_type, "finished", rating=rating)
                body = append_log_entry(body, format_log_line(today, text))
            summaries.append("Marked finished")
        else:
            summaries.append(f"Status: {status}")

    if rating is not None:
        meta["rating"] = rating
        summaries.append(f"Rated {rating}/10")

    if total is not None:
        if _valid_int(total, 1, 1_000_000) is None:
            raise ValueError("total must be a whole number from 1 to 1000000")
        meta["total"] = int(total)
        summaries.append(f"Set total to {int(total)}")

    if alias is not None:
        alias = alias.strip()
        if alias:
            meta["alias"] = alias
            summaries.append(f"Aliased to “{alias}”")
        else:
            meta.pop("alias", None)
            summaries.append("Cleared alias")

    if not summaries:
        raise ValueError("Nothing to update: pass a rating, status, total, alias, or log entry")
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
    review_body = f"{_clean_review(review.strip())}\n\n" if review and review.strip() else ""
    return fm.serialize(meta, review_body + log)
