"""OMNIA's read, proposal, direct-action, and confirmed-action registry."""
import asyncio
import difflib
import json
import logging
import math
import re
from datetime import date as date_cls
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Any, Awaitable, Callable

from ..vault import fm
from ..vault import names as vault_names
from ..index.db import content_hash
from ..index.semantic import prefix_list
from ..tz import user_tz
from ..vault.notes import LANDING_GUARD, NoteError
from .habits import (
    HABITS_PATH,
    KEY_CHARS as HABIT_KEY_CHARS,
    VALUE_CHARS as HABIT_VALUE_CHARS,
    mirror as mirror_habits,
    valid_key as valid_habit_key,
    valid_value as valid_habit_value,
)
from .memory import MEMORY_PATH, is_memory_path
from .provider import ToolDef

log = logging.getLogger(__name__)

# agent.py clips every tool result at MAX_TOOL_RESULT_CHARS (30k). Readers that
# can return a lot size themselves below it, so the clip never lands mid-list
# and a page's `returned` / `next_offset` match what the model actually saw.
# agent imports this module, so the relation is pinned in test_read_budget.py.
TOOL_RESULT_BUDGET = 28_000
# read_notes: serialized note entries per call. A single note longer than
# this comes back in slices with `next_offset`.
MAX_READ_CHARS = 26_000

Executor = Callable[[Any, str, dict], Awaitable[dict]]  # (state, session_id, input) -> result

log = logging.getLogger(__name__)


def _err(msg: str) -> dict:
    return {"error": msg}


def _json_len(value) -> int:
    """Characters `value` takes in a tool result, serialized as execute_tool
    does — escapes included, so a note full of newlines and quotes counts true."""
    return len(json.dumps(value, ensure_ascii=False))


# --- the user's calendar days -------------------------------------------------
# Note and task stamps are UTC (fm.now_iso, iso_utc); the dates the model asks
# about are the user's local days (the agent tells it the local date). Compare
# instants, never UTC date prefixes — those file a 9pm New York note under
# tomorrow.

DATE_QUERY_LIMIT = 200


def _utc_bounds(start: date_cls, end: date_cls, tz) -> tuple[str, str]:
    """The local days start..end (inclusive) as a half-open [lo, hi) pair of
    UTC instants shaped like fm.now_iso: hi is the start of the day after end."""

    def instant(day: date_cls) -> str:
        local = datetime(day.year, day.month, day.day, tzinfo=tz)
        return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    return instant(start), instant(end + timedelta(days=1))


def _utc_sql(expr: str) -> str:
    """A stored stamp in _utc_bounds' shape, so the two compare lexically.
    Task stamps carry microseconds, and imported frontmatter can hold
    `2026-09-21` or `2026-09-21 18:00:00-04:00`; anything unparseable is ''."""
    return f"COALESCE(strftime('%Y-%m-%dT%H:%M:%SZ', {expr}), '')"


# --- read tools -------------------------------------------------------------


async def semantic_search(state, session_id, inp) -> dict:
    if not state.semantic.available:
        return _err("Semantic index is not available.")
    k = min(int(inp.get("k", 8)), 20)
    path_prefix = inp.get("path_prefix") or None
    exclude_prefix = inp.get("exclude_prefix") or None
    results = await state.semantic.search(
        inp["query"], k=k, path_prefix=path_prefix, exclude_prefix=exclude_prefix
    )
    out: dict = {
        "results": [
            {
                "path": r["path"],
                "heading": r["heading"],
                "excerpt": r["text"][:800],
                "score": r["score"],
            }
            for r in results
        ]
    }
    if not out["results"]:
        out["note"] = (
            "No semantic matches"
            + scope_phrase(path_prefix, exclude_prefix)
            + ". Try keyword_search on a distinctive word, "
            "or read_structure to see what the vault contains."
        )
    return out


def scope_phrase(path_prefix, exclude_prefix) -> str:
    """Name the folder scope in an empty-result note. Without it the model
    cannot tell an empty vault from a filter it set two calls ago."""
    parts = []
    if path_prefix:
        inside = path_prefix if isinstance(path_prefix, list) else [path_prefix]
        parts.append("under " + " or ".join(f"“{p}”" for p in inside))
    if exclude_prefix:
        outside = exclude_prefix if isinstance(exclude_prefix, list) else [exclude_prefix]
        parts.append("excluding " + " or ".join(f"“{p}”" for p in outside))
    return " " + ", ".join(parts) if parts else ""


async def keyword_search(state, session_id, inp) -> dict:
    db = state.db
    query = inp.get("query")
    tag = inp.get("tag")
    limit = min(int(inp.get("limit", 30)), 100)
    offset = max(int(inp.get("offset", 0)), 0)
    if query:
        # The tag must filter INSIDE the query. Filtering the top-N in Python
        # afterwards returned nothing whenever the tagged match ranked below
        # the limit — "notes tagged #ml about gradients" came back empty.
        tag_clause = (
            " AND f.path IN (SELECT path FROM tags WHERE tag = ?)" if tag else ""
        )
        sql = f"""SELECT f.path, n.title,
                         snippet(notes_fts, 2, '>>', '<<', ' … ', 20) AS snippet
                  FROM notes_fts f JOIN notes n ON n.path = f.path
                  WHERE notes_fts MATCH ? AND f.path NOT LIKE 'templates/%'
                        {tag_clause}
                  ORDER BY rank LIMIT ? OFFSET ?"""

        async def run(match: str):
            params = [match] + ([tag] if tag else []) + [limit, offset]
            return await db.execute_fetchall(sql, tuple(params))

        try:
            rows = await run(query)
        except Exception:
            rows = await run('"' + query.replace('"', '""') + '"')
        results = [dict(r) for r in rows]
        out = {
            "results": results,
            "offset": offset,
            "more_available": len(results) == limit,
        }
        if not results and not offset:
            out["note"] = await _empty_keyword_note(db, query, tag)
        return out
    if tag:
        rows = await db.execute_fetchall(
            """SELECT n.path, n.title, n.modified FROM tags t
               JOIN notes n ON n.path = t.path
               WHERE t.tag = ? AND t.path NOT LIKE 'templates/%'
               ORDER BY n.modified DESC LIMIT ?""",
            (tag, limit),
        )
        out = {"results": [dict(r) for r in rows]}
        if not out["results"]:
            out["note"] = f"No note carries the tag #{tag}."
        return out
    return _err("Provide `query` and/or `tag`.")


async def _empty_keyword_note(db, query: str, tag: str | None) -> str:
    """Say whether the tag or the query text is what failed — an empty result
    alone doesn't tell the model which filter to change."""
    if tag:
        tagged = await db.execute_fetchall(
            "SELECT 1 FROM tags WHERE tag = ? LIMIT 1", (tag,)
        )
        if not tagged:
            return f"No note carries the tag #{tag} — the tag is the problem, not the query."
        return (
            f"Notes tagged #{tag} exist, but none also match “{query}” — "
            "drop the query or the tag."
        )
    return (
        f"No full-text match for “{query}”. Try fewer or different words, "
        "or semantic_search for meaning rather than exact wording."
    )


async def query_by_date(state, session_id, inp) -> dict:
    field = inp.get("field", "modified")
    if field not in ("created", "modified"):
        return _err("field must be 'created' or 'modified'")
    try:
        start_d = date_cls.fromisoformat(str(inp.get("start") or ""))
        end_d = date_cls.fromisoformat(str(inp.get("end") or ""))
        lo, hi = _utc_bounds(start_d, end_d, user_tz(state))
    except (ValueError, OverflowError):
        return _err("start and end must be YYYY-MM-DD")
    if end_d < start_d:
        return _err("end is before start")
    start, end = start_d.isoformat(), end_d.isoformat()
    offset = max(int(inp.get("offset") or 0), 0)
    rows = await state.db.execute_fetchall(
        f"""SELECT path, title, created, modified FROM notes
            WHERE {_utc_sql(field)} >= ? AND {_utc_sql(field)} < ?
              AND path NOT LIKE 'templates/%'
            ORDER BY {field} DESC, path LIMIT ? OFFSET ?""",
        (lo, hi, DATE_QUERY_LIMIT + 1, offset),
    )
    results = [dict(r) for r in rows[:DATE_QUERY_LIMIT]]
    out = {
        "results": results,
        "offset": offset,
        "returned": len(results),
        "more_available": len(rows) > DATE_QUERY_LIMIT,
    }
    if not results and not offset:
        out["note"] = (
            f"No notes {field} between {start} and {end}. Widen the range, "
            f"or switch field to '{'modified' if field == 'created' else 'created'}'."
        )
    return out


