"""Recipes: the seventh section, stored as notes.

A recipe is a note under ``recipes/`` whose frontmatter carries ``recipe: 1``
(the integer; ``recipe: true``, or the marker on a note elsewhere, is an
ordinary note). The whole structured record lives in frontmatter and the body
is the recipe's free notes:

    title, created, modified    the note's own keys
    recipe: 1                   the marker
    servings                    RecipeIn.base_servings
    prep_minutes, cook_minutes  omitted when null
    recipe_tags                 lowercased, sorted, deduped; omitted when empty
    source_type, source_url     RecipeIn.source; omitted when there is none
    thumbnail                   RecipeIn.source.thumbnail; omitted when null
    ingredients, steps          lists of mappings; defaults left out
    art, art_colors             lists of row strings; omitted when empty
    art_palette                 colour key -> hex; omitted when empty

New recipes land in the protected ``recipes/`` folder, named the way media
items are. The vault path is the recipe's identity, and a rename changes the
frontmatter title only.

Reading is forgiving: a bad field degrades to its default and a bad
ingredient or step is dropped, so one hand-edited note never takes indexing
or the menu down. Writing goes through the models in ``recipe_models.py``,
and a rewrite patches only the recipe's own keys: whatever else the user put
in the frontmatter stays byte for byte.

Imported by the indexer, so this module must not import from ``app.index``.
The service functions at the bottom use only what ``app.state`` hands them.
"""
import hashlib
import json
import logging
import math
import re
import unicodedata
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import ValidationError

from . import media_lookup
from .media_items import media_slug
from .recipe_models import (
    UNITS,
    Drawing,
    Ingredient,
    Recipe,
    RecipeCard,
    RecipeIn,
    Source,
    Step,
)
from .vault import fm, names
from .vault.internal import atomic_json, read_json
from .vault.notes import NoteError, NoteStore

log = logging.getLogger(__name__)

RECIPE_KEY = "recipe"
RECIPES_DIR = "recipes"
# The menu order in .exscalia/config.json, beside media_profile.
ORDER_KEY = "recipe_order"

# Every frontmatter key the format owns besides title/created/modified. A
# rewrite sets or removes exactly these and leaves every other key alone.
RECORD_KEYS = (
    RECIPE_KEY,
    "servings",
    "prep_minutes",
    "cook_minutes",
    "recipe_tags",
    "source_type",
    "source_url",
    "thumbnail",
    "ingredients",
    "steps",
)
ART_KEYS = ("art", "art_colors", "art_palette")

_SCALINGS = ("linear", "whole", "to_taste")
_SOURCE_TYPES = ("typed", "text", "website", "photo", "dish_photo", "youtube", "reel", "tiktok", "video")
_MAX_TITLE = 200
_MAX_ART = 4000
_MAX_ID = 64
_MAX_MINUTES = 100_000
_ID_RE = re.compile(r"[^\w]+")


def is_recipe(meta: dict) -> bool:
    """The frontmatter half of the test. Strictly the integer 1: `recipe:
    true` or `recipe: yes` typed in the editor stays an ordinary note rather
    than vanishing into the menu."""
    value = meta.get(RECIPE_KEY)
    return type(value) is int and value == 1


def in_recipes_folder(path: str) -> bool:
    """Under `recipes/`, nested folders included. The folder is spelled
    exactly so, as `NoteStore.TITLE_EXEMPT_ROOTS`, the frontend's routes and
    OMNIA's tools all read it: on production Linux `Recipes/` is another
    folder, and a note there keeps its filename as its title."""
    first, sep, _rest = path.partition("/")
    return bool(sep) and first == RECIPES_DIR


def is_recipe_note(path: str, meta: dict) -> bool:
    """Whether the note at `path` is a recipe: `recipe: 1` and a path under
    `recipes/`. The recipe page lives at /recipes/<path>, so a marked note
    anywhere else is an ordinary note, and moving it back makes it a recipe
    again."""
    return in_recipes_folder(path) and is_recipe(meta)


def recipe_slug(title: str) -> str:
    """A title reduced to a filename stem safe on every synced OS: the media
    slug, so both sections name their notes by one spec."""
    return media_slug(title)


