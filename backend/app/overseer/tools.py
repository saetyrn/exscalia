"""OMNIA's read, proposal, direct-action, and confirmed-action registry."""
import difflib
import json
import math
import re
from datetime import date as date_cls
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..vault import fm
from ..index.db import content_hash
from ..vault.notes import NoteError
from .provider import ToolDef

MAX_READ_CHARS = 60_000

Executor = Callable[[Any, str, dict], Awaitable[dict]]  # (state, session_id, input) -> result


def _err(msg: str) -> dict:
    return {"error": msg}


# --- read tools -------------------------------------------------------------


async def semantic_search(state, session_id, inp) -> dict:
    if not state.semantic.available:
        return _err("Semantic index is not available.")
    k = min(int(inp.get("k", 8)), 20)
    results = await state.semantic.search(inp["query"], k=k)
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
            "No semantic matches. Try keyword_search on a distinctive word, "
            "or read_structure to see what the vault contains."
        )
    return out


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
    start, end = inp.get("start"), inp.get("end")
    if not start or not end:
        return _err("Provide start and end as YYYY-MM-DD.")
    rows = await state.db.execute_fetchall(
        f"""SELECT path, title, created, modified FROM notes
            WHERE substr({field}, 1, 10) BETWEEN ? AND ?
              AND path NOT LIKE 'templates/%'
            ORDER BY {field} DESC LIMIT 200""",
        (start, end),
    )
    out = {"results": [dict(r) for r in rows]}
    if not out["results"]:
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
        "SELECT path, title, created, modified, pinned FROM notes"
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
    modified_since = inp.get("modified_since")
    untagged_only = bool(inp.get("untagged_only"))
    limit = max(1, min(int(inp.get("limit", 300)), 1000))
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
        if modified_since and str((meta.get(p) or {}).get("modified") or "")[:10] < modified_since:
            continue
        selected.append(p)

    notes = []
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
        notes.append(entry)
    return {
        "note_count": len(selected),
        "returned": len(notes),
        "offset": offset,
        "more_available": offset + len(notes) < len(selected),
        "notes": notes,
    }


async def read_notes(state, session_id, inp) -> dict:
    paths = inp.get("paths") or []
    if not isinstance(paths, list) or not paths:
        return _err("Provide `paths`: a list of note paths.")
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
        if total + len(content) > MAX_READ_CHARS:
            out.append({"path": p, "error": "read budget exceeded; request fewer notes"})
            break
        total += len(content)
        out.append({"path": p, "content": content, "revision": content_hash(content)})
    return {"notes": out}


# --- write tools (all staged) ----------------------------------------------

STAGED_NOTE = "Staged for user review — not applied until approved in the pending-changes box."


async def create_note(state, session_id, inp) -> dict:
    path, content = inp["path"], inp["content"]
    if await state.pending.effective_content(path) is not None:
        return _err(f"{path} already exists. Use edit_note.")
    pid = await state.pending.stage(
        session_id, "create", path, new_content=content, rationale=inp.get("rationale")
    )
    return {"staged": True, "pending_id": pid, "note": STAGED_NOTE}


async def edit_note(state, session_id, inp) -> dict:
    path = inp["path"]
    if await state.pending.effective_content(path) is None:
        return _err(f"{path} does not exist. Use create_note.")
    pid = await state.pending.stage(
        session_id, "edit", path, new_content=inp["new_content"], rationale=inp.get("rationale")
    )
    return {"staged": True, "pending_id": pid, "note": STAGED_NOTE}


async def delete_note(state, session_id, inp) -> dict:
    path = inp["path"]
    if await state.pending.effective_content(path) is None:
        return _err(f"{path} does not exist.")
    pid = await state.pending.stage(
        session_id, "delete", path, rationale=inp.get("rationale")
    )
    return {"staged": True, "pending_id": pid, "note": STAGED_NOTE}


async def move_note(state, session_id, inp) -> dict:
    path, new_path = inp["path"], inp["new_path"]
    if await state.pending.effective_content(path) is None:
        return _err(f"{path} does not exist.")
    if await state.pending.effective_content(new_path) is not None:
        return _err(f"{new_path} already exists.")
    pid = await state.pending.stage(
        session_id, "move", path, new_path=new_path, rationale=inp.get("rationale")
    )
    return {"staged": True, "pending_id": pid, "note": STAGED_NOTE}