async def read_structure(state, session_id, inp) -> dict:
    """Structured vault view: paths + frontmatter + tags. For organizing."""
    db = state.db
    paths = await state.pending.effective_paths()
    meta_rows = await db.execute_fetchall(
        "SELECT path, title, created, modified, pinned, "
        f"{_utc_sql('modified')} AS modified_utc FROM notes"
    )
    meta = {r["path"]: dict(r) for r in meta_rows}
    tag_rows = await db.execute_fetchall("SELECT path, tag FROM tags")
    tags_by_path: dict[str, list[str]] = {}
    for r in tag_rows:
        tags_by_path.setdefault(r["path"], []).append(r["tag"])

    pending = await state.pending.list_changes()
    pending_paths = {c.path for c in pending} | {c.new_path for c in pending if c.new_path}

    # Filters and paging. Without them this returns the whole vault and is then
    # sliced mid-JSON by the tool-result cap, leaving the model an arbitrary
    # alphabetical prefix with no idea the rest exists.
    folder = (inp.get("folder") or "").strip("/")
    want_tag = inp.get("tag")
    modified_since = None
    if inp.get("modified_since"):
        try:
            since_d = date_cls.fromisoformat(str(inp["modified_since"]))
            modified_since, _ = _utc_bounds(since_d, since_d, user_tz(state))
        except (ValueError, OverflowError):
            return _err("modified_since must be YYYY-MM-DD")
    untagged_only = bool(inp.get("untagged_only"))
    limit = max(1, min(int(inp.get("limit", 150)), 1000))
    offset = max(int(inp.get("offset", 0)), 0)

    selected = []
    for p in paths:
        if folder and not p.startswith(f"{folder}/"):
            continue
        note_tags = tags_by_path.get(p, [])
        if want_tag and want_tag.lstrip("#") not in note_tags:
            continue
        if untagged_only and note_tags:
            continue
        if modified_since and ((meta.get(p) or {}).get("modified_utc") or "") < modified_since:
            continue
        selected.append(p)

    # A page is also cut to what fits one tool result (entries run ~140-230
    # chars). The header is counted from what was kept: reporting the asked-for
    # page while the clip cut its tail sent the next offset past the lost notes.
    notes = []
    used = 0
    for p in selected[offset : offset + limit]:
        entry: dict = {"path": p}
        if p in meta:
            entry.update(
                {
                    "title": meta[p]["title"],
                    "created": meta[p]["created"],
                    "modified": meta[p]["modified"],
                }
            )
        entry["tags"] = tags_by_path.get(p, [])
        if p in meta and meta[p]["pinned"]:
            entry["pinned"] = True
        if p in pending_paths:
            entry["has_pending_change"] = True
        size = _json_len(entry) + 2  # ", " between entries
        if notes and used + size > TOOL_RESULT_BUDGET - 1_000:  # header headroom
            break
        used += size
        notes.append(entry)
    more = offset + len(notes) < len(selected)
    return {
        "note_count": len(selected),
        "returned": len(notes),
        "offset": offset,
        "more_available": more,
        "next_offset": offset + len(notes) if more else None,
        "notes": notes,
    }


async def read_notes(state, session_id, inp) -> dict:
    paths = inp.get("paths") or []
    if not isinstance(paths, list) or not paths:
        return _err("Provide `paths`: a list of note paths.")
    offset = max(int(inp.get("offset") or 0), 0)
    max_chars = max(1, min(int(inp.get("max_chars") or MAX_READ_CHARS), MAX_READ_CHARS))
    if len(paths) > 1 and (offset or inp.get("max_chars")):
        return _err("offset and max_chars page through one note; pass a single path.")
    out = []
    total = 0
    for p in paths[:20]:
        try:
            content = await state.pending.effective_content(p)
        except NoteError as e:
            out.append({"path": p, "error": e.detail})
            continue
        if content is None:
            out.append({"path": p, "error": "not found"})
            continue
        # The revision leads, so no clip can cut it off, and it always covers
        # the whole note: a slice still unlocks the revision-gated edit tools.
        entry = {"path": p, "revision": content_hash(content), "content": content}
        if len(paths) == 1:
            out.append(_note_slice(entry, offset, max_chars))
            break
        size = _json_len(entry) + 2  # ", " between entries
        if total + size > MAX_READ_CHARS:
            out.append({
                "path": p,
                "error": "too long to read with other notes; read it on its own to page through it"
                if size > MAX_READ_CHARS
                else "read budget exceeded; read it in another call",
            })
            continue
        total += size
        out.append(entry)
    return {"notes": out}


def _note_slice(entry: dict, offset: int, max_chars: int) -> dict:
    """The note, or the part from `offset` that fits one read. Anything short
    of the whole note says where it stands and where to continue."""
    content = entry["content"]
    total = len(content)
    if offset and offset >= total:
        return {
            "path": entry["path"],
            "error": f"offset {offset} is past the end of the note ({total} characters)",
        }
    room = MAX_READ_CHARS - _json_len({**entry, "content": ""}) - 120  # slice keys
    end = min(total, offset + max_chars)
    while end > offset + 1:
        # Escapes make the serialized slice longer than the raw one; each raw
        # character dropped takes at least one serialized character with it.
        over = _json_len(content[offset:end]) - room
        if over <= 0:
            break
        end = max(offset + 1, end - over)
    if offset == 0 and end == total:
        return entry
    return {
        **entry,
        "content": content[offset:end],
        "offset": offset,
        "total_chars": total,
        "next_offset": end if end < total else None,
        "truncated": end < total,
    }


# --- habit / preference store (the learning layer) --------------------------
#
# Every write mirrors the whole table into the vault (see .habits): the table
# itself is in the index, which is documented as rebuildable, and nothing else
# in the app holds what the user answered.

HABIT_SET_CONFIDENCE = 0.85
HABIT_CONFIRM_BUMP = 0.05
HABIT_CONTRADICT_DROP = 0.25


async def read_habits(state, session_id, inp) -> dict:
    rows = await state.db.execute_fetchall(
        "SELECT key, value, confidence, source, last_seen, times_confirmed"
        " FROM habits ORDER BY confidence DESC, key"
    )
    return {"habits": [dict(r) for r in rows]}


async def _record_habit(state, session_id: str, summary: str, result: dict) -> dict:
    """Every habit write as an action: a card, an entry in the action history,
    and an Undo. The rows ride in every later system prompt, so a write nobody
    can see or take back is a standing instruction nobody gave. The mirror was
    just written, so the scoped commit of it is the write itself."""
    if not hasattr(state, "actions"):
        return result
    return await state.actions.record(
        thread_id=session_id,
        tool="update_habit",
        summary=summary,
        paths=[HABITS_PATH],
        refresh=["habits"],
        result=result,
    )


async def update_habit(state, session_id, inp) -> dict:
    key = str(inp.get("key") or "").strip()
    event = inp.get("event")
    if not key:
        return _err("Provide `key`.")
    if event not in ("set", "confirm", "contradict", "delete"):
        return _err("`event` must be set, confirm, contradict, or delete")
    db = state.db
    now = fm.now_iso()
    cur = await db.execute("SELECT * FROM habits WHERE key = ?", (key,))
    row = await cur.fetchone()

    if event == "delete":
        if not row:
            return {"key": key, "deleted": False, "note": "No habit has that key."}
        await db.execute("DELETE FROM habits WHERE key = ?", (key,))
        await db.commit()
        await mirror_habits(state)
        return await _record_habit(
            state, session_id, f"Deleted habit {key}", {"key": key, "deleted": True}
        )

    if event == "set":
        value = inp.get("value")
        if value is None:
            return _err("`set` needs a `value`.")
        value = str(value).strip()
        # Held on set only: delete must still reach a key stored before the
        # limits existed.
        if not valid_habit_key(key):
            return _err(
                f"`key` must be one dotted token of at most {HABIT_KEY_CHARS} "
                "characters, like 'daily_note.logs_hours'."
            )
        if not valid_habit_value(value):
            return _err(
                f"`value` must be one line of at most {HABIT_VALUE_CHARS} "
                "characters. Keep prose for remember()."
            )
        await db.execute(
            """INSERT INTO habits(key, value, confidence, source, last_seen, times_confirmed)
               VALUES (?,?,?,?,?,0)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                 confidence=excluded.confidence, source=excluded.source,
                 last_seen=excluded.last_seen""",
            (key, value, HABIT_SET_CONFIDENCE, str(inp.get("source") or "user"), now),
        )
        await db.commit()
        await mirror_habits(state)
        return await _record_habit(
            state,
            session_id,
            f"Set habit {key}: {value}",
            {"key": key, "value": value, "confidence": HABIT_SET_CONFIDENCE},
        )

    if not row:
        return _err(f"No habit with key '{key}' — use event=set first.")
    if event == "confirm":
        conf = min(1.0, row["confidence"] + HABIT_CONFIRM_BUMP)
        confirmed = row["times_confirmed"] + 1
        summary = f"Confirmed habit {key}"
    else:  # contradict
        conf = max(0.05, row["confidence"] - HABIT_CONTRADICT_DROP)
        confirmed = row["times_confirmed"]
        summary = f"Lowered confidence in habit {key}"
    await db.execute(
        "UPDATE habits SET confidence=?, times_confirmed=?, last_seen=? WHERE key=?",
        (conf, confirmed, now, key),
    )
    await db.commit()
    await mirror_habits(state)
    return await _record_habit(
        state,
        session_id,
        summary,
        {
            "key": key,
            "value": row["value"],
            "confidence": round(conf, 3),
            "times_confirmed": confirmed,
            "note": "confidence low — consider re-asking the user once" if conf < 0.4 else "",
        },
    )