def next_recipe_path(title: str, taken: set[str]) -> str:
    """`recipes/<slug>.md`, stepping past anything already there, compared
    as the store compares siblings (names.identity_key): 'Straße' against
    'Strasse.md', or an NFC title against an NFD file, was a 409."""
    base = f"{RECIPES_DIR}/{recipe_slug(title)}"
    candidate = f"{base}.md"
    number = 1
    lowered = {names.identity_key(path) for path in taken}
    while names.identity_key(candidate) in lowered:
        number += 1
        candidate = f"{base} {number}.md"
    return candidate


# -- writing --------------------------------------------------------------------


def _normalize(recipe: RecipeIn) -> RecipeIn:
    """The stored form: one-line title, tags lowercased, stripped, deduped and
    sorted, and no amount JSON could not carry back."""
    title = " ".join(recipe.title.split())
    if not title:
        raise ValueError("A recipe needs a title")
    ingredients = [
        i if i.amount is None or math.isfinite(i.amount) else i.model_copy(update={"amount": None})
        for i in recipe.ingredients
    ]
    return recipe.model_copy(
        update={
            "title": title,
            "tags": sorted({t.strip().lower() for t in recipe.tags if t.strip()}),
            "ingredients": ingredients,
        }
    )


def _ingredient_meta(i: Ingredient) -> dict[str, Any]:
    out: dict[str, Any] = {"id": i.id}
    if i.amount is not None:
        out["amount"] = float(i.amount)
    out["unit"] = i.unit
    out["item"] = i.item
    if i.prep:
        out["prep"] = i.prep
    if i.scaling != "linear":
        out["scaling"] = i.scaling
    if i.estimated:
        out["estimated"] = True
    if i.group is not None:
        out["group"] = i.group
    return out


def _step_meta(s: Step) -> dict[str, Any]:
    out: dict[str, Any] = {"text": s.text}
    if s.timer_seconds is not None:
        out["timer_seconds"] = s.timer_seconds
    return out


def _record_meta(recipe: RecipeIn) -> dict[str, Any]:
    """The RECORD_KEYS a recipe writes, in contract order; a key left out is
    one the note must not carry."""
    meta: dict[str, Any] = {RECIPE_KEY: 1, "servings": recipe.base_servings}
    if recipe.prep_minutes is not None:
        meta["prep_minutes"] = recipe.prep_minutes
    if recipe.cook_minutes is not None:
        meta["cook_minutes"] = recipe.cook_minutes
    if recipe.tags:
        meta["recipe_tags"] = list(recipe.tags)
    if recipe.source is not None:
        meta["source_type"] = recipe.source.type
        if recipe.source.url:
            meta["source_url"] = recipe.source.url
        if recipe.source.thumbnail:
            meta["thumbnail"] = recipe.source.thumbnail
    meta["ingredients"] = [_ingredient_meta(i) for i in recipe.ingredients]
    meta["steps"] = [_step_meta(s) for s in recipe.steps]
    return meta


def _set_art(meta: dict, art: str, colors: str, palette: dict[str, str]) -> None:
    """Rows as a YAML list, so each one is quoted on its own line and keeps
    its trailing spaces."""
    if len(art) > _MAX_ART or len(colors) > _MAX_ART:
        raise ValueError(f"A drawing is limited to {_MAX_ART} characters")
    for key, value in (("art", art), ("art_colors", colors)):
        if value:
            meta[key] = value.split("\n")
        else:
            meta.pop(key, None)
    if palette:
        meta["art_palette"] = {str(k): str(v) for k, v in palette.items()}
    else:
        meta.pop("art_palette", None)


def _body(notes: str) -> str:
    # LF only: notes are read with universal newlines, so a CR written here
    # reads back as other text and the revision a save answers never matches.
    text = notes.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
    return f"{text}\n" if text.strip() else ""


def build_recipe_content(recipe: RecipeIn, *, created: str | None = None) -> str:
    """Full note text for a new recipe. Raises ValueError for a blank title."""
    recipe = _normalize(recipe)
    now = fm.now_iso()
    meta: dict[str, Any] = {"title": recipe.title, "created": created or now, "modified": now}
    meta.update(_record_meta(recipe))
    _set_art(meta, recipe.art, recipe.art_colors, recipe.art_palette)
    return fm.serialize(meta, _body(recipe.notes))


