"""Metadata endpoints: tags, backlinks, keyword search, templates, daily notes."""
from datetime import date as date_cls, datetime
from pathlib import Path
import re
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from ..overseer.housekeeping import DEFAULT_ENABLED as HOUSEKEEPING_DEFAULT
from ..overseer.runtime_config import (
    REASONING_LEVELS,
    effective_ai,
    effective_autocorrect_model,
    effective_background_model,
    effective_media_model,
    load_ai_config,
    model_ids,
    monthly_token_budget,
    provider_info,
    save_ai_config,
)
from ..vault import templates as tpl
from ..vault.internal import atomic_json, read_json
from ..vault.notes import NoteStore
from ..vault.notes import NoteError

router = APIRouter(prefix="/api", tags=["meta"])

DAILY_DIR = "daily"
HIGHLIGHT_OPEN = "\uE000"
HIGHLIGHT_CLOSE = "\uE001"


class GeneralSettingsBody(BaseModel):
    timezone: str


ThemeId = Literal["system", "light", "dark", "black", "beige", "forest", "custom"]
FontId = Literal["system", "serif", "rounded", "mono"]
THEME_COLOR_VARS = (
    "bg",
    "bg-panel",
    "bg-hover",
    "bg-active",
    "border",
    "text",
    "text-muted",
    "accent",
    "accent-soft",
    "danger",
    "added",
    "removed",
)
HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


class AppearanceSettingsBody(BaseModel):
    theme: ThemeId = "system"
    font: FontId = "system"
    intoxicate: bool = False
    custom_theme: dict[str, Any] | None = None
    saved_themes: list[dict[str, Any]] = Field(default_factory=list, max_length=50)
    active_saved_theme_id: str | None = Field(default=None, max_length=100)


def _config_path(request: Request) -> Path:
    return (
        request.app.state.settings.vault_dir
        / NoteStore.INTERNAL_DIR
        / "config.json"
    )


def _update_config(request: Request, **updates: Any) -> None:
    path = _config_path(request)
    config = read_json(path, {})
    if not isinstance(config, dict):
        config = {}
    config.update(updates)
    atomic_json(path, config)
    request.app.state.autocommit.touch()