# --- tag analysis ------------------------------------------------------------


def _norm_tag(tag: str) -> str:
    t = tag.lower().replace("-", "").replace("_", "").replace(" ", "")
    return t[:-1] if t.endswith("s") and len(t) > 3 else t


async def analyze_tags(state, session_id, inp) -> dict:
    rows = await state.db.execute_fetchall(
        """SELECT tag, COUNT(*) AS count FROM tags
           WHERE path NOT LIKE 'templates/%' GROUP BY tag ORDER BY count DESC"""
    )
    tags = [dict(r) for r in rows]
    # Cluster likely-duplicate tags: same normalized form, or high string similarity.
    clusters: dict[str, list[dict]] = {}
    for t in tags:
        clusters.setdefault(_norm_tag(t["tag"]), []).append(t)
    similar = [v for v in clusters.values() if len(v) > 1]
    names = [t["tag"] for t in tags]
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            if _norm_tag(a) != _norm_tag(b) and difflib.SequenceMatcher(None, a, b).ratio() >= 0.85:
                similar.append([t for t in tags if t["tag"] in (a, b)])
    return {"tags": tags, "possible_duplicates": similar}


async def suggest_tags(state, session_id, inp) -> dict:
    path = inp.get("path")
    if not path:
        return _err("Provide `path`.")
    content = await state.pending.effective_content(path)
    if content is None:
        return _err(f"{path} does not exist.")
    _meta, body = fm.parse(content)

    existing = [
        r["tag"]
        for r in await state.db.execute_fetchall(
            "SELECT tag FROM tags WHERE path = ?", (path,)
        )
    ]
    neighbor_tags: dict[str, int] = {}
    if state.semantic.available:
        results = await state.semantic.search(body[:2000], k=8)
        neighbor_paths = {r["path"] for r in results if r["path"] != path}
        for np in neighbor_paths:
            for r in await state.db.execute_fetchall(
                "SELECT tag FROM tags WHERE path = ?", (np,)
            ):
                neighbor_tags[r["tag"]] = neighbor_tags.get(r["tag"], 0) + 1
    prefs = await state.db.execute_fetchall(
        "SELECT key, value, confidence FROM habits WHERE key LIKE '%tag%'"
    )
    return {
        "existing_tags": existing,
        "similar_notes_tags": sorted(
            ({"tag": t, "notes": c} for t, c in neighbor_tags.items()),
            key=lambda x: -x["notes"],
        ),
        "tag_preferences": [dict(r) for r in prefs],
        "note": "Also consider candidates from the note's own headings and key terms. "
        "Tags are added as inline #hashtags via edit_note.",
    }


# --- backlinks, digests, duplicates ------------------------------------------


async def get_backlinks(state, session_id, inp) -> dict:
    path = inp.get("path")
    if not path:
        return _err("Provide `path`.")
    rows = await state.db.execute_fetchall(
        """SELECT DISTINCT l.source AS path, n.title FROM links l
           JOIN notes n ON n.path = l.source
           WHERE l.resolved_path = ? AND l.source != ?""",
        (path, path),
    )
    return {"backlinks": [dict(r) for r in rows]}


async def get_digest_data(state, session_id, inp) -> dict:
    days = max(1, min(int(inp.get("days", 1)), 60))
    tz = user_tz(state)
    today = datetime.now(tz).date()
    # The window opens at the start of the user's local day `days` ago.
    since, _ = _utc_bounds(today - timedelta(days=days), today, tz)
    today_s = today.isoformat()
    db = state.db

    open_tasks = [
        dict(r)
        for r in await db.execute_fetchall(
            # board = 0: the morning digest answers "what is on my plate
            # today", the same scope as the universal board. A folder board is
            # a workspace you open on purpose.
            """SELECT path, text, due FROM tasks
               WHERE status != 'done' AND board = 0
                 AND path NOT LIKE 'templates/%'
               ORDER BY due IS NULL, due"""
        )
    ]
    buckets = {"overdue": [], "due_today": [], "upcoming": [], "no_due_date": []}
    for t in open_tasks:
        if not t["due"]:
            buckets["no_due_date"].append(t)
        elif t["due"] < today_s:
            buckets["overdue"].append(t)
        elif t["due"] == today_s:
            buckets["due_today"].append(t)
        else:
            buckets["upcoming"].append(t)

    created = await db.execute_fetchall(
        f"""SELECT path, title FROM notes
           WHERE {_utc_sql('created')} >= ? AND path NOT LIKE 'templates/%'
           ORDER BY created DESC LIMIT 40""",
        (since,),
    )
    modified = await db.execute_fetchall(
        f"""SELECT path, title FROM notes
           WHERE {_utc_sql('modified')} >= ? AND {_utc_sql('created')} < ?
             AND path NOT LIKE 'templates/%'
           ORDER BY modified DESC LIMIT 40""",
        (since, since),
    )
    # A task counts when it was finished in the window. The note's own
    # `modified` is only the fallback for a task with no completed_at stamp:
    # as the criterion it credited every done task in any note touched
    # recently — one new line in Tasks Inbox.md claimed its whole history.
    # Folder-board completions stay in, as on the universal board and the
    # calendar: only a board's open tasks are kept off the global views.
    done_where = f"""t.status = 'done'
             AND {_utc_sql('COALESCE(t.completed_at, n.modified)')} >= ?
             AND t.path NOT LIKE 'templates/%'"""
    done_recent = await db.execute_fetchall(
        f"""SELECT t.path, t.text, t.completed_at
           FROM tasks t JOIN notes n ON n.path = t.path
           WHERE {done_where}
           ORDER BY COALESCE(t.completed_at, n.modified) DESC LIMIT 40""",
        (since,),
    )
    done_count = await db.execute_fetchall(
        f"""SELECT COUNT(*) AS c FROM tasks t JOIN notes n ON n.path = t.path
           WHERE {done_where}""",
        (since,),
    )
    event_digest = (
        state.events.digest(today, mark_surfaced=False)
        if hasattr(state, "events")
        else {"events_today": [], "reminders": []}
    )
    upcoming_events = (
        state.events.occurrences(today, today + timedelta(days=days))
        if hasattr(state, "events")
        else []
    )
    return {
        "period_days": days,
        "today": today_s,
        "tasks": {k: v[:40] for k, v in buckets.items()},
        "notes_created": [dict(r) for r in created],
        "notes_modified": [dict(r) for r in modified],
        "tasks_completed_recently": [dict(r) for r in done_recent],
        "tasks_completed_count": done_count[0]["c"],
        "events_today": event_digest["events_today"],
        "event_reminders": [
            reminder
            for reminder in event_digest["reminders"]
            if not reminder.get("overdue")
        ],
        "missed_event_reminders": [
            reminder
            for reminder in event_digest["reminders"]
            if reminder.get("overdue")
        ],
        "upcoming_events": upcoming_events,
    }


# Folders whose notes share a skeleton by design: every unfilled daily note
# carries the same template body, media items one layout, and omnia/ is
# OMNIA's own memory. An unscoped scan ranked those pairs as its top
# "duplicates", so it skips them; a path_prefix inside one scans it.
DUPLICATE_SKIP = ("daily/", "media/", "omnia/")
DUPLICATE_CAP = 300
DUPLICATE_OVERLAP = 0.35