def apply_recipe(content: str, recipe: RecipeIn) -> str:
    """Rewrite an existing note as `recipe`. Keeps `created`, every key the
    format does not own, and the stored drawing when `recipe.art` is empty;
    the body becomes `recipe.notes`. Raises ValueError (FrontmatterError for
    a block that does not load)."""
    meta, _body_text = fm.parse(content, strict=True)
    recipe = _normalize(recipe)
    meta["title"] = recipe.title
    record = _record_meta(recipe)
    for key in RECORD_KEYS:
        if key in record:
            meta[key] = record[key]
        else:
            meta.pop(key, None)
    if recipe.art:
        _set_art(meta, recipe.art, recipe.art_colors, recipe.art_palette)
    return fm.serialize(meta, _body(recipe.notes))


def apply_art(content: str, drawing: Drawing) -> str:
    """Store a drawing (an empty one clears it), leaving everything else."""
    meta, body = fm.parse(content, strict=True)
    _set_art(meta, drawing.art, drawing.art_colors, drawing.art_palette)
    return fm.serialize(meta, body)


def carry_art(content: str, source: str) -> str:
    """`content` with the drawing `source` holds, its ART_KEYS entries taken
    as stored there (none when it has none). Raises ValueError for a block
    that does not load."""
    meta, body = fm.parse(content, strict=True)
    theirs, _body = fm.parse(source, strict=True)
    for key in ART_KEYS:
        if key not in theirs:
            meta.pop(key, None)
    for key in theirs:
        if key in ART_KEYS:
            meta[key] = theirs[key]
    return fm.serialize(meta, body)


def only_art_changed(path: str, recorded: str, current: str) -> bool:
    """Whether the note at `path` is the recipe `recorded` with nothing
    changed since but its drawing, if that. A drawing lands by itself
    seconds after a recipe is written (recipe_art), so Undo of the write
    sets it aside; any other change, or a note that is no recipe, is False."""
    try:
        meta, _body = fm.parse(recorded, strict=True)
        return is_recipe_note(path, meta) and (
            current == recorded or carry_art(recorded, current) == current
        )
    except ValueError:
        return False


# -- reading ----------------------------------------------------------------------


def _revision(content: str) -> str:
    # index.db.content_hash, which this module may not import.
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _scalar(value: Any) -> bool:
    return isinstance(value, (str, int, float, date)) and not isinstance(value, bool)


def _text(value: Any) -> str | None:
    """A scalar as text; None for a missing value, a mapping or a list."""
    if isinstance(value, bool):
        return str(value).lower()
    return str(value) if _scalar(value) else None


def _int(value: Any, lo: int, hi: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    if isinstance(value, float):
        if not value.is_integer():
            return None
        value = int(value)
    try:
        n = int(value)
    except ValueError:
        return None
    return n if lo <= n <= hi else None


def _amount(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        n = float(value)
    except ValueError:
        return None
    return n if math.isfinite(n) else None


def _stamp(value: Any) -> str:
    """created/modified as text. An unquoted timestamp loads as a datetime;
    give it back in the form the app writes."""
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc)
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, date):
        return value.isoformat()
    return _text(value) or ""


def _tags(value: Any) -> list[str]:
    if isinstance(value, str):
        raw = value.split(",")
    elif isinstance(value, list):
        raw = [t for t in (_text(v) for v in value) if t is not None]
    else:
        raw = []
    return sorted({t.strip().lower() for t in raw if t.strip()})


def _derived_id(item: str) -> str:
    return _ID_RE.sub("-", item.casefold()).strip("-")[:_MAX_ID] or "item"


def _ingredient(raw: Any) -> Ingredient | None:
    if not isinstance(raw, dict):
        return None
    item = _text(raw.get("item"))
    if not item or not item.strip():
        return None
    ident = (_text(raw.get("id")) or "").strip()[:_MAX_ID] or _derived_id(item)
    unit = (_text(raw.get("unit")) or "").strip().lower()
    if unit not in UNITS and unit.endswith("s") and unit[:-1] in UNITS:
        unit = unit[:-1]  # "cups", "lbs", "cloves" typed by hand
    scaling = raw.get("scaling")
    try:
        return Ingredient(
            id=ident,
            amount=_amount(raw.get("amount")),
            unit=unit if unit in UNITS else "",
            item=item,
            prep=_text(raw.get("prep")) or "",
            scaling=scaling if scaling in _SCALINGS else "linear",
            estimated=raw.get("estimated") is True,
            group=_text(raw.get("group")),
        )
    except ValidationError:
        return None