async def edit_frontmatter(state, session_id, inp) -> dict:
    path = inp["path"]
    content = await state.pending.effective_content(path)
    if content is None:
        return _err(f"{path} does not exist.")
    meta, body = fm.parse(content)
    for key, value in (inp.get("set") or {}).items():
        if key in ("created", "modified"):
            return _err(f"`{key}` is auto-managed and cannot be set.")
        meta[key] = value
    for key in inp.get("remove") or []:
        if key in ("created", "modified", "title"):
            return _err(f"`{key}` cannot be removed.")
        meta.pop(key, None)
    new_content = fm.serialize(meta, body)
    pid = await state.pending.stage(
        session_id, "edit", path, new_content=new_content, rationale=inp.get("rationale")
    )
    return {"staged": True, "pending_id": pid, "note": STAGED_NOTE}


# --- habit / preference store (the learning layer) --------------------------

HABIT_SET_CONFIDENCE = 0.85
HABIT_CONFIRM_BUMP = 0.05
HABIT_CONTRADICT_DROP = 0.25


async def read_habits(state, session_id, inp) -> dict:
    rows = await state.db.execute_fetchall(
        "SELECT key, value, confidence, source, last_seen, times_confirmed"
        " FROM habits ORDER BY confidence DESC, key"
    )
    return {"habits": [dict(r) for r in rows]}


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
        await db.execute("DELETE FROM habits WHERE key = ?", (key,))
        await db.commit()
        return {"key": key, "deleted": True}

    if event == "set":
        value = inp.get("value")
        if value is None:
            return _err("`set` needs a `value`.")
        await db.execute(
            """INSERT INTO habits(key, value, confidence, source, last_seen, times_confirmed)
               VALUES (?,?,?,?,?,0)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                 confidence=excluded.confidence, source=excluded.source,
                 last_seen=excluded.last_seen""",
            (key, str(value), HABIT_SET_CONFIDENCE, str(inp.get("source") or "user"), now),
        )
        await db.commit()
        return {"key": key, "value": str(value), "confidence": HABIT_SET_CONFIDENCE}

    if not row:
        return _err(f"No habit with key '{key}' — use event=set first.")
    if event == "confirm":
        conf = min(1.0, row["confidence"] + HABIT_CONFIRM_BUMP)
        confirmed = row["times_confirmed"] + 1
    else:  # contradict
        conf = max(0.05, row["confidence"] - HABIT_CONTRADICT_DROP)
        confirmed = row["times_confirmed"]
    await db.execute(
        "UPDATE habits SET confidence=?, times_confirmed=?, last_seen=? WHERE key=?",
        (conf, confirmed, now, key),
    )
    await db.commit()
    return {
        "key": key,
        "value": row["value"],
        "confidence": round(conf, 3),
        "times_confirmed": confirmed,
        "note": "confidence low — consider re-asking the user once" if conf < 0.4 else "",
    }


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
    stem = Path(path).stem.lower()
    rows = await state.db.execute_fetchall(
        """SELECT DISTINCT l.source AS path, n.title FROM links l
           JOIN notes n ON n.path = l.source
           WHERE l.target_lower = ? AND l.source != ?""",
        (stem, path),
    )
    return {"backlinks": [dict(r) for r in rows]}