async def find_duplicates(state, session_id, inp) -> dict:
    if not state.semantic.available:
        return _err("Semantic index is not available — cannot scan for duplicates.")
    threshold = float(inp.get("similarity_threshold", 0.75))
    included = prefix_list(inp.get("path_prefix"))
    skipped = [*([] if included else DUPLICATE_SKIP), *prefix_list(inp.get("exclude_prefix"))]
    # One scope for the note list, the cap and every vector search alike.
    excluded = ("templates/", *skipped)
    notes = [
        n
        for n in state.store.list_notes()
        if (not included or n.path.startswith(tuple(included)))
        and not n.path.startswith(excluded)
    ]
    scope = scope_phrase(included, skipped)
    if len(notes) > DUPLICATE_CAP:
        return _err(
            f"{len(notes)} notes{scope} — one scan covers at most {DUPLICATE_CAP}. "
            "Narrow it with path_prefix (a folder, e.g. 'School/') or exclude_prefix."
        )
    bodies: dict[str, str] = {}
    for n in notes:
        content = await state.pending.effective_content(n.path)
        if content:
            bodies[n.path] = fm.parse(content)[1]
    seen: set[tuple[str, str]] = set()
    neighbours: list[tuple[tuple[str, str], float]] = []
    for path, body in bodies.items():
        if not body.strip():
            continue
        hits = await state.semantic.search(
            body[:2000], k=4, path_prefix=included or None, exclude_prefix=list(excluded)
        )
        for r in hits:
            other = r["path"]
            if other == path or other not in bodies:
                continue
            key = tuple(sorted((path, other)))
            if key in seen or r["score"] < threshold:
                continue
            seen.add(key)
            neighbours.append((key, r["score"]))

    def confirm() -> list[dict]:
        # SequenceMatcher.ratio() is quadratic in the worst case over whole
        # bodies, so it runs off the event loop, behind the cheap upper bounds
        # (the same cascade as difflib.get_close_matches).
        pairs = []
        for (a, b), score in neighbours:
            matcher = difflib.SequenceMatcher(None, bodies[a], bodies[b])
            if (
                matcher.real_quick_ratio() < DUPLICATE_OVERLAP
                or matcher.quick_ratio() < DUPLICATE_OVERLAP
            ):
                continue
            overlap = matcher.ratio()
            if overlap >= DUPLICATE_OVERLAP:
                pairs.append(
                    {"a": a, "b": b, "similarity": score, "content_overlap": round(overlap, 3)}
                )
        return pairs

    pairs = await asyncio.to_thread(confirm)
    if not pairs:
        if not bodies:
            note = f"No notes to scan{scope}. " + (
                "Check the folder with read_structure."
                if included
                else "Pass path_prefix to scan daily/, media/ or omnia/."
            )
        else:
            note = (
                f"No near-duplicate pairs among {len(bodies)} notes{scope} "
                f"(similarity ≥ {threshold}, content overlap ≥ {DUPLICATE_OVERLAP}). "
                "A lower similarity_threshold widens the net"
                + ("." if included else "; path_prefix scans daily/, media/ or omnia/.")
            )
        return {"candidates": [], "note": note}
    pairs.sort(key=lambda p: -(p["similarity"] + p["content_overlap"]))
    return {
        "candidates": pairs[:20],
        "note": "Verify with read_notes before proposing a merge. A merge = one "
        "session staging: the merged note, inbound-link rewrites (use "
        "get_backlinks), and deletion of the originals.",
    }


# --- single-use graphics ----------------------------------------------------

GRAPHIC_TYPES = ("bar", "line", "pie", "heatmap")
MAX_SERIES = 8
MAX_POINTS = 50
MAX_HEATMAP = 30


def validate_graphic(inp: dict) -> tuple[dict | None, str | None]:
    """Normalize a show_graphic input into a renderable spec, or explain why not."""

    def nums(seq) -> list[float]:
        out = []
        for v in seq:
            f = float(v)
            if not math.isfinite(f):
                raise ValueError("values must be finite numbers")
            out.append(f)
        return out

    gtype = inp.get("type")
    if gtype not in GRAPHIC_TYPES:
        return None, f"type must be one of {', '.join(GRAPHIC_TYPES)}"
    spec: dict = {"type": gtype, "title": str(inp.get("title") or "")[:120]}
    for k in ("x_title", "y_title"):
        if inp.get(k):
            spec[k] = str(inp[k])[:60]

    def labels(raw, n: int) -> list[str]:
        given = [str(s)[:24] for s in (raw or [])][:n]
        return given + [str(i + 1) for i in range(len(given), n)]

    try:
        if gtype == "heatmap":
            cells = inp.get("cells")
            if not cells or not isinstance(cells, list) or not isinstance(cells[0], list):
                return None, "heatmap needs `cells`: a 2-D array of numbers (rows × columns)"
            width = len(cells[0])
            if len(cells) > MAX_HEATMAP or width > MAX_HEATMAP:
                return None, f"heatmap is capped at {MAX_HEATMAP}×{MAX_HEATMAP} cells"
            rows = []
            for row in cells:
                if not isinstance(row, list) or len(row) != width:
                    return None, "`cells` rows must all be the same length"
                rows.append(nums(row))
            spec["cells"] = rows
            spec["x_labels"] = labels(inp.get("x_labels"), width)
            spec["y_labels"] = labels(inp.get("y_labels"), len(rows))
        else:
            series = inp.get("series")
            if not series or not isinstance(series, list):
                return None, 'needs `series`: [{"name": …, "values": [numbers]}]'
            if gtype == "pie":
                series = series[:1]
            if len(series) > MAX_SERIES:
                return None, f"at most {MAX_SERIES} series"
            norm = []
            n = 0
            for s in series:
                vals = nums((s or {}).get("values") or [])
                if not vals:
                    return None, "each series needs a non-empty numeric `values` array"
                if len(vals) > MAX_POINTS:
                    return None, f"at most {MAX_POINTS} values per series"
                if gtype == "pie" and any(v < 0 for v in vals):
                    return None, "pie values must be non-negative"
                n = max(n, len(vals))
                norm.append({"name": str((s or {}).get("name") or "")[:40], "values": vals})
            spec["series"] = norm
            spec["x_labels"] = labels(inp.get("x_labels"), n)
    except (ValueError, TypeError) as e:
        return None, str(e)
    return spec, None


async def show_graphic(state, session_id, inp) -> dict:
    spec, err = validate_graphic(inp)
    if err:
        return _err(err)
    # The agent loop lifts `_spec` out for the UI and hands the model a bare ack.
    return {"displayed": True, "_spec": spec}


# --- tool domains (the use_tools escape hatch) ------------------------------

# The registry is far past the API's 128-tool ceiling, so not everything can be
# offered at once. It used to be scored per message and trimmed to the best
# 120 — which meant the tool array changed shape between two ordinary messages
# (36 tools of drift, measured), throwing away the prompt cache the system
# prompt is built around, AND silently dropping abilities that same prompt
# advertises by name whenever the wording missed them.
#
# So the array is fixed instead: a deterministic base set every turn, with the
# genuinely niche tools parked in these domains. One use_tools call brings a
# domain in for the rest of the turn. Every registry tool is either in the base
# set or in exactly one domain here — a test enforces that partition.
ESCAPE_TOOL = "use_tools"

TOOL_DOMAINS: dict[str, tuple[str, frozenset[str]]] = {
    "appearance": (
        "Themes, fonts, sidebar order, and the media hub profile.",
        frozenset({
            "get_appearance", "set_appearance", "manage_saved_theme",
            "reorder_sidebar", "edit_media_profile",
        }),
    ),
    "maintenance": (
        "Backup status and pushes, search-index rebuild, security overview, "
        "timezone, and OMNIA housekeeping settings.",
        frozenset({
            "get_backup_status", "push_backup", "rebuild_search_index",
            "get_security_overview", "get_timezone", "set_timezone",
            "set_omnia_housekeeping",
        }),
    ),
    "flashcards": (
        "Flashcard decks: create, edit, review, and study statistics.",
        frozenset({
            "list_flashcard_decks", "get_flashcard_deck", "create_flashcard_deck",
            "rename_flashcard_deck", "delete_flashcard_deck", "open_flashcard_deck",
            "add_flashcards", "remove_flashcards", "review_flashcard",
            "flashcards_from_note", "study_stats",
        }),
    ),
    "history": (
        "A note's git version history: inspect, diff, and restore old "
        "versions, plus what is inside the Trash.",
        frozenset({
            "get_note_history", "inspect_note_version", "restore_note_version",
            "restore_note_to", "note_changes", "inspect_trash",
            "get_task_timeline",
        }),
    ),
    "vault_surgery": (
        "Vault-wide operations: find/replace, link repair, exports, templates, "
        "project scaffolding, and timespan summaries.",
        frozenset({
            "find_replace_across_vault", "resolve_unlinked_mentions",
            "find_broken_links", "get_forward_links", "export_notes",
            "save_note_as_template", "list_templates", "scaffold_project",
            "summarize_timespan",
        }),
    ),
    "automation": (
        "Scheduled OMNIA runs, note follow-up reminders, and the structured "
        "daily briefing.",
        frozenset({
            "schedule_agent_run", "manage_scheduled_runs",
            "generate_daily_briefing", "set_note_followup",
            "list_queued_reminders",
        }),
    ),
    "activity": (
        "The Activity tab: logged hours, day/week/month summaries, and manual "
        "entries.",
        frozenset({
            "get_activity_summary", "add_activity", "update_activity",
            "delete_activity",
        }),
    ),
    "attachments": (
        "Attachment bookkeeping: embedding a file into a note, deleting one.",
        frozenset({"embed_attachment", "delete_attachment"}),
    ),
    "proposals": (
        "Staging a delete or a move in the review tray instead of applying it.",
        frozenset({"propose_note_delete", "propose_note_move"}),
    ),
    "paper": (
        "Working with one handwritten Paper page: looking at its render, "
        "transcribing it with the model, and turning it into a markdown note.",
        frozenset({"read_page", "transcribe_page", "note_from_page"}),
    ),
}