def _step(raw: Any) -> Step | None:
    if isinstance(raw, dict):
        text = _text(raw.get("text"))
        timer = _int(raw.get("timer_seconds"), 1, 10**7)
    else:
        text, timer = (raw if isinstance(raw, str) else None), None
    if not text or not text.strip():
        return None
    try:
        return Step(text=text, timer_seconds=timer)
    except ValidationError:
        return None


def _source(meta: dict) -> Source | None:
    kind = meta.get("source_type")
    kind = kind if kind in _SOURCE_TYPES else None
    url = _text(meta.get("source_url")) or None
    thumbnail = _text(meta.get("thumbnail")) or None
    if kind is None and url is None and thumbnail is None:
        return None
    return Source(type=kind or "typed", url=url, thumbnail=thumbnail)


def _rows(value: Any) -> str:
    """An art grid back to one string. A grid with a row that is not text is
    not a drawing any more, and an overlong one would not validate."""
    if isinstance(value, str):
        text = value.rstrip("\n")
    elif isinstance(value, list) and all(v is None or _scalar(v) for v in value):
        text = "\n".join("" if v is None else str(v) for v in value)
    else:
        return ""
    return text if len(text) <= _MAX_ART else ""


def _palette(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items() if _scalar(k) and _scalar(v)}


def _list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _recipe_in(meta: dict, body: str, fallback_title: str) -> RecipeIn:
    """The forgiving read: every field already valid on its own, so building
    the model cannot fail on anything a hand edit leaves behind."""
    title = " ".join((_text(meta.get("title")) or "").split())[:_MAX_TITLE]
    title = title or " ".join(fallback_title.split())[:_MAX_TITLE] or "Untitled"
    values = dict(
        base_servings=_int(meta.get("servings"), 1, 100) or 2,
        prep_minutes=_int(meta.get("prep_minutes"), 0, _MAX_MINUTES),
        cook_minutes=_int(meta.get("cook_minutes"), 0, _MAX_MINUTES),
        ingredients=[i for i in map(_ingredient, _list(meta.get("ingredients"))) if i],
        steps=[s for s in map(_step, _list(meta.get("steps"))) if s],
        notes=body.rstrip("\r\n"),
        tags=_tags(meta.get("recipe_tags")),
        source=_source(meta),
        art=_rows(meta.get("art")),
        art_colors=_rows(meta.get("art_colors")),
        art_palette=_palette(meta.get("art_palette")),
    )
    try:
        return RecipeIn(title=title, **values)
    except ValidationError:  # pragma: no cover - every value above is pre-validated
        log.warning("recipe fields did not validate; reading %r by title only", title)
        return RecipeIn(title=title)


def recipe_from_content(path: str, content: str) -> Recipe:
    """The recipe a note holds. Raises ValueError when the note is not one
    (`is_recipe_note`), including a note whose frontmatter does not load."""
    meta, body = fm.parse(content)
    if not is_recipe_note(path, meta):
        raise ValueError(f"Not a recipe: {path}")
    recipe = _recipe_in(meta, body, Path(path).stem)
    return Recipe(
        **recipe.model_dump(),
        path=path,
        revision=_revision(content),
        created=_stamp(meta.get("created")),
        modified=_stamp(meta.get("modified")),
    )


def _fold(text: str) -> str:
    # index.db.fold: what case-insensitive matching compares.
    return unicodedata.normalize("NFC", text).casefold()


def card_fields(meta: dict, body: str, fallback_title: str) -> dict:
    """Frontmatter projected to `recipes` index-row values: `tags` and `art`
    are JSON text, `search` the folded words `q` matches. Never raises."""
    recipe = _recipe_in(meta, body, fallback_title)
    total = None
    if recipe.prep_minutes is not None or recipe.cook_minutes is not None:
        total = (recipe.prep_minutes or 0) + (recipe.cook_minutes or 0)
    words = [recipe.title, *(i.item for i in recipe.ingredients), *recipe.tags, recipe.notes]
    return {
        "title": recipe.title,
        "tags": json.dumps(recipe.tags, ensure_ascii=False),
        "servings": recipe.base_servings,
        "total_minutes": total,
        "thumbnail": recipe.source.thumbnail if recipe.source else None,
        "search": _fold(" ".join(words)),
        "art": json.dumps(
            {"art": recipe.art, "art_colors": recipe.art_colors, "art_palette": recipe.art_palette},
            ensure_ascii=False,
        ),
        "created": _stamp(meta.get("created")),
        "modified": _stamp(meta.get("modified")),
    }


