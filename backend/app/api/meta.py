"""Metadata endpoints: tags, backlinks, keyword search, templates, daily notes."""
from datetime import date as date_cls, datetime
import logging
from pathlib import Path
import re
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field, ValidationError

from ..index.db import content_hash, fold
from ..index.highlight import (
    HIGHLIGHT_CLOSE,
    HIGHLIGHT_OPEN,
    highlight_segments as _highlight_segments,
    safe_result as _safe_result,
)
from ..index.paper_index import page_view_url as paper_url
from ..paper_store import paper_page_url
from ..overseer.housekeeping import DEFAULT_ENABLED as HOUSEKEEPING_DEFAULT
from ..overseer.runtime_config import (
    REASONING_LEVELS,
    RECIPES_REASONING_DEFAULTS,
    effective_ai,
    effective_autocorrect_model,
    effective_background_model,
    effective_media_model,
    effective_recipes_model,
    load_ai_config,
    model_ids,
    monthly_token_budget,
    provider_info,
    recipes_reasoning,
    recipes_reasoning_setting,
    save_ai_config,
)
from ..vault import templates as tpl
from ..vault.internal import atomic_json, read_json
from ..vault.notes import NoteStore
from ..vault.notes import NoteError

router = APIRouter(prefix="/api", tags=["meta"])
log = logging.getLogger(__name__)

DAILY_DIR = "daily"


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
# Stands in for a colour a stored theme lacks — the same grey the client's
# themeFromCurrent uses for a variable it cannot read.
PLACEHOLDER_COLOR = "#888888"
MAX_SAVED_THEMES = 50


class AppearanceSettingsBody(BaseModel):
    theme: ThemeId = "system"
    font: FontId = "system"
    intoxicate: bool = False
    # Scanlines and grain over the whole window, on dark surfaces only.
    texture: bool = False
    custom_theme: dict[str, Any] | None = None
    saved_themes: list[dict[str, Any]] = Field(
        default_factory=list, max_length=MAX_SAVED_THEMES
    )
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


def _custom_theme(
    value: Any, *, salvage: dict[str, int] | None = None
) -> dict[str, Any] | None:
    """Validate a palette. Strict for user input; with `salvage` (the counters
    of a stored-config read) a missing or invalid colour is backfilled and
    counted instead of raising, so adding a THEME_COLOR_VARS entry does not
    invalidate every theme saved before it. A value without a usable mode or
    colour map is still unusable: None when salvaging."""
    if value is None:
        return None
    if not isinstance(value, dict) or value.get("mode") not in ("light", "dark"):
        if salvage is not None:
            return None
        raise NoteError(400, "Invalid custom theme")
    colors = value.get("colors")
    if not isinstance(colors, dict):
        if salvage is not None:
            return None
        raise NoteError(400, "Invalid custom theme colors")
    normalized: dict[str, str] = {}
    for name in THEME_COLOR_VARS:
        color = colors.get(name)
        if not isinstance(color, str) or not HEX_COLOR.fullmatch(color):
            if salvage is None:
                raise NoteError(400, f"Invalid custom theme color: {name}")
            salvage["backfilled"] += 1
            color = PLACEHOLDER_COLOR
        normalized[name] = color.lower()
    return {"mode": value["mode"], "colors": normalized}


def _saved_theme(
    entry: Any, seen: set[str], *, salvage: dict[str, int] | None = None
) -> dict[str, Any]:
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
    saved_theme = _custom_theme(entry.get("theme"), salvage=salvage)
    if saved_theme is None:
        raise NoteError(400, "Invalid saved theme palette")
    seen.add(theme_id)
    return {"id": theme_id, "name": name.strip()[:40], "theme": saved_theme}