def _first_sentence(text: str) -> str:
    """The opening sentence of a tool description — enough for the model to
    pick the right tool without paying for nine full schemas it may not use."""
    head = (text or "").strip().split("\n")[0]
    stop = head.find(". ")
    return head[: stop + 1] if stop != -1 else head


async def use_tools(state, session_id, inp) -> dict:
    domain = str(inp.get("domain") or "").strip()
    entry = TOOL_DOMAINS.get(domain)
    if not entry:
        return _err(
            f"Unknown domain: {domain}. Available: {', '.join(TOOL_DOMAINS)}."
        )
    _description, names = entry
    revealed = [t for t in TOOL_DEFS if t.name in names]
    return {
        "domain": domain,
        "tools": [
            {"name": t.name, "description": _first_sentence(t.description)}
            for t in revealed
        ],
        "note": "These tools are now available this turn.",
        # The agent loop lifts the marker out and extends this turn's tool
        # array with the full definitions; the model never sees the key.
        "_reveal_tools": [t.name for t in revealed],
    }


# --- registry ---------------------------------------------------------------


def _schema(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required}

TOOLS: list[tuple[ToolDef, Executor]] = [
    (
        ToolDef(
            "semantic_search",
            "Search the vault by MEANING. Returns the most relevant note chunks with "
            "source paths. Use for questions and content discovery. Do NOT use for "
            "organizing/sorting/deduping — use read_structure for that.",
            _schema(
                {
                    "query": {"type": "string"},
                    "k": {"type": "integer", "description": "Max results (default 8, max 20)"},
                    "path_prefix": {
                        "type": "string",
                        "description": "Only notes under this folder prefix, e.g. 'School/'",
                    },
                    "exclude_prefix": {
                        "type": "string",
                        "description": "Skip notes under this folder prefix, e.g. 'media/'",
                    },
                },
                ["query"],
            ),
        ),
        semantic_search,
    ),
    (
        ToolDef(
            "keyword_search",
            "Exact-word full-text search and/or tag filter over all notes.",
            _schema(
                {
                    "query": {"type": "string", "description": "FTS query (words, phrases)"},
                    "tag": {"type": "string", "description": "Filter to notes with this tag"},
                    "limit": {"type": "integer", "description": "Max results (default 30, max 100)"},
                    "offset": {
                        "type": "integer",
                        "description": "Skip this many results, for paging past the limit",
                    },
                },
                [],
            ),
        ),
        keyword_search,
    ),
    (
        ToolDef(
            "query_by_date",
            "Find notes created or modified in a date range (inclusive). Dates are "
            "the user's local calendar days. Returns up to 200 notes, newest first.",
            _schema(
                {
                    "start": {"type": "string", "description": "YYYY-MM-DD, local day"},
                    "end": {"type": "string", "description": "YYYY-MM-DD, local day"},
                    "field": {"type": "string", "enum": ["created", "modified"]},
                    "offset": {
                        "type": "integer",
                        "description": "Skip this many notes, for paging past the first 200",
                    },
                },
                ["start", "end"],
            ),
        ),
        query_by_date,
    ),
    (
        ToolDef(
            "read_structure",
            "Structured view of the whole vault: every note's path, title, tags, and "
            "created/modified timestamps. THE tool for organizing, filing, sorting and "
            "deduplicate work (use semantic_search only for meaning-based lookup).",
            _schema(
                {
                    "folder": {
                        "type": "string",
                        "description": "Only notes inside this folder, e.g. 'School'",
                    },
                    "tag": {"type": "string", "description": "Only notes carrying this tag"},
                    "untagged_only": {
                        "type": "boolean",
                        "description": "Only notes with no tags at all",
                    },
                    "modified_since": {
                        "type": "string",
                        "description": "Only notes modified on or after this local day, "
                        "YYYY-MM-DD",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max notes (default 150, max 1000); a page is "
                        "also cut to what fits one result",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Skip this many notes, for paging through a large "
                        "vault: pass the previous page's next_offset",
                    },
                },
                [],
            ),
        ),
        read_structure,
    ),
    (
        ToolDef(
            "read_notes",
            "Read up to 20 notes by path, each with its revision. Several notes share "
            "one read budget. A single note too long for one read comes back in slices: "
            "call again with offset set to the returned next_offset until it is null.",
            _schema(
                {
                    "paths": {"type": "array", "items": {"type": "string"}},
                    "offset": {
                        "type": "integer",
                        "description": "One path only: start at this character, from a "
                        "previous next_offset",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "One path only: at most this many characters "
                        f"(default and max {MAX_READ_CHARS})",
                    },
                },
                ["paths"],
            ),
        ),
        read_notes,
    ),
    (
        ToolDef(
            "read_habits",
            "Read the habit/preference store — everything learned about the user so "
            "far (each fact has a confidence and confirmation count). Check this "
            "before asking the user a question you may already have the answer to.",
            _schema({}, []),
        ),
        read_habits,
    ),
    (
        ToolDef(
            "update_habit",
            "Record or adjust a learned fact about the user. event=set stores a new "
            "answer (after asking the user, or a clearly stated preference); "
            "event=confirm when observed behavior matches the fact; event=contradict "
            "when it doesn't; event=delete removes it. Keys are dotted paths like "
            "'daily_note.logs_hours' or 'tags.canonical.ml'; a value is one short "
            "line.",
            _schema(
                {
                    "key": {"type": "string"},
                    "value": {"type": "string"},
                    "event": {
                        "type": "string",
                        "enum": ["set", "confirm", "contradict", "delete"],
                    },
                    "source": {"type": "string"},
                },
                ["key", "event"],
            ),
            # This writes persistent claims about the user, which ride in every
            # later system prompt. Direct: it runs without a confirmation card,
            # but each write is a recorded action with an Undo (the rows are
            # mirrored into habits.json, so the scoped commit carries them).
            # It must never run unattended, which tier="action" enforces both
            # in the tool filter and in the loop. Previously it was plain
            # read/suggestion, so a background morning pass could silently
            # rewrite the user's habit profile.
            mode="direct",
            tier="action",
        ),
        update_habit,
    ),
    (
        ToolDef(
            "analyze_tags",
            "Tag hygiene: every tag with usage counts, plus clusters of "
            "likely-duplicate tags (#ml vs #machine-learning, plural/case/hyphen "
            "variants) to propose canonical merges from.",
            _schema({}, []),
        ),
        analyze_tags,
    ),
    (
        ToolDef(
            "suggest_tags",
            "Tag-suggestion inputs for one note: its current tags, the tags of "
            "semantically similar notes, and the user's learned tag preferences. "
            "Combine with the note's own content to propose tags.",
            _schema({"path": {"type": "string"}}, ["path"]),
        ),
        suggest_tags,
    ),
    (
        ToolDef(
            "get_backlinks",
            "List the notes that link TO a given note. Essential before a merge or "
            "delete: inbound links must be rewritten so nothing breaks.",
            _schema({"path": {"type": "string"}}, ["path"]),
        ),
        get_backlinks,
    ),
    (
        ToolDef(
            "get_digest_data",
            "Raw material for a daily/weekly digest: open tasks bucketed into "
            "overdue / due today / upcoming, recently created and modified notes, "
            "and recently completed tasks. days=1 for a daily digest, 7 for weekly.",
            _schema({"days": {"type": "integer"}}, []),
        ),
        get_digest_data,
    ),
    (
        ToolDef(
            "find_duplicates",
            "Scan the vault for near-duplicate note pairs (semantic similarity "
            "confirmed by content overlap). Skips daily/, media/ and omnia/, "
            "whose notes share a template, unless path_prefix points into one. "
            "One scan covers at most 300 notes; scope a larger vault with "
            "path_prefix or exclude_prefix. Action-tier: run only when the user "
            "explicitly asks to find duplicates or merge notes.",
            _schema(
                {
                    "similarity_threshold": {
                        "type": "number",
                        "description": "0-1, default 0.75",
                    },
                    "path_prefix": {
                        "type": "string",
                        "description": "Only notes under this folder prefix, e.g. 'School/'",
                    },
                    "exclude_prefix": {
                        "type": "string",
                        "description": "Also skip notes under this folder prefix, e.g. 'Archive/'",
                    },
                },
                [],
            ),
            tier="action",
        ),
        find_duplicates,
    ),
    (
        ToolDef(
            "show_graphic",
            "Render a single-use chart inline in the chat — NOT saved to the vault. "
            "Use for quick visualizations of note data: counts per tag, tasks over "
            "time, a habit heatmap. Types: bar/line (series + x_labels), pie (one "
            "series, x_labels as slice names), heatmap (cells matrix + x/y labels).",
            _schema(
                {
                    "type": {"type": "string", "enum": list(GRAPHIC_TYPES)},
                    "title": {"type": "string"},
                    "x_labels": {"type": "array", "items": {"type": "string"}},
                    "y_labels": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Heatmap row labels",
                    },
                    "series": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "values": {"type": "array", "items": {"type": "number"}},
                            },
                            "required": ["values"],
                        },
                    },
                    "cells": {
                        "type": "array",
                        "items": {"type": "array", "items": {"type": "number"}},
                        "description": "Heatmap matrix, rows × columns",
                    },
                    "x_title": {"type": "string"},
                    "y_title": {"type": "string"},
                },
                ["type"],
            ),
        ),
        show_graphic,
    ),
    (
        ToolDef(
            ESCAPE_TOOL,
            "Bring a domain of less-common tools into this turn. Your tool list "
            "holds the everyday set; call this when the ability you need is not "
            "in it, then call the tool it returns. The domain stays available "
            "for the rest of this turn. Domains: "
            + "; ".join(
                f"{name} — {description}"
                for name, (description, _names) in TOOL_DOMAINS.items()
            ),
            _schema(
                {"domain": {"type": "string", "enum": sorted(TOOL_DOMAINS)}},
                ["domain"],
            ),
        ),
        use_tools,
    ),
]