async def get_digest_data(state, session_id, inp) -> dict:
    days = max(1, min(int(inp.get("days", 1)), 60))
    try:
        today = datetime.now(
            ZoneInfo(getattr(state, "timezone", "America/New_York"))
        ).date()
    except ZoneInfoNotFoundError:
        today = date_cls.today()
    since = (today - timedelta(days=days)).isoformat()
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
        """SELECT path, title FROM notes
           WHERE substr(created,1,10) >= ? AND path NOT LIKE 'templates/%'
           ORDER BY created DESC LIMIT 40""",
        (since,),
    )
    modified = await db.execute_fetchall(
        """SELECT path, title FROM notes
           WHERE substr(modified,1,10) >= ? AND substr(created,1,10) < ?
             AND path NOT LIKE 'templates/%'
           ORDER BY modified DESC LIMIT 40""",
        (since, since),
    )
    done_recent = await db.execute_fetchall(
        """SELECT t.path, t.text FROM tasks t JOIN notes n ON n.path = t.path
           WHERE t.status = 'done' AND substr(n.modified,1,10) >= ?
             AND t.path NOT LIKE 'templates/%' LIMIT 40""",
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


async def find_duplicates(state, session_id, inp) -> dict:
    if not state.semantic.available:
        return _err("Semantic index is not available — cannot scan for duplicates.")
    threshold = float(inp.get("similarity_threshold", 0.75))
    notes = [n for n in state.store.list_notes() if not n.path.startswith("templates/")]
    if len(notes) > 300:
        return _err("Vault too large for a full scan (cap 300 notes). Narrow the scope.")
    bodies: dict[str, str] = {}
    for n in notes:
        content = await state.pending.effective_content(n.path)
        if content:
            bodies[n.path] = fm.parse(content)[1]
    seen: set[tuple[str, str]] = set()
    pairs = []
    for path, body in bodies.items():
        if not body.strip():
            continue
        for r in await state.semantic.search(body[:2000], k=4):
            other = r["path"]
            if other == path or other not in bodies:
                continue
            key = tuple(sorted((path, other)))
            if key in seen or r["score"] < threshold:
                continue
            seen.add(key)
            overlap = difflib.SequenceMatcher(None, bodies[path], bodies[other]).ratio()
            if overlap >= 0.35:
                pairs.append(
                    {
                        "a": key[0],
                        "b": key[1],
                        "similarity": r["score"],
                        "content_overlap": round(overlap, 3),
                    }
                )
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


# --- registry ---------------------------------------------------------------


def _schema(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required}

RATIONALE = {"type": "string", "description": "One short sentence: why this change."}

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
            "Find notes created or modified in a date range (inclusive).",
            _schema(
                {
                    "start": {"type": "string", "description": "YYYY-MM-DD"},
                    "end": {"type": "string", "description": "YYYY-MM-DD"},
                    "field": {"type": "string", "enum": ["created", "modified"]},
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
                        "description": "Only notes modified on or after this YYYY-MM-DD",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max notes (default 300, max 1000)",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Skip this many notes, for paging through a large vault",
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
            "Read the full content of up to 20 notes by path.",
            _schema(
                {"paths": {"type": "array", "items": {"type": "string"}}},
                ["paths"],
            ),
        ),
        read_notes,
    ),
    (
        ToolDef(
            "create_note",
            "Stage a new note (markdown; frontmatter optional — title/created/modified "
            "are auto-managed). Lands in the review tray, not the vault.",
            _schema(
                {
                    "path": {"type": "string", "description": "Vault-relative path ending in .md"},
                    "content": {"type": "string"},
                    "rationale": RATIONALE,
                },
                ["path", "content"],
            ),
        ),
        create_note,
    ),
    (
        ToolDef(
            "edit_note",
            "Stage a full-content replacement of an existing note.",
            _schema(
                {
                    "path": {"type": "string"},
                    "new_content": {"type": "string", "description": "Complete new note content"},
                    "rationale": RATIONALE,
                },
                ["path", "new_content"],
            ),
        ),
        edit_note,
    ),
    (
        ToolDef(
            "delete_note",
            "Stage a note deletion (recoverable from git history even after approval). "
            "Action-tier: only when the user explicitly asked for a deletion/merge.",
            _schema({"path": {"type": "string"}, "rationale": RATIONALE}, ["path"]),
            tier="action",
        ),
        delete_note,
    ),
    (
        ToolDef(
            "move_note",
            "Stage a move/rename of a note to a new vault-relative path. Action-tier: "
            "only when the user explicitly asked to move/reorganize.",
            _schema(
                {
                    "path": {"type": "string"},
                    "new_path": {"type": "string"},
                    "rationale": RATIONALE,
                },
                ["path", "new_path"],
            ),
            tier="action",
        ),
        move_note,
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
            "'daily_note.logs_hours' or 'tags.canonical.ml'.",
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
            # This writes persistent claims about the user. It is not a vault
            # mutation (habits live in SQLite, not git), so it needs no
            # confirmation card — but it must never run unattended, which
            # tier="action" enforces both in the tool filter and in the loop.
            # Previously it was plain read/suggestion, so a background morning
            # pass could silently rewrite the user's habit profile.
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
            "confirmed by content overlap). Action-tier: run only when the user "
            "explicitly asks to find duplicates or merge notes.",
            _schema(
                {
                    "similarity_threshold": {
                        "type": "number",
                        "description": "0-1, default 0.75",
                    }
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
            "edit_frontmatter",
            "Stage a frontmatter-only change: set or remove custom fields without "
            "touching the body. NOTE: for tagging, prefer edit_note to add inline "
            "#hashtags to the body instead. created/modified are auto-managed.",
            _schema(
                {
                    "path": {"type": "string"},
                    "set": {"type": "object", "description": "Fields to set, e.g. {\"tags\": [\"a\"]}"},
                    "remove": {"type": "array", "items": {"type": "string"}},
                    "rationale": RATIONALE,
                },
                ["path"],
            ),
        ),
        edit_frontmatter,
    ),
]

from .tool_expansion import EXPANDED_TOOLS, OVERRIDDEN_TOOL_NAMES

TOOLS = [
    pair for pair in TOOLS if pair[0].name not in OVERRIDDEN_TOOL_NAMES
] + EXPANDED_TOOLS

TOOL_DEFS = [t for t, _ in TOOLS]
SUGGESTION_TOOL_DEFS = [
    t for t, _ in TOOLS if t.mode in {"read", "proposal"} and t.tier == "suggestion"
]
TOOL_TIERS: dict[str, str] = {t.name: t.tier for t, _ in TOOLS}
TOOL_MODES: dict[str, str] = {t.name: t.mode for t, _ in TOOLS}
EXECUTORS: dict[str, Executor] = {t.name: fn for t, fn in TOOLS}


# The Anthropic API rejects a request that carries more than 128 tool
# definitions. The registry has outgrown that, so each request is trimmed to a
# relevant slice: a curated core is always offered, and the rest of the budget
# goes to the tools whose name/description best match what the user just asked.
# Every executor stays reachable — this only changes what a single turn sees.
MAX_TOOLS = 120

# Fundamentals offered on every turn, so a generic ask still has the essentials
# even when relevance scoring has nothing to grab onto. Names that no longer
# exist are ignored (a test guards against drift).
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
    "remember", "forget",
    # meta / navigation / awareness
    "open", "close_tabs", "notify_user", "undo_last_action",
    "list_recent_actions", "list_pending", "get_digest_data",
    # tags
    "suggest_tags", "analyze_tags", "bulk_tag",
    # trash
    "list_trash", "restore_trash",
})