# -- the service layer: API routes and OMNIA tools both call these ----------------


def _config_path(state) -> Path:
    return state.settings.vault_dir / NoteStore.INTERNAL_DIR / "config.json"


def _saved_order(state) -> list[str]:
    try:
        config = read_json(_config_path(state), {}) or {}
    except NoteError:
        # read_json has set the unreadable file aside; the order is a
        # preference and the menu still answers without it.
        return []
    value = config.get(ORDER_KEY) if isinstance(config, dict) else None
    return [p for p in value if isinstance(p, str)] if isinstance(value, list) else []


def _display_order(rows: list, saved: list[str]) -> list:
    """Recipes the saved order does not name first, newest `created` first,
    then the named ones in saved order. Stale paths are ignored."""
    by_path = {row["path"]: row for row in rows}
    listed = [p for p in dict.fromkeys(saved) if p in by_path]
    named = set(listed)
    rest = sorted((row for row in rows if row["path"] not in named), key=lambda r: r["path"])
    rest.sort(key=lambda r: r["created"] or "", reverse=True)
    return rest + [by_path[p] for p in listed]


def _json(text: str | None, default):
    try:
        value = json.loads(text) if text else default
    except ValueError:
        return default
    return value if isinstance(value, type(default)) else default


def _card(row, tags: list[str]) -> RecipeCard:
    art = _json(row["art"], {})
    return RecipeCard(
        path=row["path"],
        title=row["title"],
        total_minutes=row["total_minutes"],
        thumbnail=row["thumbnail"],
        tags=tags,
        modified=row["modified"] or "",
        art=art.get("art") if isinstance(art.get("art"), str) else "",
        art_colors=art.get("art_colors") if isinstance(art.get("art_colors"), str) else "",
        art_palette=_palette(art.get("art_palette")),
    )


async def list_cards(state, q: str = "", tag: str = "") -> list[RecipeCard]:
    """Menu cards in display order. Every word of `q` must appear in the title,
    an ingredient, a tag or the notes; `tag` must be one of the recipe's."""
    rows = await state.db.execute_fetchall("SELECT * FROM recipes")
    terms = _fold(q).split()
    wanted = tag.strip().lower()
    cards = []
    for row in _display_order(rows, _saved_order(state)):
        search = row["search"] or ""
        if any(term not in search for term in terms):
            continue
        tags = [t for t in _json(row["tags"], []) if isinstance(t, str)]
        if wanted and wanted not in tags:
            continue
        cards.append(_card(row, tags))
    return cards