from .tool_expansion import EXPANDED_TOOLS

TOOLS = TOOLS + EXPANDED_TOOLS

TOOL_DEFS = [t for t, _ in TOOLS]

# What a scheduled run or the morning pass is handed. Nobody is watching those,
# so the set is spelled out rather than derived: it used to be every read or
# proposal tool, and a tool registered without mode= is read — so the next one
# with a side effect would have run at 04:00 unasked (update_habit already did
# once). A new read tool sits in neither list until its author picks one;
# test_tool_selection fails until they do.
UNATTENDED_TOOL_NAMES: frozenset[str] = frozenset({
    # notes
    "read_notes", "read_structure", "get_outline", "list_folders",
    "list_templates", "list_attachments", "read_attachment",
    "get_note_history", "inspect_note_version", "note_changes", "list_trash",
    "inspect_trash",
    # search / discovery
    "semantic_search", "keyword_search", "ask_vault", "query_by_date",
    "find_notes", "related_notes", "summarize_notes", "summarize_timespan",
    "vault_overview", "get_backlinks", "get_forward_links",
    "find_broken_links", "find_unlinked_mentions", "analyze_tags",
    "suggest_tags",
    # tasks / calendar
    "list_tasks", "get_task_timeline", "board_digest", "list_events",
    "get_calendar_range", "week_agenda", "find_free_time",
    "list_queued_reminders", "get_digest_data", "generate_daily_briefing",
    "get_activity_summary",
    # media (lookups go out over HTTP, read-only)
    "list_media", "media_details", "media_diary", "media_stats",
    "lookup_media", "similar_media", "search_media_images",
    # paper (transcribe_page stores derived text, budget-gated) / flashcards
    "list_notebooks", "read_page", "search_handwriting", "transcribe_page",
    "list_flashcard_decks", "get_flashcard_deck", "study_stats",
    # OMNIA itself
    "read_habits", "list_conversations", "read_conversation",
    "search_conversations", "list_pending", "list_recent_actions",
    "get_appearance", "get_timezone", "get_backup_status",
    # output: proposals, one staged observation, a push, a chart
    "propose_note_create", "propose_note_edit", "propose_note_move",
    "propose_note_delete", "note_observation", "notify_user", "show_graphic",
})

# Read tools deliberately kept out of unattended passes.
UNATTENDED_EXCLUDED: frozenset[str] = frozenset({
    # A proactive pass runs on the fixed set; revealing a domain mid-pass would
    # hand it action-tier tools the loop then has to refuse call by call.
    ESCAPE_TOOL,
    # Devices, session locations and failed logins: nothing a background pass
    # has a use for.
    "get_security_overview",
})
SUGGESTION_TOOL_DEFS = [t for t, _ in TOOLS if t.name in UNATTENDED_TOOL_NAMES]

TOOL_TIERS: dict[str, str] = {t.name: t.tier for t, _ in TOOLS}
TOOL_MODES: dict[str, str] = {t.name: t.mode for t, _ in TOOLS}
EXECUTORS: dict[str, Executor] = {t.name: fn for t, fn in TOOLS}


# The Anthropic API rejects a request that carries more than 128 tool
# definitions, and the registry has outgrown that. The budget is spent on a
# FIXED base set — the same array on every turn, so the tool schemas stay part
# of the cached prefix — with the niche tools parked in TOOL_DOMAINS and pulled
# in mid-turn by use_tools. Every executor stays reachable either way.
BASE_TOOLS = 113  # registry tools offered at rest (+ use_tools = 114)
MAX_TOOLS = 126  # ceiling once a turn's use_tools reveals are added

# The fundamentals: the first claim on the base budget, ahead of the tools the
# system prompt names and the registry-order fill behind them. Names that no
# longer exist are ignored (a test guards against drift).
CORE_TOOL_NAMES: frozenset[str] = frozenset({
    # notes
    "read_notes", "read_structure", "get_outline", "list_folders",
    "create_note", "edit_note", "append_to_note", "patch_note", "delete_note",
    "rename_note", "move_note", "move_notes", "duplicate_note", "split_note",
    "merge_notes", "archive_note", "add_wikilink", "link_notes", "move_section",
    "edit_frontmatter", "set_note_pin", "create_note_from_template",
    "create_folder", "move_folder",
    # in-note editing
    "edit_selection", "format_selection", "extract_to_note", "insert_toc",
    "sort_list", "format_table", "convert_bullets_to_tasks",
    "extract_action_items",
    # search / discovery
    "semantic_search", "keyword_search", "ask_vault", "query_by_date",
    "related_notes", "find_notes", "get_backlinks", "summarize_notes",
    "vault_overview",
    # tasks
    "create_task", "add_tasks", "update_task", "complete_task", "delete_task",
    "move_task", "list_tasks", "reschedule_task", "set_task_reminder",
    "create_task_board", "roll_over_tasks", "archive_completed_tasks",
    "bulk_edit_tasks", "create_recurring_task", "reorder_tasks",
    # calendar / events
    "create_event", "update_event", "delete_event", "list_events",
    "get_calendar_range", "reschedule_event", "create_reminder",
    "find_free_time", "week_agenda", "ensure_daily_note",
    # media
    "log_media", "bulk_log_media", "update_media", "update_media_by_title",
    "list_media", "lookup_media", "media_details", "media_diary",
    # memory
    "remember", "forget", "note_observation",
    # meta / navigation / awareness
    "open", "close_tabs", "notify_user", "undo_last_action",
    "list_recent_actions", "list_pending", "get_digest_data",
    # tags
    "suggest_tags", "analyze_tags", "bulk_tag",
    # trash
    "list_trash", "restore_trash",
    # paper (handwritten notebooks) — the two entry points; the per-page tools
    # live in the `paper` domain behind use_tools.
    "list_notebooks", "search_handwriting",
})


def hidden_tool_names() -> frozenset[str]:
    """Every tool parked behind use_tools."""
    return frozenset().union(*(names for _desc, names in TOOL_DOMAINS.values()))


def domain_of(name: str) -> str | None:
    """The domain a hidden tool lives in, or None if it is not hidden."""
    for domain, (_description, names) in TOOL_DOMAINS.items():
        if name in names:
            return domain
    return None


def prompt_referenced_tools(*texts: str) -> frozenset[str]:
    """Every registry tool name that appears verbatim in the given prompt text.

    The system prompt advertises abilities by name ("call read_attachment on
    it"). Under relevance scoring those names dropped out of the array whenever
    the user's wording missed them, so the model was told to use tools it had
    not been given — this is what makes them unconditional.
    """
    blob = "\n".join(texts)
    return frozenset(
        t.name
        for t in TOOL_DEFS
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(t.name)}(?![A-Za-z0-9_])", blob)
    )


_BASE_NAMES: frozenset[str] | None = None
_FILL_NAMES: tuple[str, ...] = ()


def _build_base() -> None:
    """Work out the base set once. Deferred rather than computed at import
    because the system prompt lives in .agent, which imports this module."""
    global _BASE_NAMES, _FILL_NAMES
    from .agent import SYSTEM_PROMPT
    from .persona import PERSONA

    registry = [t.name for t in TOOL_DEFS]
    hidden = hidden_tool_names()
    # use_tools is added unconditionally at the end, and the prompt names it —
    # so it must not also spend one of the BASE_TOOLS slots here.
    keep = (
        (CORE_TOOL_NAMES | prompt_referenced_tools(SYSTEM_PROMPT, PERSONA))
        & set(registry)
    ) - {ESCAPE_TOOL}
    # Whatever budget the fundamentals leave goes to the rest of the registry in
    # registration order — arbitrary, but stable, which is the whole point.
    fill = [
        name
        for name in registry
        if name not in keep and name not in hidden and name != ESCAPE_TOOL
    ]
    _FILL_NAMES = tuple(fill[: max(0, BASE_TOOLS - len(keep))])
    _BASE_NAMES = frozenset(keep | set(_FILL_NAMES) | {ESCAPE_TOOL})