_WORD_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return set(_WORD_RE.findall((text or "").lower()))


def select_tools(
    tool_defs: list[ToolDef], query: str, limit: int = MAX_TOOLS
) -> list[ToolDef]:
    """Trim a tool set to at most `limit`, keeping the core plus the tools most
    relevant to `query`. Returns the input unchanged when it already fits, so
    the proactive/suggestion set (well under the cap) is never touched."""
    if len(tool_defs) <= limit:
        return tool_defs
    q = _tokens(query)
    scored: list[tuple[int, int, str]] = []
    for i, t in enumerate(tool_defs):
        name_tokens = set(t.name.split("_"))
        relevance = 2 * len(q & name_tokens) + len(q & _tokens(t.description))
        core = 1000 if t.name in CORE_TOOL_NAMES else 0
        # -i lets earlier-registered tools win ties, deterministically.
        scored.append((core + relevance, -i, t.name))
    scored.sort(reverse=True)
    keep = {name for _, _, name in scored[:limit]}
    return [t for t in tool_defs if t.name in keep]


def needs_confirmation(name: str, inp: dict) -> bool:
    """Whether this call must be user-approved before it runs. Shared with the
    agent loop's bulk gate so the two can't drift: direct tools are for
    explicit user requests and run unconfirmed no matter how many arrive in
    one round — only calls that would confirm individually get bundled."""
    if TOOL_MODES.get(name) == "confirmed":
        return True
    return name == "restore_note_version" and inp.get("mode") == "replace"


async def execute_tool(
    state,
    session_id: str,
    name: str,
    inp: dict,
    *,
    confirmed: bool = False,
    auth_session_id: str | None = None,
) -> str:
    fn = EXECUTORS.get(name)
    if not fn:
        return json.dumps(_err(f"Unknown tool: {name}"))
    mode = TOOL_MODES.get(name, "read")
    if (
        needs_confirmation(name, inp)
        and not confirmed
        # Full auto answers every confirmation with yes before it exists;
        # the action record and its Undo stay exactly as they were.
        and not getattr(state, "omnia_full_auto", False)
        and hasattr(state, "actions")
    ):
        try:
            confirmation = state.actions.request_confirmation(
                session_id, name, inp, auth_session_id
            )
            return json.dumps(
                {"confirmation_required": confirmation}, ensure_ascii=False
            )
        except Exception as e:
            return json.dumps(_err(f"Could not request confirmation: {e}"))
    try:
        if mode in {"direct", "confirmed"} and hasattr(state, "actions"):
            async with state.actions.mutation_lock:
                result = await fn(state, session_id, inp)
        else:
            result = await fn(state, session_id, inp)
    except NoteError as e:
        result = _err(e.detail)
    except KeyError as e:
        result = _err(f"Missing required field: {e}")
    except Exception as e:
        result = _err(f"Tool failed: {e}")
    try:
        return json.dumps(result, ensure_ascii=False)
    except (TypeError, ValueError) as e:
        # Outside the try above, one unserializable value (a date, a Decimal)
        # propagated out of execute_tool and failed the whole turn instead of
        # returning a single tool error the model could work around.
        return json.dumps(_err(f"Tool returned unserializable data: {e}"))