async def tag_counts(state) -> list[dict]:
    """Every tag in the library with its recipe count, most used first."""
    counts: dict[str, int] = {}
    for row in await state.db.execute_fetchall("SELECT tags FROM recipes"):
        for t in _json(row["tags"], []):
            if isinstance(t, str):
                counts[t] = counts.get(t, 0) + 1
    return [
        {"tag": t, "count": n} for t, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


def _read_recipe(state, path: str) -> tuple[str, Recipe]:
    # One spelling per recipe. The store resolves "recipes/./x.md" and
    # "recipes//x.md" to the same file, and a write through either indexed a
    # second row: a menu entry that outlived the note when it was deleted.
    if str(PurePosixPath(path)) != path:
        raise NoteError(404, f"Not a recipe: {path}")
    content = state.store.read(path)
    try:
        return content, recipe_from_content(path, content)
    except ValueError:
        raise NoteError(404, f"Not a recipe: {path}") from None


async def get_recipe(state, path: str) -> Recipe:
    return _read_recipe(state, path)[1]


async def _announce(state, path: str, written: str, origin: str | None) -> None:
    await state.indexer.index_note(path, written)
    state.autocommit.touch()
    state.live.note_changed(path, _revision(written), origin=origin)


ATTACHMENT_PREFIX = "/api/attachments/"


async def _stored_thumbnail(state, recipe: RecipeIn) -> RecipeIn:
    """The recipe with a thumbnail the vault holds. A remote https image (an
    imported page's photo) is downloaded into attachments, as media covers
    are; a download that fails leaves no thumbnail rather than failing the
    save. Anything that is neither is dropped."""
    source = recipe.source
    if source is None or not source.thumbnail:
        return recipe
    thumbnail = source.thumbnail.strip()
    stored = None
    if thumbnail.startswith(ATTACHMENT_PREFIX):
        stored = thumbnail
    elif thumbnail.lower().startswith("https://"):
        try:
            ingested = await media_lookup.ingest_cover(state, thumbnail, recipe_slug(recipe.title))
            stored = ingested["url"]
        except Exception as exc:
            log.warning("recipe thumbnail %s not stored: %s", thumbnail[:200], exc)
    if stored == source.thumbnail:
        return recipe
    return recipe.model_copy(update={"source": source.model_copy(update={"thumbnail": stored})})


async def create_recipe(state, recipe: RecipeIn, *, origin: str | None = None) -> Recipe:
    try:
        build_recipe_content(recipe)  # refuse a bad recipe before any download
    except ValueError as exc:
        raise NoteError(400, str(exc)) from exc
    recipe = await _stored_thumbnail(state, recipe)
    content = build_recipe_content(recipe)
    taken = {note.path for note in state.store.list_notes()}
    path = next_recipe_path(_normalize(recipe).title, taken)
    # recipe_slug has made the name safe to store; recipes open from the
    # menu, not by wikilink, so '#' and '[' in a title are no reason to refuse.
    written = state.store.create(path, content, check_name=False)
    await _announce(state, path, written, origin)
    created = recipe_from_content(path, written)
    if not created.art:
        # Imported here: recipe_art imports this module.
        from . import recipe_art

        recipe_art.draw_soon(state, path)
    return created


async def update_recipe(
    state,
    path: str,
    recipe: RecipeIn,
    *,
    base_revision: str | None,
    origin: str | None = None,
) -> Recipe:
    def current() -> str:
        content, _recipe = _read_recipe(state, path)
        if base_revision is not None and base_revision != _revision(content):
            raise NoteError(409, "Recipe changed elsewhere — reload and retry")
        return content

    if recipe.source and recipe.source.thumbnail:
        current()  # a 404 or 409 before any download
        recipe = await _stored_thumbnail(state, recipe)
    # Read, check and write with no await between them: the note cannot
    # change under the revision check.
    content = current()
    try:
        updated = apply_recipe(content, recipe)
    except ValueError as exc:
        raise NoteError(400, str(exc)) from exc
    written = state.store.write(path, updated)
    await _announce(state, path, written, origin)
    return recipe_from_content(path, written)


async def delete_recipe(state, path: str, *, origin: str | None = None) -> None:
    """To Trash, as the notes API deletes a note, so it can be restored."""
    _read_recipe(state, path)
    state.trash.trash_note(path)
    await state.indexer.remove_note(path)
    proofread = getattr(state, "proofread", None)
    if proofread is not None:
        proofread.clear(path)
    state.autocommit.touch()
    state.live.note_removed(path, origin=origin)


async def set_order(state, order: list[str]) -> list[str]:
    """Save the menu order and answer the full display order after it. Paths
    that are not recipes are dropped; an empty list clears the order."""
    rows = await state.db.execute_fetchall("SELECT path, created FROM recipes")
    known = {row["path"] for row in rows}
    cleaned = [p for p in dict.fromkeys(str(p) for p in order) if p in known]
    # From here to the write nothing awaits, so a profile or appearance save
    # landing mid-request is never overwritten by a stale copy of the file.
    config_path = _config_path(state)
    config = read_json(config_path, {}) or {}
    if not isinstance(config, dict):
        raise NoteError(500, "config.json does not hold an object")
    if cleaned:
        config[ORDER_KEY] = cleaned
    else:
        config.pop(ORDER_KEY, None)
    atomic_json(config_path, config)
    state.autocommit.touch()
    return [row["path"] for row in _display_order(rows, cleaned)]


async def store_art(
    state, path: str, drawing: Drawing, *, only_if_empty: bool = False
) -> Recipe | None:
    """Store a finished drawing. None when the recipe is gone (deleted while
    the drawing ran). `only_if_empty` leaves a drawing already there alone.
    `modified` stays as it was: a drawing is not an edit."""
    try:
        content, current = _read_recipe(state, path)
    except NoteError as exc:
        if exc.status == 404:
            return None
        raise
    if only_if_empty and current.art:
        return current
    try:
        updated = apply_art(content, drawing)
    except ValueError as exc:
        raise NoteError(400, str(exc)) from exc
    written = state.store.write(path, updated, preserve_modified=True)
    # No origin: an editor open on the note merges the drawing in.
    await _announce(state, path, written, None)
    return recipe_from_content(path, written)