def base_tool_names() -> frozenset[str]:
    if _BASE_NAMES is None:
        _build_base()
    return _BASE_NAMES  # type: ignore[return-value]


def base_fill_names() -> tuple[str, ...]:
    """The registry-order tail of the base set: everything that is neither a
    fundamental nor named by the system prompt. These are the slots a mid-turn
    reveal borrows from when the ceiling is in the way."""
    if _BASE_NAMES is None:
        _build_base()
    return _FILL_NAMES


def select_tools(tool_defs: list[ToolDef] = TOOL_DEFS) -> list[ToolDef]:
    """This turn's tool array: the SAME base set for every message.

    Determinism is the feature — a set that reshuffles per message invalidates
    the cached prefix it sits behind. Sets already under the base budget (the
    proactive/suggestion set) pass through untouched.
    """
    if len(tool_defs) <= BASE_TOOLS:
        return tool_defs
    base = base_tool_names()
    return [t for t in tool_defs if t.name in base]


def reveal_tools(tool_defs: list[ToolDef], names: list[str]) -> list[ToolDef]:
    """Add use_tools' revealed definitions to this turn's array, for the rest of
    the turn. Over the ceiling, the registry-order fill slots are given back
    first (tail first), then the earliest previous reveals — never a core tool
    and never one the system prompt advertises by name."""
    have = {t.name for t in tool_defs}
    wanted = {name for name in names if name not in have}
    extra = [t for t in TOOL_DEFS if t.name in wanted]
    if not extra:
        return tool_defs
    combined = list(tool_defs) + extra
    surplus = len(combined) - MAX_TOOLS
    if surplus > 0:
        base = base_tool_names()
        droppable = [name for name in reversed(base_fill_names()) if name in have] + [
            t.name for t in tool_defs if t.name not in base
        ]
        drop = set(droppable[:surplus])
        combined = [t for t in combined if t.name not in drop]
    return combined


def unknown_tool_message(name: str) -> str:
    """Unknown to this turn is not unknown to the app: a niche tool parked in a
    domain is one use_tools call away, and saying so is what stops the model
    concluding the ability does not exist."""
    domain = domain_of(name)
    if domain:
        return (
            f"{name} is not available this turn. Call use_tools(\"{domain}\") "
            f"to enable it, then call {name} again."
        )
    return f"Unknown tool: {name}"


def _names_memory_note(name: str, inp: dict) -> bool:
    """Whether any path-like argument (path, new_path, target, target_path,
    archive_path, paths, ...) is the memory note, or move_notes would move a
    note onto it. Other tools that compute where they write are stopped at the
    store instead (_refuse_memory_landing)."""
    for key, value in inp.items():
        if key in ("target", "paths") or key.endswith("path"):
            values = value if isinstance(value, list) else [value]
            if any(is_memory_path(item) for item in values):
                return True
    if name == "move_notes" and isinstance(inp.get("paths"), list):
        folder = str(inp.get("folder") or "").strip("/")
        return any(
            is_memory_path(f"{folder + '/' if folder else ''}{str(path).split('/')[-1]}")
            for path in inp["paths"]
        )
    return False


# The tools that write the memory note one capped entry at a time.
MEMORY_WRITERS = frozenset({"remember", "forget"})


def _refuse_memory_landing(rel_path: str) -> None:
    """The store's LANDING_GUARD while a tool runs unconfirmed: a note that
    would come into being at the memory note (a folder moved onto omnia/, a
    split or extract named after a heading) is refused. Compared up to case
    and Unicode form, which APFS treats as one file."""
    landing = vault_names.identity_key(PurePosixPath(rel_path).as_posix())
    if landing == vault_names.identity_key(MEMORY_PATH):
        raise NoteError(
            403,
            f"That would put a note at {MEMORY_PATH}, OMNIA's memory note. Use "
            f"remember, or a call that names {MEMORY_PATH} itself, which the "
            "user confirms.",
        )


# Confirmed tools whose dry_run only reports what would change.
DRY_RUN_TOOLS = frozenset({"find_replace_across_vault", "rename_tag"})


def _is_dry_run(name: str, inp: dict) -> bool:
    return name in DRY_RUN_TOOLS and isinstance(inp, dict) and bool(inp.get("dry_run"))


# --- provenance: untrusted content in the turn --------------------------------
# Direct tools run unconfirmed because the user asked for them. That holds
# only while the request is the user's: once a turn has read text nobody
# vetted (a web clip in a note, a lookup, an attached file), an instruction in
# it can steer the next call. The agent loop keeps a TurnTaint per turn, and
# the few sinks whose effect outlives the chat go past the user first, full
# auto or not; a pass nobody is watching has them refused.

NOTE_TEXT = "note text"
LOOKUP_RESULT = "lookup result"
ATTACHED_FILE = "attached file"
PAPER_TRANSCRIPT = "Paper transcript"
PAST_CONVERSATION = "past conversation"
UNCLASSIFIED_RESULT = "tool result"

# Read tools whose results carry text nobody vetted, and how the card names it.
UNTRUSTED_SOURCE_TOOLS: dict[str, str] = {
    # note bodies, whole or in part (headings and log lines are body text too)
    "read_notes": NOTE_TEXT,
    "semantic_search": NOTE_TEXT,
    "keyword_search": NOTE_TEXT,
    "ask_vault": NOTE_TEXT,
    "summarize_notes": NOTE_TEXT,
    "get_outline": NOTE_TEXT,
    "related_notes": NOTE_TEXT,
    "find_unlinked_mentions": NOTE_TEXT,
    "inspect_note_version": NOTE_TEXT,
    "note_changes": NOTE_TEXT,
    "inspect_trash": NOTE_TEXT,
    "list_pending": NOTE_TEXT,
    "media_diary": NOTE_TEXT,
    "get_flashcard_deck": NOTE_TEXT,
    # fetched from the web
    "lookup_media": LOOKUP_RESULT,
    "media_details": LOOKUP_RESULT,
    "similar_media": LOOKUP_RESULT,
    "search_media_images": LOOKUP_RESULT,
    # files, handwriting, old chats
    "read_attachment": ATTACHED_FILE,
    "search_handwriting": PAPER_TRANSCRIPT,
    "read_page": PAPER_TRANSCRIPT,
    "transcribe_page": PAPER_TRANSCRIPT,
    "search_conversations": PAST_CONVERSATION,
    "read_conversation": PAST_CONVERSATION,
}

# Read tools whose results the app composes from its own records: paths,
# titles, tags, counts, settings, usage. Task and calendar rows are placed here
# too, knowingly: a task's text is a checkbox line from whichever note holds it
# (list_tasks and the digests read every note's), so a line planted in a
# clipped note reaches the model through them without a card. Carding every
# task and calendar turn was judged the larger cost (HANDOFF §5, Taint cards).
# A read tool in neither table taints (test_trust_taint fails until it is
# placed).
APP_SOURCE_TOOLS: frozenset[str] = frozenset({
    # vault structure
    "read_structure", "query_by_date", "list_folders", "list_templates",
    "list_attachments", "get_backlinks", "get_forward_links",
    "find_broken_links", "find_notes", "find_duplicates", "vault_overview",
    "analyze_tags", "suggest_tags", "get_note_history", "list_trash",
    "list_notebooks",
    # tasks, calendar, activity (task text is note text; see above)
    "list_tasks", "get_task_timeline", "board_digest", "get_digest_data",
    "generate_daily_briefing", "summarize_timespan", "get_calendar_range",
    "list_events", "week_agenda", "find_free_time", "list_queued_reminders",
    "get_activity_summary",
    # the media library and flashcard counts
    "list_media", "media_stats", "list_flashcard_decks", "study_stats",
    # OMNIA's own records and settings
    "read_habits", "list_conversations", "list_recent_actions",
    "get_appearance", "get_timezone", "get_backup_status",
    "get_security_overview",
    # acks
    "show_graphic", "notify_user", ESCAPE_TOOL,
})

# Sinks a tainted turn gates by name. Every confirmed tool is one too (bulk and
# vault-wide edits, deletes, schedules), a direct write to the memory note is
# one, and so are the calls in _is_taint_sink that set a task reminder or
# restart or drop a schedule. Not sinks: log_media and set_media_art (their
# executors download only an image URL a lookup offered in this thread),
# note_observation (it stages a suggestion the user confirms on its own
# card), and events with their reminders, which are calendar data — "add my
# syllabus dates" stays one step (HANDOFF §5, Taint cards).
TAINT_SINKS: frozenset[str] = frozenset({
    # a push, now or later, in words the model chose
    "notify_user", "create_reminder", "set_note_followup",
    # what every later turn is told (update_habit below)
    "remember", "forget",
    # a whole subtree at once
    "move_folder",
})


