"""The one recipe format, as it travels over the API.

Every import route ends in a RecipeIn; the store turns a RecipeIn into a note
in the vault and a note back into a Recipe. ``frontend/src/lib/recipes/types.ts``
mirrors this file. The on-disk shape (frontmatter keys) lives in ``recipes.py``.

Pure models: no imports from the rest of the app, so the indexer, the API, the
import readers and the AI step can all depend on it.
"""
from typing import Literal

from pydantic import BaseModel, Field

Unit = Literal[
    "",  # counted items: eggs, onions
    "tsp", "tbsp", "cup",
    "ml", "l",
    "g", "kg",
    "oz", "lb",
    "pinch", "clove", "slice", "can", "bunch", "stick", "sprig",
]
UNITS: tuple[str, ...] = Unit.__args__  # type: ignore[attr-defined]

Scaling = Literal["linear", "whole", "to_taste"]
SourceType = Literal[
    "typed", "text", "website", "photo", "dish_photo", "youtube", "reel", "tiktok", "video"
]


class Ingredient(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    amount: float | None = None
    unit: Unit = ""
    item: str = Field(min_length=1)
    prep: str = ""
    scaling: Scaling = "linear"
    estimated: bool = False
    group: str | None = None


class Step(BaseModel):
    # Steps name ingredients as {id}; the recipe page shows the scaled amount.
    text: str = Field(min_length=1)
    timer_seconds: int | None = Field(default=None, ge=1)


class Source(BaseModel):
    type: SourceType = "typed"
    url: str | None = None
    # An /api/attachments/... URL once stored. Create and update also accept a
    # remote https URL, which the server ingests into attachments.
    thumbnail: str | None = None


class RecipeIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    base_servings: int = Field(default=2, ge=1, le=100)
    prep_minutes: int | None = Field(default=None, ge=0)
    cook_minutes: int | None = Field(default=None, ge=0)
    ingredients: list[Ingredient] = []
    steps: list[Step] = []
    notes: str = ""  # the note body: free markdown
    tags: list[str] = []
    source: Source | None = None
    # ASCII art of the dish, drawn after the recipe is made: the characters, a
    # same-shaped grid of colour keys ("." = default ink), and the palette
    # mapping keys to hex colours. Rows are joined with "\n".
    art: str = Field(default="", max_length=4000)
    art_colors: str = Field(default="", max_length=4000)
    art_palette: dict[str, str] = {}


class Recipe(RecipeIn):
    path: str  # the note's vault path; the recipe's identity
    revision: str  # content hash of the note, sent back as base_revision
    created: str
    modified: str


class RecipeCard(BaseModel):
    path: str
    title: str
    total_minutes: int | None
    thumbnail: str | None
    tags: list[str]
    modified: str
    art: str = ""
    art_colors: str = ""
    art_palette: dict[str, str] = {}


class Draft(BaseModel):
    """What every import route returns: a recipe to review, plus what is missing."""

    recipe: RecipeIn
    missing: list[str] = []
    method: Literal["ai", "local", "schema_org"]


class Drawing(BaseModel):
    """One finished ASCII drawing, ready to store on a recipe."""

    art: str
    art_colors: str = ""
    art_palette: dict[str, str] = {}