def _appearance(body: AppearanceSettingsBody) -> dict[str, Any]:
    seen: set[str] = set()
    saved = [_saved_theme(entry, seen) for entry in body.saved_themes]
    active = (
        body.active_saved_theme_id
        if body.active_saved_theme_id in seen
        else None
    )
    return {
        "theme": body.theme,
        "font": body.font,
        "intoxicate": body.intoxicate,
        "texture": body.texture,
        "custom_theme": _custom_theme(body.custom_theme),
        "saved_themes": saved,
        "active_saved_theme_id": active,
    }


def salvage_appearance(raw: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int]]:
    """Normalize a stored appearance block field by field and entry by entry.

    Reads must not turn one bad value into defaults: the layout seeds the
    server from this device's localStorage when a read says unconfigured, and
    OMNIA's appearance tools write back whatever they read, so a reset here
    replaced every saved theme. A bad scalar falls back to its own default,
    missing colours are backfilled, and only structurally broken saved themes
    are dropped. Returns the normalized block and {"dropped", "backfilled"}.
    """
    counts = {"dropped": 0, "backfilled": 0}
    lost: list[str] = []
    fields: dict[str, Any] = {}
    for key in ("theme", "font", "intoxicate", "texture"):
        if key not in raw:
            continue
        try:
            checked = AppearanceSettingsBody.model_validate({key: raw[key]})
        except ValidationError:
            lost.append(key)
            continue
        fields[key] = getattr(checked, key)
    body = AppearanceSettingsBody.model_validate(fields)

    custom_theme = _custom_theme(raw.get("custom_theme"), salvage=counts)
    if raw.get("custom_theme") is not None and custom_theme is None:
        lost.append("custom_theme")

    entries = raw.get("saved_themes")
    if entries is not None and not isinstance(entries, list):
        lost.append("saved_themes")
    saved: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries if isinstance(entries, list) else []):
        if len(saved) >= MAX_SAVED_THEMES:
            lost.append(f"saved_themes[{index}]")
            continue
        try:
            saved.append(_saved_theme(entry, seen, salvage=counts))
        except NoteError:
            lost.append(f"saved_themes[{index}]")

    active = raw.get("active_saved_theme_id")
    counts["dropped"] = len(lost)
    if lost or counts["backfilled"]:
        log.warning(
            "appearance config salvaged: dropped=%d backfilled=%d %s",
            counts["dropped"],
            counts["backfilled"],
            lost,
        )
    return (
        {
            "theme": body.theme,
            "font": body.font,
            "intoxicate": body.intoxicate,
            "texture": body.texture,
            "custom_theme": custom_theme,
            "saved_themes": saved,
            "active_saved_theme_id": (
                active if isinstance(active, str) and active in seen else None
            ),
        },
        counts,
    )


def appearance_payload(raw: Any) -> dict[str, Any]:
    """What a read of the stored appearance answers, for the route and OMNIA.

    `configured` stays true whenever a block is stored, however much of it had
    to be salvaged — false is the client's cue to overwrite it with this
    device's local copy. Salvage counters appear only when non-zero.
    """
    if not isinstance(raw, dict):
        return {"configured": False, **_appearance(AppearanceSettingsBody())}
    normalized, counts = salvage_appearance(raw)
    return {
        "configured": True,
        **normalized,
        **{key: value for key, value in counts.items() if value},
    }


@router.get("/settings/general")
async def general_settings(request: Request):
    return {"timezone": request.app.state.timezone}