class TurnTaint:
    """The unvetted content one agent turn has read: the kinds of source, in
    the order they arrived."""

    def __init__(self) -> None:
        self.sources: dict[str, None] = {}

    def __bool__(self) -> bool:
        return bool(self.sources)

    def add(self, source: str) -> None:
        self.sources.setdefault(source, None)

    def observe(self, name: str, result) -> None:
        """Fold in one tool's parsed result. A bare error carries no content
        (an error beside a payload still counts); a read tool neither table
        knows taints."""
        if (
            isinstance(result, dict)
            and result.get("error")
            and not any(value for key, value in result.items() if key not in ("error", "note"))
        ):
            return
        source = UNTRUSTED_SOURCE_TOOLS.get(name)
        if source is None:
            if name in APP_SOURCE_TOOLS or TOOL_MODES.get(name, "read") != "read":
                return
            source = UNCLASSIFIED_RESULT
        self.add(source)

    def snapshot(self) -> "TurnTaint":
        """A frozen copy for one round: calls made in the same round as a read
        were chosen before its result existed."""
        copy = TurnTaint()
        copy.sources = dict(self.sources)
        return copy

    def reason(self) -> str:
        return f"This step uses content from an external source ({' / '.join(self.sources)})."


def _is_taint_sink(name: str, inp: dict) -> bool:
    if name in TAINT_SINKS:
        return True
    if name == "update_habit":
        # confirm and contradict only move a stored fact's confidence, and
        # the prompt asks for one whenever a note read bears a habit out.
        return inp.get("event") not in ("confirm", "contradict")
    mode = TOOL_MODES.get(name)
    if mode == "confirmed":
        return not _is_dry_run(name, inp)
    if mode == "direct" and _names_memory_note(name, inp):
        return True
    # A reminder pushes the task's text later, outside the chat.
    if name == "set_task_reminder":
        return not inp.get("clear")
    if name == "manage_scheduled_runs":
        # Resume restarts a run that may push; cancel drops the user's own.
        return str(inp.get("operation") or "").lower() in ("resume", "cancel")
    return False


def taint_reason(name: str, inp: dict, taint: TurnTaint | None) -> str | None:
    """The card's one-line reason when this turn's taint gates the call, else
    None. A taint-gated call confirms even in full auto."""
    if not taint:
        return None
    if not _is_taint_sink(name, inp if isinstance(inp, dict) else {}):
        return None
    return taint.reason()


def needs_confirmation(name: str, inp: dict, taint: TurnTaint | None = None) -> bool:
    """Whether this call must be user-approved before it runs. Shared with the
    agent loop's bulk gate so the two can't drift: direct tools are for
    explicit user requests and run unconfirmed no matter how many arrive in
    one round — only calls that would confirm individually get bundled. A
    turn that read unvetted content gates its sinks as well (taint_reason)."""
    if taint_reason(name, inp, taint):
        return True
    if TOOL_MODES.get(name) == "confirmed":
        # A preview changes nothing, and gating it showed the very card the
        # vault-wide apply shows, so approving the harmless one primed the
        # user to approve the other.
        return not _is_dry_run(name, inp)
    # The memory note rides in every later turn's prompt. remember/forget write
    # it one capped entry at a time (they take `text`, so this passes them by);
    # any other direct write to it goes past the user first.
    if TOOL_MODES.get(name) == "direct" and isinstance(inp, dict) and _names_memory_note(name, inp):
        return True
    # Both overwrite restores replace a whole note with an old version; the
    # prompt promises a card for each.
    return name == "restore_note_to" or (
        name == "restore_note_version" and inp.get("mode") == "replace"
    )


async def _autosave_before_mutation(state) -> None:
    """Commit the user's pending edits as their own autosave before OMNIA
    writes, so the action's scoped commit holds only OMNIA's delta.

    The scoped commit stages whole paths: a paragraph typed since the last
    debounced autosave rode along in it and Undo reverted it too, and a note
    created or trashed since then was untracked, so the commit failed on its
    pathspec after the tool had already moved the file.
    """
    vault = getattr(state, "vault", None)
    if vault is None:
        return
    try:
        if await asyncio.to_thread(vault.has_changes):
            await vault.commit_all("autosave")
    except Exception:
        log.warning("autosave before OMNIA action failed", exc_info=True)


async def _run_mutation(state, name: str, fn: Executor, session_id: str, inp: dict) -> dict:
    """A mutation is write -> index -> scoped commit -> action record. Stop and
    a dropped SSE stream both cancel the turn, and a cancel landing inside that
    sequence left the note rewritten with no commit or no Undo card. Once the
    lock is held the mutation runs in its own task and a cancel is re-raised
    only after it finishes, so Stop lands between tools, never inside one.
    Waiting for the lock stays cancellable: nothing has been written yet."""
    async with state.actions.mutation_lock:
        await _autosave_before_mutation(state)
        task = asyncio.ensure_future(fn(state, session_id, inp))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # asyncio.wait never cancels what it waits on, so Stop followed by
            # the stream teardown (two cancels) can't reach the write either.
            while not task.done():
                try:
                    await asyncio.wait({task})
                except asyncio.CancelledError:
                    pass
            if not task.cancelled() and task.exception() is not None:
                log.error(
                    "tool %s failed after its turn was cancelled (thread %s)",
                    name,
                    session_id,
                    exc_info=task.exception(),
                )
            raise


async def execute_tool(
    state,
    session_id: str,
    name: str,
    inp: dict,
    *,
    confirmed: bool = False,
    auth_session_id: str | None = None,
    available: frozenset[str] | set[str] | None = None,
    taint: TurnTaint | None = None,
) -> str:
    fn = EXECUTORS.get(name)
    # `available` is the set of names offered this turn — the agent loop passes
    # it so a call to a tool parked behind use_tools comes back with the way to
    # reach it. Callers with no turn to speak of (approving a confirmation card)
    # leave it None, which gates nothing. `taint` is the same: the loop's view
    # of what the turn has read; an approval passes none.
    if not fn or (available is not None and name not in available):
        return json.dumps(_err(unknown_tool_message(name)))
    mode = TOOL_MODES.get(name, "read")
    reason = taint_reason(name, inp, taint)
    if (
        needs_confirmation(name, inp, taint)
        and not confirmed
        # Full auto answers every confirmation with yes before it exists;
        # the action record and its Undo stay exactly as they were. Not a
        # taint card: that one asks whether the content steered the call.
        and (reason or not getattr(state, "omnia_full_auto", False))
        and hasattr(state, "actions")
    ):
        try:
            confirmation = state.actions.request_confirmation(
                session_id, name, inp, auth_session_id, reason=reason
            )
            return json.dumps(
                {"confirmation_required": confirmation}, ensure_ascii=False
            )
        except Exception as e:
            log.exception("confirmation for %s failed (thread %s)", name, session_id)
            return json.dumps(_err(f"Could not request confirmation: {e}"))
    # A call that names the memory note went past the user (or full auto) just
    # above; one that computes where it writes cannot, so the store refuses a
    # note landing there for the length of the call. A task spawned inside
    # (_run_mutation's) copies the guard with the context.
    landing_guard = None
    if not confirmed and name not in MEMORY_WRITERS and not (
        isinstance(inp, dict) and _names_memory_note(name, inp)
    ):
        landing_guard = LANDING_GUARD.set(_refuse_memory_landing)
    try:
        if mode in {"direct", "confirmed"} and hasattr(state, "actions"):
            result = await _run_mutation(state, name, fn, session_id, inp)
        else:
            # Reads and review-tray proposals stay cancellable, so Stop never
            # waits on a slow search.
            result = await fn(state, session_id, inp)
    except NoteError as e:
        result = _err(e.detail)
    except fm.FrontmatterError as e:
        # fm.serialize refusing to rewrite a block that does not load.
        result = _err(f"Frontmatter is not valid YAML: {e}")
    except KeyError as e:
        # Usually a missing input field, but a lookup bug inside a handler
        # lands here too and reads the same to the model.
        log.debug("tool %s raised KeyError (thread %s)", name, session_id, exc_info=True)
        result = _err(f"Missing required field: {e}")
    except Exception as e:
        # Input keys only: values carry note bodies, and agent.py already logs
        # a clipped summary of the call at INFO.
        log.exception(
            "tool %s failed (thread %s) input keys=%s", name, session_id, sorted(inp)
        )
        result = _err(f"Tool failed: {e}")
    finally:
        if landing_guard is not None:
            LANDING_GUARD.reset(landing_guard)
    try:
        return json.dumps(result, ensure_ascii=False)
    except (TypeError, ValueError) as e:
        # Outside the try above, one unserializable value (a date, a Decimal)
        # propagated out of execute_tool and failed the whole turn instead of
        # returning a single tool error the model could work around.
        return json.dumps(_err(f"Tool returned unserializable data: {e}"))
