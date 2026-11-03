"""Recipes API: the menu, its order, the recipe CRUD routes, imports and
drawings.

Thin by design: every route calls the service layer in ``app/recipes.py``,
which the OMNIA tools share, so a recipe written from chat and one written
from the form go through one path to the note, the index and the LiveBus.
Imports only read: they answer a Draft for the review screen, and nothing is
stored until the reviewed recipe is created.
"""
import asyncio
import re

from fastapi import APIRouter, File, Form, Request, Response, UploadFile
from pydantic import BaseModel

from .. import recipe_ai, recipe_art, recipe_import, recipe_parse, recipes
from ..recipe_ai import RecipeAIError
from ..recipe_import import RecipeImportError
from ..recipe_models import Draft, Recipe, RecipeIn
from ..vault.notes import NoteError

router = APIRouter(prefix="/api/recipes", tags=["recipes"])

# Text that is nothing but a link is imported as one, as the recipe app did.
LINK_RE = re.compile(r"https?://\S+", re.I)
VIDEO_LABELS = {"youtube": "YouTube", "reel": "Instagram", "tiktok": "TikTok"}
# The local parser matches every ingredient against every step, so a huge
# paste would hold a worker for minutes; a typed recipe is a few thousand
# characters.
MAX_LOCAL_TEXT = recipe_parse.MAX_TEXT


def _origin(request: Request, client_id: str | None = None) -> str | None:
    """The client behind a change, so it can skip its own live echo: the
    body's client_id, else the X-Client-Id header the app sends on every
    request (all a DELETE carries)."""
    return client_id or request.headers.get("x-client-id")


# NOTE: literal routes (order, import, status) must stay above the
# /item/{path:path} and /art/{path:path} routes.


@router.get("")
async def list_recipes(request: Request, q: str = "", tag: str = ""):
    state = request.app.state
    cards = await recipes.list_cards(state, q, tag)
    return {
        "recipes": [card.model_dump() for card in cards],
        "tags": await recipes.tag_counts(state),
    }


class CreateRecipe(RecipeIn):
    client_id: str | None = None


@router.post("", status_code=201)
async def create_recipe(request: Request, body: CreateRecipe) -> Recipe:
    recipe = RecipeIn.model_validate(body.model_dump(exclude={"client_id"}))
    return await recipes.create_recipe(
        request.app.state, recipe, origin=_origin(request, body.client_id)
    )


class OrderBody(BaseModel):
    order: list[str]


@router.put("/order")
async def set_order(request: Request, body: OrderBody):
    return {"order": await recipes.set_order(request.app.state, body.order)}


@router.get("/status")
async def status(request: Request):
    return {"ai": recipe_ai.available(request.app.state)}


# -- imports ----------------------------------------------------------------------


class ImportBody(BaseModel):
    text: str | None = None
    url: str | None = None


def _https(link: str) -> str:
    """A pasted link as the fetch takes it: https, with the scheme added to
    a bare link and http upgraded."""
    if link[:7].lower() == "http://":
        return "https://" + link[7:]
    return link if "://" in link else "https://" + link


async def _parse_locally(text: str) -> Draft:
    cut = len(text) > MAX_LOCAL_TEXT
    draft = await asyncio.to_thread(recipe_parse.parse_text, text[:MAX_LOCAL_TEXT])
    if cut:
        draft.missing.append(
            f"The text was longer than {MAX_LOCAL_TEXT:,} characters and was cut."
        )
    return draft


async def _import_url(state, link: str) -> Draft:
    if any(ch.isspace() for ch in link):
        raise NoteError(400, "Not a valid link")
    url = _https(link)
    kind = recipe_import.classify_url(url)
    if kind in VIDEO_LABELS:
        raise NoteError(400, f"{VIDEO_LABELS[kind]} links are not supported yet")
    try:
        page = await recipe_import.fetch(url)
    except RecipeImportError as exc:
        raise NoteError(exc.status, exc.detail) from None
    # Both in a thread: a 5 MB page can hold a million JSON-LD objects to walk
    # and thousands of ingredients and steps.
    data = await asyncio.to_thread(recipe_import.find_recipe, page.blocks)
    if data is not None:
        return await asyncio.to_thread(recipe_import.draft_from_schema, data, page.url)
    if not recipe_ai.available(state):
        raise NoteError(400, "This page has no recipe data")
    try:
        return await recipe_ai.extract(state, text=page.text, url=page.url)
    except RecipeAIError as exc:
        raise NoteError(exc.status, str(exc)) from None