@router.put("/settings/general")
async def save_general_settings(request: Request, body: GeneralSettingsBody):
    try:
        ZoneInfo(body.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        # An empty or path-like name is a ValueError, which surfaced as a 500.
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
    recipes_model: str | None = None
    # Reasoning overrides: "inherit" clears the role back to the chat knob
    # (recipes back to its own defaults: medium imports, low drawings).
    background_reasoning: str | None = None
    media_reasoning: str | None = None
    autocorrect_reasoning: str | None = None
    recipes_reasoning: str | None = None


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
        "recipes_model": effective_recipes_model(state.settings, config),
        "chat_model": model,
        # Raw override dict — an absent role means "inherit the chat knob",
        # which the UI renders differently from an explicit level.
        "reasoning_levels": dict(config.get("reasoning_levels") or {}),
        # Recipes does not inherit the chat knob. The owner's level, or
        # "inherit" (what PUT takes to clear it) when unset; unset means
        # medium for imports and low for drawings, as the next field says.
        "recipes_reasoning": recipes_reasoning_setting(config) or "inherit",
        "recipes_reasoning_in_effect": {
            job: recipes_reasoning(config, job) for job in RECIPES_REASONING_DEFAULTS
        },
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
        or body.recipes_model is not None
        or body.background_reasoning is not None
        or body.media_reasoning is not None
        or body.autocorrect_reasoning is not None
        or body.recipes_reasoning is not None
    ):
        config = load_ai_config(state.settings.data_dir)
        provider_id, _key, model = effective_ai(state.settings, config)
        if body.monthly_token_budget is not None:
            config["monthly_token_budget"] = body.monthly_token_budget
        for value, key in (
            (body.background_model, "background_models"),
            (body.media_model, "media_models"),
            (body.autocorrect_model, "autocorrect_models"),
            (body.recipes_model, "recipes_models"),
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
            (body.recipes_reasoning, "recipes"),
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
    return appearance_payload(raw)


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
    # The push can only send what the autocommitter committed; a commit that
    # keeps failing otherwise looks like "nothing new to push".
    committer = request.app.state.autocommit
    autocommit = {
        "last_commit_at": committer.last_commit_at,
        "last_error": committer.last_error,
        "failures": committer.failures,
    }
    pusher = request.app.state.backup
    if pusher is None:
        return {"configured": False, "autocommit": autocommit}
    return {
        "configured": True,
        "autocommit": autocommit,
        "remote": _redact_remote(pusher.url),
        "interval": pusher.interval,
        "last_push": pusher.last_push,
        "last_error": pusher.last_error,
        "failures": pusher.failures,
        "failing_since": pusher.failing_since,
        "stale": pusher.stale,
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


@router.get("/tags/{tag:path}/notes")
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
    if not getattr(state.semantic, "ready", True):
        # Never hold a note open on the cold model load: start it, answer
        # empty, and let the panel ask again.
        warm = getattr(state.semantic, "warm", None)
        if warm is not None:
            warm()
        return {"available": True, "related": [], "state": "warming"}
    try:
        content = state.store.read(path)
    except NoteError:
        return {"available": True, "related": []}
    from ..vault import fm as fm_mod

    _meta, body = fm_mod.parse(content)
    if not body.strip():
        return {"available": True, "related": []}

    results = await state.semantic.search(body[:2000], k=max(k * 3, 12))
    titles = {
        r["path"]: r["title"]
        for r in await state.db.execute_fetchall("SELECT path, title FROM notes")
    }
    best: dict[str, dict] = {}
    for r in results:
        p = r["path"]
        # Only notes that exist: a row here opens as /note/<path> and offers
        # '+ link'; vectors can outlive a note until the next boot's backfill,
        # and a Paper page is no /note/ link.
        if (
            p == path
            or p.startswith("templates/")
            or not p.endswith(".md")
            or p not in titles
        ):
            continue
        if p not in best or r["score"] > best[p]["score"]:
            best[p] = r

    linked_paths = {
        r["resolved_path"]
        for r in await state.db.execute_fetchall(
            "SELECT resolved_path FROM links WHERE source = ?", (path,)
        )
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
                "linked": p in linked_paths,
            }
        )
    return {"available": True, "related": related}


@router.get("/graph")
async def graph(request: Request):
    """Every note and every resolved wikilink, for the graph views.

    Edges follow links.resolved_path — the note a click on the link opens,
    the same rule backlinks use — so a link only becomes an edge when it
    actually reaches a note."""
    db = request.app.state.db
    notes = await db.execute_fetchall(
        "SELECT path, title FROM notes WHERE path NOT LIKE 'templates/%'"
    )
    nodes = {row["path"] for row in notes}
    links = await db.execute_fetchall(
        "SELECT DISTINCT source, resolved_path FROM links"
        " WHERE source NOT LIKE 'templates/%' AND resolved_path IS NOT NULL"
    )
    edges = [
        {"source": row["source"], "target": row["resolved_path"]}
        for row in links
        if row["resolved_path"] in nodes and row["resolved_path"] != row["source"]
    ]
    return {
        "nodes": [{"path": r["path"], "title": r["title"]} for r in notes],
        "edges": edges,
    }


@router.get("/backlinks/{path:path}")
async def backlinks(request: Request, path: str):
    db = request.app.state.db
    rows = await db.execute_fetchall(
        """SELECT DISTINCT l.source AS path, n.title FROM links l
           JOIN notes n ON n.path = l.source
           WHERE l.resolved_path = ? AND l.source != ?
             AND l.source NOT LIKE 'templates/%'
           ORDER BY n.modified DESC""",
        (path, path),
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
    include_paper: bool = False,
):
    """Blend exact FTS and semantic matches without ever returning HTML."""
    db = request.app.state.db
    query = q.strip()
    phrase = '"' + query.replace('"', '""') + '"'
    note_scores: dict[str, float] = {}
    note_data: dict[str, dict] = {}
    # Paper pages score on the same curve as notes so the three kinds of hit
    # interleave honestly in one ranked list.
    paper_scores: dict[str, float] = {}
    paper_data: dict[str, dict] = {}
    paper_index = getattr(request.app.state, "paper_index", None)
    # Handwriting is opt-in: installed desktops that predate Paper never send
    # the flag, label every non-task row a Note and open it as /note/<path>,
    # which a page directory is not.
    paper_wanted = include_paper or result_type == "paper"

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
                fold(n.title) = fold(?)
                OR fold(n.path) = fold(?)
                OR fold(n.path) = fold(?)
                OR EXISTS (
                    SELECT 1 FROM tags exact_tag
                    WHERE exact_tag.path=n.path AND fold(exact_tag.tag)=fold(?)
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
            folded = fold(query)
            boost = 0.0
            if fold(title) == folded:
                boost += 5.0
            if fold(path) in (folded, f"{folded}.md"):
                boost += 4.0
            tag_match = await db.execute(
                "SELECT 1 FROM tags WHERE path=? AND fold(tag)=fold(?) LIMIT 1",
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

    def _paper_row(row: dict, path: str) -> dict:
        """One `paper_pages` row (joined with its notebook) as a result."""
        notebook_title = row.get("notebook_title") or Path(
            str(row.get("notebook") or "")
        ).stem
        return {
            "type": "paper",
            "path": path,
            "notebook": row.get("notebook"),
            "page_id": row.get("page_id"),
            "title": f"{notebook_title} · {row.get('title') or ''}".strip(),
            "url": paper_url(path),
            "durable_url": paper_page_url(
                str(row.get("notebook_id") or ""), str(row.get("page_id") or "")
            ),
            "created": row.get("created"),
            "modified": row.get("modified"),
        }

    def _paper_allowed(row: dict, path: str) -> bool:
        """The shared filters, minus `tag` — a page carries no tags of its own,
        so a tag filter excludes handwriting entirely rather than ignoring it."""
        if tag:
            return False
        if folder and not path.startswith(folder.strip("/") + "/"):
            return False
        if created_from and str(row.get("created") or "") < created_from:
            return False
        if modified_from and str(row.get("modified") or "") < modified_from:
            return False
        return True

    async def _paper_rows(paths: list[str]) -> dict[str, dict]:
        if not paths:
            return {}
        holes = ",".join("?" * len(paths))
        return {
            row["path"]: dict(row)
            for row in await db.execute_fetchall(
                    f"""SELECT p.path, p.notebook, p.page_id, p.title, p.created,
                           p.modified, nb.id AS notebook_id,
                           nb.title AS notebook_title
                    FROM paper_pages p
                    LEFT JOIN paper_notebooks nb ON nb.path = p.notebook
                    WHERE p.path IN ({holes})""",
                tuple(paths),
            )
        }

    if paper_wanted and result_type in ("all", "paper") and paper_index is not None:
        try:
            hits = await paper_index.search(query, limit=max(limit * 2, 40))
        except Exception:
            hits = []
        meta_rows = await _paper_rows([hit["path"] for hit in hits])
        for rank, hit in enumerate(hits, start=1):
            path = hit["path"]
            row = meta_rows.get(path)
            if row is None or not _paper_allowed(row, path):
                continue
            entry = _paper_row(row, path)
            boost = 0.0
            lower = query.lower()
            if entry["title"].lower() == lower:
                boost += 3.0
            elif lower in entry["title"].lower():
                boost += 1.3
            paper_scores[path] = paper_scores.get(path, 0.0) + 0.68 / (60 + rank) + boost
            paper_data[path] = {
                **entry,
                "snippet": hit["snippet"],
                "highlights": hit["highlights"],
            }

    semantic_index = request.app.state.semantic
    if not semantic_index.available:
        semantic_state = "unavailable"
    elif getattr(semantic_index, "ready", True):
        semantic_state = "ready"
    else:
        # The cold model load takes seconds and the keyword results are ready
        # now: start it, answer without the semantic pass, and let the page
        # ask again.
        semantic_state = "warming"
        warm = getattr(semantic_index, "warm", None)
        if warm is not None:
            warm()

    # One semantic pass feeds both kinds. A hit whose path is not a note is
    # looked up in `paper_pages` before it is discarded: the vector index keys
    # handwriting by its page directory, so a meaning-based query reaches a
    # page nobody ever typed a word of.
    if semantic_state == "ready" and result_type in (
        "all",
        "note",
        "paper",
    ):
        try:
            semantic = await semantic_index.search(
                query, k=max(limit * 3, 40), include_paper=paper_wanted
            )
        except Exception:
            semantic = []
        titles = {
            row["path"]: dict(row)
            for row in await db.execute_fetchall(
                "SELECT path, title, created, modified FROM notes"
            )
        }
        unknown = [
            row["path"]
            for row in semantic
            if row["path"] not in titles and row["path"].startswith("paper/")
        ]
        paper_meta = (
            await _paper_rows(unknown)
            if unknown and paper_wanted and result_type in ("all", "paper")
            else {}
        )
        for rank, row in enumerate(semantic, start=1):
            path = row["path"]
            info = titles.get(path)
            if info is None:
                page = paper_meta.get(path)
                if page is None or not _paper_allowed(page, path):
                    continue
                paper_scores[path] = paper_scores.get(path, 0.0) + 0.32 / (60 + rank)
                if path not in paper_data:
                    snippet = str(row.get("text") or "").replace("\n", " ").strip()[:260]
                    paper_data[path] = {
                        **_paper_row(page, path),
                        "snippet": snippet,
                        "highlights": _highlight_segments(snippet, query),
                    }
                continue
            if path.startswith("templates/") or result_type not in ("all", "note"):
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
        {**note_data[path], "score": score} for path, score in note_scores.items()
    ] + [{**paper_data[path], "score": score} for path, score in paper_scores.items()]

    if result_type in ("all", "task"):
        clauses = ["instr(fold(t.text), fold(?)) > 0"]
        params: list[object] = [query]
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
        "semantic_state": semantic_state,
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
        # fromisoformat also takes `20260921` and `2026-W38-1`; the note is
        # always named by the canonical form, the one the calendar looks up.
        date = date_cls.fromisoformat(date).isoformat()
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
    state.live.note_changed(path, content_hash(written))
    return {"path": path, "created": True}