def _custom_theme(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or value.get("mode") not in ("light", "dark"):
        raise NoteError(400, "Invalid custom theme")
    colors = value.get("colors")
    if not isinstance(colors, dict):
        raise NoteError(400, "Invalid custom theme colors")
    normalized: dict[str, str] = {}
    for name in THEME_COLOR_VARS:
        color = colors.get(name)
        if not isinstance(color, str) or not HEX_COLOR.fullmatch(color):
            raise NoteError(400, f"Invalid custom theme color: {name}")
        normalized[name] = color.lower()
    return {"mode": value["mode"], "colors": normalized}


def _appearance(body: AppearanceSettingsBody) -> dict[str, Any]:
    saved: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in body.saved_themes:
        if not isinstance(entry, dict):
            raise NoteError(400, "Invalid saved theme")
        theme_id = entry.get("id")
        name = entry.get("name")
        if (
            not isinstance(theme_id, str)
            or not theme_id
            or len(theme_id) > 100
            or theme_id in seen
            or not isinstance(name, str)
            or not name.strip()
        ):
            raise NoteError(400, "Invalid saved theme")
        seen.add(theme_id)
        saved_theme = _custom_theme(entry.get("theme"))
        if saved_theme is None:
            raise NoteError(400, "Invalid saved theme palette")
        saved.append(
            {
                "id": theme_id,
                "name": name.strip()[:40],
                "theme": saved_theme,
            }
        )
    active = (
        body.active_saved_theme_id
        if body.active_saved_theme_id in seen
        else None
    )
    return {
        "theme": body.theme,
        "font": body.font,
        "intoxicate": body.intoxicate,
        "custom_theme": _custom_theme(body.custom_theme),
        "saved_themes": saved,
        "active_saved_theme_id": active,
    }


@router.get("/settings/general")
async def general_settings(request: Request):
    return {"timezone": request.app.state.timezone}


@router.put("/settings/general")
async def save_general_settings(request: Request, body: GeneralSettingsBody):
    try:
        ZoneInfo(body.timezone)
    except ZoneInfoNotFoundError:
        raise NoteError(400, "Unknown IANA timezone")
    request.app.state.timezone = body.timezone
    _update_config(request, timezone=body.timezone)
    return {"timezone": body.timezone}


class OmniaSettingsBody(BaseModel):
    # All optional so a client can save one knob without clobbering the rest —
    # the general-settings body is replace-whole and that bit us before.
    housekeeping: bool | None = None
    monthly_token_budget: int | None = Field(default=None, ge=0, le=10_000_000_000)
    background_model: str | None = None
    media_model: str | None = None
    full_auto: bool | None = None
    autocorrect: bool | None = None
    autocorrect_model: str | None = None
    # Reasoning overrides: "inherit" clears the role back to the chat knob.
    background_reasoning: str | None = None
    media_reasoning: str | None = None
    autocorrect_reasoning: str | None = None


def _omnia_settings_payload(request: Request) -> dict:
    state = request.app.state
    config = load_ai_config(state.settings.data_dir)
    provider_id, _key, model = effective_ai(state.settings, config)
    info = provider_info(provider_id) or {}
    ledger = getattr(state, "usage", None)
    return {
        "housekeeping": bool(
            getattr(state, "omnia_housekeeping", HOUSEKEEPING_DEFAULT)
        ),
        "full_auto": bool(getattr(state, "omnia_full_auto", False)),
        "autocorrect": bool(getattr(state, "omnia_autocorrect", False)),
        "monthly_token_budget": monthly_token_budget(config),
        "background_model": effective_background_model(state.settings, config),
        "media_model": effective_media_model(state.settings, config),
        "autocorrect_model": effective_autocorrect_model(state.settings, config),
        "chat_model": model,
        # Raw override dict — an absent role means "inherit the chat knob",
        # which the UI renders differently from an explicit level.
        "reasoning_levels": dict(config.get("reasoning_levels") or {}),
        # For the background-model picker; same shape the AI section uses.
        "models": info.get("models", []),
        "usage": ledger.month_summary() if ledger is not None else None,
    }


# Its own pair rather than a field on the general settings: that body is
# replace-whole and the client sends only a timezone, so a merged field would
# silently switch this back off every time the timezone was saved.
@router.get("/settings/omnia")
async def omnia_settings(request: Request):
    return _omnia_settings_payload(request)


@router.put("/settings/omnia")
async def save_omnia_settings(request: Request, body: OmniaSettingsBody):
    state = request.app.state
    if body.housekeeping is not None:
        # Both the live value and the file, like the timezone route — the
        # nightly loop reads state, so this takes effect on the next tick
        # without a restart.
        state.omnia_housekeeping = body.housekeeping
        _update_config(request, omnia_housekeeping=body.housekeeping)

    if body.full_auto is not None:
        state.omnia_full_auto = body.full_auto
        _update_config(request, omnia_full_auto=body.full_auto)

    if body.autocorrect is not None:
        state.omnia_autocorrect = body.autocorrect
        _update_config(request, omnia_autocorrect=body.autocorrect)

    if (
        body.monthly_token_budget is not None
        or body.background_model is not None
        or body.media_model is not None
        or body.autocorrect_model is not None
        or body.background_reasoning is not None
        or body.media_reasoning is not None
        or body.autocorrect_reasoning is not None
    ):
        config = load_ai_config(state.settings.data_dir)
        provider_id, _key, model = effective_ai(state.settings, config)
        if body.monthly_token_budget is not None:
            config["monthly_token_budget"] = body.monthly_token_budget
        for value, key in (
            (body.background_model, "background_models"),
            (body.media_model, "media_models"),
            (body.autocorrect_model, "autocorrect_models"),
        ):
            if value is None:
                continue
            if value and value not in model_ids(provider_id):
                raise NoteError(400, "Unknown model for the current provider")
            overrides = config.setdefault(key, {})
            # Picking the chat model (or blank) clears the override.
            if value in ("", model):
                overrides.pop(provider_id, None)
            else:
                overrides[provider_id] = value
        for value, role in (
            (body.background_reasoning, "background"),
            (body.media_reasoning, "media"),
            (body.autocorrect_reasoning, "autocorrect"),
        ):
            if value is None:
                continue
            if value != "inherit" and value not in REASONING_LEVELS:
                raise NoteError(400, "Unknown reasoning level")
            levels = config.setdefault("reasoning_levels", {})
            if value == "inherit":
                levels.pop(role, None)
            else:
                levels[role] = value
        save_ai_config(state.settings.data_dir, config)
        state.ai_config = config

    return _omnia_settings_payload(request)


@router.get("/settings/appearance")
async def appearance_settings(request: Request):
    config = read_json(_config_path(request), {})
    raw = config.get("appearance") if isinstance(config, dict) else None
    if not isinstance(raw, dict):
        return {"configured": False, **_appearance(AppearanceSettingsBody())}
    try:
        normalized = _appearance(AppearanceSettingsBody.model_validate(raw))
    except Exception:
        return {"configured": False, **_appearance(AppearanceSettingsBody())}
    return {"configured": True, **normalized}


@router.put("/settings/appearance")
async def save_appearance_settings(request: Request, body: AppearanceSettingsBody):
    normalized = _appearance(body)
    _update_config(request, appearance=normalized)
    return {"configured": True, **normalized}


# Sidebar folder order. Kept in the vault config rather than localStorage so the
# arrangement rides the backup and follows you between devices, like the theme.
MAX_ORDERED_FOLDERS = 2000


class FolderOrderBody(BaseModel):
    order: list[str]


def _folder_order(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    seen: set[str] = set()
    order: list[str] = []
    for item in value:
        path = str(item).strip().strip("/").strip()
        if not path or path in seen:
            continue
        seen.add(path)
        order.append(path)
        if len(order) >= MAX_ORDERED_FOLDERS:
            break
    return order


# Manual note arrangement, same contract as folder-order: the client sends
# every note path it displays, in order; stale entries fall away on save and
# unlisted notes sort alphabetically after the arranged ones.
@router.get("/settings/note-order")
async def note_order(request: Request):
    config = read_json(_config_path(request), {})
    raw = config.get("note_order") if isinstance(config, dict) else None
    return {"order": _folder_order(raw)}


@router.put("/settings/note-order")
async def save_note_order(request: Request, body: FolderOrderBody):
    order = _folder_order(body.order)
    _update_config(request, note_order=order)
    return {"order": order}


@router.get("/settings/folder-order")
async def folder_order(request: Request):
    config = read_json(_config_path(request), {})
    raw = config.get("folder_order") if isinstance(config, dict) else None
    return {"order": _folder_order(raw)}


@router.put("/settings/folder-order")
async def save_folder_order(request: Request, body: FolderOrderBody):
    """The client sends the full ordered list of folders it knows about, so
    entries for folders that were since renamed or deleted fall away here
    instead of accumulating forever."""
    order = _folder_order(body.order)
    _update_config(request, folder_order=order)
    return {"order": order}


def _redact_remote(url: str) -> str:
    """Strip userinfo (tokens) from a remote URL for display."""
    import re

    return re.sub(r"//[^@/]+@", "//", url)


@router.get("/backup/status")
async def backup_status(request: Request):
    pusher = request.app.state.backup
    if pusher is None:
        return {"configured": False}
    return {
        "configured": True,
        "remote": _redact_remote(pusher.url),
        "interval": pusher.interval,
        "last_push": pusher.last_push,
        "last_error": pusher.last_error,
    }


@router.post("/backup/push")
async def backup_push(request: Request):
    pusher = request.app.state.backup
    if pusher is None:
        raise NoteError(400, "No backup remote configured (set OMNIA_BACKUP_REMOTE).")
    return await pusher.push()


@router.get("/tags")
async def list_tags(request: Request):
    db = request.app.state.db
    rows = await db.execute_fetchall(
        """SELECT tag, COUNT(*) AS count FROM tags
           WHERE path NOT LIKE 'templates/%'
           GROUP BY tag ORDER BY count DESC, tag"""
    )
    return [{"tag": r["tag"], "count": r["count"]} for r in rows]


@router.get("/tags/{tag}/notes")
async def notes_for_tag(request: Request, tag: str):
    db = request.app.state.db
    rows = await db.execute_fetchall(
        """SELECT n.path, n.title, n.modified FROM tags t
           JOIN notes n ON n.path = t.path
           WHERE t.tag = ? AND t.path NOT LIKE 'templates/%'
           ORDER BY n.modified DESC""",
        (tag,),
    )
    return [dict(r) for r in rows]


@router.get("/related/{path:path}")
async def related_notes(request: Request, path: str, k: int = 6):
    """Read-only 'Related Notes' feed: semantically nearest notes to this one,
    flagged by whether the open note already wikilinks them."""
    state = request.app.state
    if not state.semantic.available:
        return {"available": False, "related": []}
    try:
        content = state.store.read(path)
    except NoteError:
        return {"available": True, "related": []}
    from ..vault import fm as fm_mod

    _meta, body = fm_mod.parse(content)
    if not body.strip():
        return {"available": True, "related": []}

    results = await state.semantic.search(body[:2000], k=max(k * 3, 12))
    best: dict[str, dict] = {}
    for r in results:
        p = r["path"]
        if p == path or p.startswith("templates/"):
            continue
        if p not in best or r["score"] > best[p]["score"]:
            best[p] = r

    linked_targets = {
        r["target_lower"]
        for r in await state.db.execute_fetchall(
            "SELECT target_lower FROM links WHERE source = ?", (path,)
        )
    }
    titles = {
        r["path"]: r["title"]
        for r in await state.db.execute_fetchall("SELECT path, title FROM notes")
    }

    related = []
    for p, r in sorted(best.items(), key=lambda kv: -kv[1]["score"])[:k]:
        text = r["text"]
        # Chunk text is prefixed "Title — heading\n\n…" — snip past it.
        snippet = text.split("\n\n", 1)[-1].replace("\n", " ").strip()[:120]
        related.append(
            {
                "path": p,
                "title": titles.get(p) or Path(p).stem,
                "score": r["score"],
                "snippet": snippet,
                "linked": Path(p).stem.lower() in linked_targets,
            }
        )
    return {"available": True, "related": related}


@router.get("/graph")
async def graph(request: Request):
    """Every note and every resolved wikilink, for the graph views.

    Targets resolve by stem — the same rule backlinks use — so a link only
    becomes an edge when a note with that name actually exists."""
    db = request.app.state.db
    notes = await db.execute_fetchall(
        "SELECT path, title FROM notes WHERE path NOT LIKE 'templates/%'"
    )
    by_stem: dict[str, list[str]] = {}
    for row in notes:
        by_stem.setdefault(Path(row["path"]).stem.lower(), []).append(row["path"])
    links = await db.execute_fetchall(
        "SELECT DISTINCT source, target_lower FROM links"
        " WHERE source NOT LIKE 'templates/%'"
    )
    seen: set[tuple[str, str]] = set()
    edges = []
    for row in links:
        for target in by_stem.get(row["target_lower"], []):
            key = (row["source"], target)
            if target != row["source"] and key not in seen:
                seen.add(key)
                edges.append({"source": row["source"], "target": target})
    return {
        "nodes": [{"path": r["path"], "title": r["title"]} for r in notes],
        "edges": edges,
    }


@router.get("/backlinks/{path:path}")
async def backlinks(request: Request, path: str):
    db = request.app.state.db
    stem = Path(path).stem.lower()
    rows = await db.execute_fetchall(
        """SELECT DISTINCT l.source AS path, n.title FROM links l
           JOIN notes n ON n.path = l.source
           WHERE l.target_lower = ? AND l.source != ?
             AND l.source NOT LIKE 'templates/%'
           ORDER BY n.modified DESC""",
        (stem, path),
    )
    return [dict(r) for r in rows]


@router.get("/search")
async def keyword_search(request: Request, q: str = Query(min_length=1), limit: int = 30):
    db = request.app.state.db
    limit = max(1, min(limit, 100))

    async def run(match: str):
        return await db.execute_fetchall(
            """SELECT f.path, n.title,
                      snippet(notes_fts, 2, ?, ?, ' … ', 14) AS snippet
               FROM notes_fts f JOIN notes n ON n.path = f.path
               WHERE notes_fts MATCH ? AND f.path NOT LIKE 'templates/%'
               ORDER BY rank LIMIT ?""",
            (HIGHLIGHT_OPEN, HIGHLIGHT_CLOSE, match, limit),
        )

    try:
        rows = await run(q)
    except Exception:
        # User input isn't valid FTS5 syntax — retry as a literal phrase.
        safe = '"' + q.replace('"', '""') + '"'
        rows = await run(safe)
    return [_safe_result(dict(row)) for row in rows]


def _highlight_segments(text: str, query: str, marked: bool = False) -> list[dict]:
    if marked:
        segments: list[dict] = []
        for part in re.split(f"({HIGHLIGHT_OPEN}|{HIGHLIGHT_CLOSE})", text):
            if not part:
                continue
            if part == HIGHLIGHT_OPEN:
                segments.append({"text": "", "highlight": True, "_marker": "open"})
            elif part == HIGHLIGHT_CLOSE:
                segments.append({"text": "", "highlight": False, "_marker": "close"})
            else:
                active = bool(segments and segments[-1].get("_marker") == "open")
                if segments and segments[-1].get("_marker") in ("open", "close"):
                    marker = segments.pop()
                    if marker["_marker"] == "close":
                        active = False
                segments.append({"text": part, "highlight": active})
        return [
            {"text": segment["text"], "highlight": bool(segment["highlight"])}
            for segment in segments
            if segment.get("text")
        ]
    if not query:
        return [{"text": text, "highlight": False}]
    pattern = re.compile(re.escape(query), re.IGNORECASE)
    segments = []
    cursor = 0
    for match in pattern.finditer(text):
        if match.start() > cursor:
            segments.append({"text": text[cursor : match.start()], "highlight": False})
        segments.append({"text": match.group(0), "highlight": True})
        cursor = match.end()
    if cursor < len(text):
        segments.append({"text": text[cursor:], "highlight": False})
    return segments or [{"text": text, "highlight": False}]


def _safe_result(row: dict, query: str = "") -> dict:
    snippet = str(row.get("snippet") or "")
    plain = snippet.replace(HIGHLIGHT_OPEN, "").replace(HIGHLIGHT_CLOSE, "")
    return {
        **row,
        "snippet": plain,
        "highlights": _highlight_segments(
            snippet if HIGHLIGHT_OPEN in snippet else plain,
            query,
            marked=HIGHLIGHT_OPEN in snippet,
        ),
    }


@router.get("/search/unified")
async def unified_search(
    request: Request,
    q: str = Query(min_length=1),
    limit: int = Query(default=30, ge=1, le=100),
    folder: str | None = None,
    tag: str | None = None,
    result_type: str = "all",
    created_from: str | None = None,
    modified_from: str | None = None,
):
    """Blend exact FTS and semantic matches without ever returning HTML."""
    db = request.app.state.db
    query = q.strip()
    phrase = '"' + query.replace('"', '""') + '"'
    note_scores: dict[str, float] = {}
    note_data: dict[str, dict] = {}

    async def keyword_rows(match: str):
        conditions = [
            "notes_fts MATCH ?",
            "f.path NOT LIKE 'templates/%'",
        ]
        params: list[object] = [match]
        if folder:
            conditions.append("f.path LIKE ?")
            params.append(folder.strip("/") + "/%")
        if created_from:
            conditions.append("n.created >= ?")
            params.append(created_from)
        if modified_from:
            conditions.append("n.modified >= ?")
            params.append(modified_from)
        if tag:
            conditions.append(
                "EXISTS (SELECT 1 FROM tags tt WHERE tt.path=f.path AND tt.tag=?)"
            )
            params.append(tag.lstrip("#"))
        params.append(max(limit * 3, 60))
        return await db.execute_fetchall(
            f"""SELECT f.path, n.title, n.created, n.modified, rank,
                       snippet(notes_fts, 2, ?, ?, ' … ', 22) AS snippet
                FROM notes_fts f JOIN notes n ON n.path=f.path
                WHERE {' AND '.join(conditions)}
                ORDER BY rank LIMIT ?""",
            (HIGHLIGHT_OPEN, HIGHLIGHT_CLOSE, *params),
        )

    if result_type in ("all", "note"):
        exact_conditions = [
            "n.path NOT LIKE 'templates/%'",
            """(
                lower(n.title) = lower(?)
                OR lower(n.path) = lower(?)
                OR lower(n.path) = lower(?)
                OR EXISTS (
                    SELECT 1 FROM tags exact_tag
                    WHERE exact_tag.path=n.path AND lower(exact_tag.tag)=lower(?)
                )
            )""",
        ]
        exact_params: list[object] = [
            query,
            query,
            query if query.lower().endswith(".md") else f"{query}.md",
            query.lstrip("#"),
        ]
        if folder:
            exact_conditions.append("n.path LIKE ?")
            exact_params.append(folder.strip("/") + "/%")
        if created_from:
            exact_conditions.append("n.created >= ?")
            exact_params.append(created_from)
        if modified_from:
            exact_conditions.append("n.modified >= ?")
            exact_params.append(modified_from)
        if tag:
            exact_conditions.append(
                "EXISTS (SELECT 1 FROM tags filter_tag WHERE filter_tag.path=n.path AND filter_tag.tag=?)"
            )
            exact_params.append(tag.lstrip("#"))
        exact_candidates = await db.execute_fetchall(
            f"""SELECT n.path, n.title, n.created, n.modified
                FROM notes n WHERE {' AND '.join(exact_conditions)}
                LIMIT ?""",
            (*exact_params, max(limit, 30)),
        )
        for row in exact_candidates:
            data = dict(row)
            path = data["path"]
            title = str(data.get("title") or Path(path).stem)
            lower = query.lower()
            boost = 0.0
            if title.lower() == lower:
                boost += 5.0
            if path.lower() in (lower, f"{lower}.md"):
                boost += 4.0
            tag_match = await db.execute(
                "SELECT 1 FROM tags WHERE path=? AND lower(tag)=lower(?) LIMIT 1",
                (path, query.lstrip("#")),
            )
            if await tag_match.fetchone() is not None:
                boost += 3.0
            note_scores[path] = note_scores.get(path, 0.0) + boost
            note_data[path] = {
                "type": "note",
                "path": path,
                "title": title,
                "snippet": path,
                "highlights": _highlight_segments(title, query),
                "created": data.get("created"),
                "modified": data.get("modified"),
            }

        try:
            exact = await keyword_rows(query)
        except Exception:
            exact = await keyword_rows(phrase)
        for rank, row in enumerate(exact, start=1):
            data = _safe_result(dict(row), query)
            path = data["path"]
            title = str(data.get("title") or Path(path).stem)
            boost = 0.0
            lower = query.lower()
            if title.lower() == lower:
                boost += 3.0
            elif lower in title.lower():
                boost += 1.3
            if lower in path.lower():
                boost += 0.7
            note_scores[path] = note_scores.get(path, 0.0) + 0.68 / (60 + rank) + boost
            note_data[path] = {
                "type": "note",
                "path": path,
                "title": title,
                "snippet": data["snippet"],
                "highlights": data["highlights"],
                "created": data.get("created"),
                "modified": data.get("modified"),
            }

        if len(query.split()) > 1:
            try:
                phrase_rows = await keyword_rows(phrase)
            except Exception:
                phrase_rows = []
            for rank, row in enumerate(phrase_rows, start=1):
                data = _safe_result(dict(row), query)
                path = data["path"]
                note_scores[path] = note_scores.get(path, 0.0) + 1.2 / (10 + rank)
                if path not in note_data:
                    note_data[path] = {
                        "type": "note",
                        "path": path,
                        "title": data.get("title") or Path(path).stem,
                        "snippet": data["snippet"],
                        "highlights": data["highlights"],
                        "created": data.get("created"),
                        "modified": data.get("modified"),
                    }

        if request.app.state.semantic.available:
            try:
                semantic = await request.app.state.semantic.search(
                    query, k=max(limit * 3, 40)
                )
            except Exception:
                semantic = []
            titles = {
                row["path"]: dict(row)
                for row in await db.execute_fetchall(
                    "SELECT path, title, created, modified FROM notes"
                )
            }
            for rank, row in enumerate(semantic, start=1):
                path = row["path"]
                info = titles.get(path)
                if not info or path.startswith("templates/"):
                    continue
                if folder and not path.startswith(folder.strip("/") + "/"):
                    continue
                if created_from and str(info.get("created") or "") < created_from:
                    continue
                if modified_from and str(info.get("modified") or "") < modified_from:
                    continue
                if tag:
                    has_tag = await db.execute(
                        "SELECT 1 FROM tags WHERE path=? AND tag=? LIMIT 1",
                        (path, tag.lstrip("#")),
                    )
                    if await has_tag.fetchone() is None:
                        continue
                note_scores[path] = note_scores.get(path, 0.0) + 0.32 / (60 + rank)
                if path not in note_data:
                    snippet = str(row.get("text") or "").replace("\n", " ").strip()[:260]
                    note_data[path] = {
                        "type": "note",
                        "path": path,
                        "title": info.get("title") or Path(path).stem,
                        "snippet": snippet,
                        "highlights": _highlight_segments(snippet, query),
                        "created": info.get("created"),
                        "modified": info.get("modified"),
                    }

    results = [
        {**note_data[path], "score": score}
        for path, score in note_scores.items()
    ]

    if result_type in ("all", "task"):
        clauses = ["t.text LIKE ?"]
        params: list[object] = [f"%{query}%"]
        if folder:
            clauses.append("t.path LIKE ?")
            params.append(folder.strip("/") + "/%")
        if tag:
            clauses.append(
                "EXISTS (SELECT 1 FROM tags task_tag WHERE task_tag.path=t.path AND task_tag.tag=?)"
            )
            params.append(tag.lstrip("#"))
        if created_from:
            clauses.append("t.created_at >= ?")
            params.append(created_from)
        if modified_from:
            clauses.append("n.modified >= ?")
            params.append(modified_from)
        task_rows = await db.execute_fetchall(
            f"""SELECT t.* FROM tasks t JOIN notes n ON n.path=t.path
                WHERE {' AND '.join(clauses)}
                ORDER BY CASE t.status WHEN 'in_progress' THEN 0 WHEN 'todo' THEN 1 ELSE 2 END,
                         t.due, t.path LIMIT ?""",
            (*params, limit),
        )
        for rank, row in enumerate(task_rows, start=1):
            task = dict(row)
            task_score = 1.0 / (20 + rank)
            if task["text"].casefold() == query.casefold():
                task_score += 3.0
            if query.casefold() in task["path"].casefold():
                task_score += 0.7
            results.append(
                {
                    "type": "task",
                    "id": task["id"],
                    "path": task["path"],
                    "title": task["text"],
                    "snippet": task["path"],
                    "highlights": _highlight_segments(task["text"], query),
                    "status": task["status"],
                    "due": task["due"],
                    "score": task_score,
                }
            )

    results.sort(key=lambda item: (-float(item.get("score") or 0), item["title"].lower()))
    return {
        "query": query,
        "semantic_available": request.app.state.semantic.available,
        "results": results[:limit],
    }


@router.get("/templates")
async def list_templates(request: Request):
    return tpl.list_templates(request.app.state.settings.vault_dir)


@router.post("/daily")
@router.post("/daily/{date}")
async def open_daily(request: Request, date: str | None = None):
    """Get-or-create the daily note for `date` (default: today)."""
    if date is None:
        try:
            today = datetime.now(ZoneInfo(request.app.state.timezone)).date()
        except ZoneInfoNotFoundError:
            today = date_cls.today()
        date = today.isoformat()
    try:
        date_cls.fromisoformat(date)
    except ValueError:
        raise NoteError(400, f"Invalid date: {date}")

    state = request.app.state
    path = f"{DAILY_DIR}/{date}.md"
    if state.store.exists(path):
        return {"path": path, "created": False}

    content = tpl.render(state.settings.vault_dir, "daily", title=date, date=date)
    written = state.store.create(path, content)
    await state.indexer.index_note(path, written)
    state.autocommit.touch()
    return {"path": path, "created": True}