@router.post("/import")
async def import_recipe(request: Request, body: ImportBody) -> Draft:
    """A draft from a link or from typed text. A recipe website is read from
    its schema.org data, else by the AI from the page text; typed text by the
    AI, else (no provider, or the AI failed) by the local parser."""
    state = request.app.state
    text = (body.text or "").strip()
    url = (body.url or "").strip()
    if not url and LINK_RE.fullmatch(text):
        url, text = text, ""
    if url:
        return await _import_url(state, url)
    if not text:
        raise NoteError(400, "Paste a link or a recipe")
    if not recipe_ai.available(state):
        return await _parse_locally(text)
    try:
        return await recipe_ai.extract(state, text=text)
    except RecipeAIError as exc:
        draft = await _parse_locally(text)
        draft.missing.insert(0, f"{exc} The text was read without AI.")
        return draft


@router.post("/import/files")
async def import_files(
    request: Request,
    files: list[UploadFile] | None = File(default=None),
    text: str | None = Form(default=None),
) -> Draft:
    """A draft from 1 to MAX_FILES photos or PDFs of one recipe, plus
    optional text. Needs the AI."""
    state = request.app.state
    if not recipe_ai.available(state):
        raise NoteError(503, recipe_ai.NO_PROVIDER)
    uploads = files or []
    if not uploads:
        raise NoteError(400, "Add a photo or a PDF.")
    if len(uploads) > recipe_ai.MAX_FILES:
        raise NoteError(400, f"Send at most {recipe_ai.MAX_FILES} files at once.")
    sources = []
    for upload in uploads:
        # A Blob appended without a name arrives as "blob".
        name = upload.filename if upload.filename not in (None, "blob") else ""
        sources.append(recipe_ai.SourceFile(await upload.read(), upload.content_type or "", name))
    try:
        return await recipe_ai.extract(state, text=(text or "").strip() or None, files=sources)
    except RecipeAIError as exc:
        raise NoteError(exc.status, str(exc)) from None


@router.get("/item/{path:path}")
async def get_recipe(request: Request, path: str) -> Recipe:
    return await recipes.get_recipe(request.app.state, path)


class UpdateRecipe(BaseModel):
    recipe: RecipeIn
    base_revision: str | None = None
    client_id: str | None = None


@router.put("/item/{path:path}")
async def update_recipe(request: Request, path: str, body: UpdateRecipe) -> Recipe:
    return await recipes.update_recipe(
        request.app.state,
        path,
        body.recipe,
        base_revision=body.base_revision,
        origin=_origin(request, body.client_id),
    )


@router.delete("/item/{path:path}", status_code=204)
async def delete_recipe(request: Request, path: str) -> Response:
    await recipes.delete_recipe(request.app.state, path, origin=_origin(request))
    return Response(status_code=204)


# -- drawings ---------------------------------------------------------------------


@router.post("/art/{path:path}", status_code=202)
async def redraw_art(request: Request, path: str):
    """Start a new drawing, replacing the stored one when it lands; the page
    polls the GET route."""
    state = request.app.state
    await recipes.get_recipe(state, path)  # 404 for anything that is no recipe
    if not recipe_ai.available(state):
        raise NoteError(503, recipe_ai.NO_PROVIDER)
    return {"drawing": recipe_art.draw_soon(state, path, force=True)}


@router.get("/art/{path:path}")
async def art_status(request: Request, path: str):
    state = request.app.state
    await recipes.get_recipe(state, path)
    return recipe_art.status(state, path)
