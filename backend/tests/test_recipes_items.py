"""The recipe note format: building, reading forgivingly, and rewriting a note
while keeping everything the user wrote."""
import json

import pytest

from app import recipes
from app.index.db import content_hash
from app.recipe_models import Drawing, Ingredient, RecipeIn, Source, Step
from app.vault import fm

ART = "   .-~~-.   \n  (      )  \n   `-..-'   "
COLORS = "   aaaaaa   \n  a......a  \n   aaaaaa   "


def full_recipe(**overrides) -> RecipeIn:
    values = dict(
        title="Spaghetti Carbonara",
        base_servings=2,
        prep_minutes=10,
        cook_minutes=15,
        ingredients=[
            Ingredient(id="butter", amount=2.0, unit="tbsp", item="butter", prep="softened",
                       scaling="whole", estimated=True, group="Sauce"),
            Ingredient(id="eggs", amount=3, unit="", item="eggs"),
            Ingredient(id="salt", unit="pinch", item="salt", scaling="to_taste"),
        ],
        steps=[Step(text="Melt {butter} in a pan.", timer_seconds=300), Step(text="Whisk {eggs}.")],
        notes="Free notes in *markdown*.",
        tags=["pasta", "quick"],
        source=Source(type="website", url="https://example.com/carbonara", thumbnail="/api/attachments/abc.jpg"),
        art=ART,
        art_colors=COLORS,
        art_palette={"a": "#e8c070"},
    )
    values.update(overrides)
    return RecipeIn(**values)


def as_in(recipe) -> dict:
    """The RecipeIn part of a Recipe, for comparing with what was written."""
    return RecipeIn.model_validate(recipe.model_dump()).model_dump()


# -- identity ------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [(1, True), (True, False), (1.0, False), ("1", False), ("yes", False), (2, False), (None, False)],
)
def test_only_the_integer_one_marks_a_recipe(value, expected):
    assert recipes.is_recipe({"recipe": value}) is expected
    assert recipes.is_recipe({}) is False


def test_recipe_true_in_a_note_is_an_ordinary_note():
    content = "---\ntitle: Pie\nrecipe: true\n---\n\nbody\n"
    with pytest.raises(ValueError):
        recipes.recipe_from_content("recipes/Pie.md", content)


@pytest.mark.parametrize(
    "path, inside",
    [
        ("recipes/Pie.md", True),
        ("recipes/Desserts/Pie.md", True),
        ("Recipes/Pie.md", False),
        ("RECIPES/a/b/Pie.md", False),
        ("Pie.md", False),
        ("Notes/Pie.md", False),
        ("Notes/recipes/Pie.md", False),
        ("recipes.md", False),
        ("recipesx/Pie.md", False),
    ],
)
def test_a_recipe_lives_under_the_recipes_folder(path, inside):
    assert recipes.in_recipes_folder(path) is inside
    assert recipes.is_recipe_note(path, {"recipe": 1}) is inside
    assert recipes.is_recipe_note(path, {"recipe": True}) is False


def test_the_marker_outside_the_recipes_folder_is_an_ordinary_note():
    content = recipes.build_recipe_content(RecipeIn(title="Pie"))
    assert recipes.recipe_from_content("recipes/Pie.md", content).title == "Pie"
    with pytest.raises(ValueError):
        recipes.recipe_from_content("Notes/Pie.md", content)


def test_new_paths_slug_like_media_and_step_past_taken_names():
    assert recipes.RECIPES_DIR == "recipes" and recipes.RECIPE_KEY == "recipe"
    assert recipes.recipe_slug("Mac: and Cheese?") == "Mac and Cheese"
    assert recipes.next_recipe_path("Mac: and Cheese?", set()) == "recipes/Mac and Cheese.md"
    taken = {"recipes/mac and cheese.md", "recipes/Mac and Cheese 2.md"}
    assert recipes.next_recipe_path("Mac and Cheese", taken) == "recipes/Mac and Cheese 3.md"


# -- the format ----------------------------------------------------------------


def test_build_then_read_round_trips_every_field():
    recipe = full_recipe()
    content = recipes.build_recipe_content(recipe, created="2026-10-04T12:00:00Z")
    read = recipes.recipe_from_content("recipes/Spaghetti Carbonara.md", content)
    assert as_in(read) == recipe.model_dump()
    assert read.path == "recipes/Spaghetti Carbonara.md"
    assert read.created == "2026-10-04T12:00:00Z"
    assert read.modified
    assert read.revision == content_hash(content)


def test_frontmatter_follows_the_contract():
    content = recipes.build_recipe_content(full_recipe(), created="2026-10-04T12:00:00Z")
    meta, body = fm.parse(content)
    assert list(meta) == [
        "title", "created", "modified", "recipe", "servings", "prep_minutes", "cook_minutes",
        "recipe_tags", "source_type", "source_url", "thumbnail", "ingredients", "steps",
        "art", "art_colors", "art_palette",
    ]
    assert meta["recipe"] == 1 and type(meta["recipe"]) is int
    assert meta["created"] == "2026-10-04T12:00:00Z"
    assert meta["servings"] == 2
    assert meta["recipe_tags"] == ["pasta", "quick"]
    assert meta["source_type"] == "website"
    assert meta["thumbnail"] == "/api/attachments/abc.jpg"
    assert meta["ingredients"][0] == {
        "id": "butter", "amount": 2.0, "unit": "tbsp", "item": "butter", "prep": "softened",
        "scaling": "whole", "estimated": True, "group": "Sauce",
    }
    # Defaults are left out; unit stays, '' for counted items.
    assert meta["ingredients"][1] == {"id": "eggs", "amount": 3.0, "unit": "", "item": "eggs"}
    assert meta["ingredients"][2] == {"id": "salt", "unit": "pinch", "item": "salt", "scaling": "to_taste"}
    assert meta["steps"] == [
        {"text": "Melt {butter} in a pan.", "timer_seconds": 300},
        {"text": "Whisk {eggs}."},
    ]
    assert meta["art"] == ART.split("\n")
    assert meta["art_colors"] == COLORS.split("\n")
    assert meta["art_palette"] == {"a": "#e8c070"}
    assert body == "Free notes in *markdown*.\n"


def test_empty_optional_fields_are_omitted():
    content = recipes.build_recipe_content(RecipeIn(title="Toast"))
    meta, body = fm.parse(content)
    for key in ("prep_minutes", "cook_minutes", "recipe_tags", "source_type", "source_url",
                "thumbnail", "art", "art_colors", "art_palette"):
        assert key not in meta
    assert meta["ingredients"] == [] and meta["steps"] == []
    assert meta["servings"] == 2
    assert body == ""
    assert recipes.recipe_from_content("recipes/Toast.md", content).source is None


def test_art_rows_keep_their_trailing_spaces():
    art = "  /\\  \n /  \\ \n/____\\\n      "
    content = recipes.build_recipe_content(RecipeIn(title="Tent", art=art, art_colors=art))
    assert "- '  /\\  '" in content  # quoted, so the spaces are part of the row
    read = recipes.recipe_from_content("recipes/Tent.md", content)
    assert read.art == art and read.art_colors == art


def test_tags_are_lowercased_stripped_deduped_and_sorted():
    recipe = RecipeIn(title="Soup", tags=[" Winter", "soup", "SOUP ", "", "  ", "easy"])
    content = recipes.build_recipe_content(recipe)
    assert fm.parse(content)[0]["recipe_tags"] == ["easy", "soup", "winter"]
    assert recipes.recipe_from_content("recipes/Soup.md", content).tags == ["easy", "soup", "winter"]


def test_a_blank_title_is_refused():
    with pytest.raises(ValueError):
        recipes.build_recipe_content(RecipeIn(title="   "))


def test_revision_is_the_note_content_hash():
    content = recipes.build_recipe_content(full_recipe())
    assert recipes.recipe_from_content("recipes/x.md", content).revision == content_hash(content)


# -- forgiving reads -------------------------------------------------------------


BROKEN = """---
title: Hand Written
created: 2026-10-01T08:00:00Z
recipe: 1
servings: lots
prep_minutes: -5
cook_minutes: '20'
recipe_tags: Pasta, quick, pasta
source_type: carrier pigeon
source_url: https://example.com/x
ingredients:
- just a string
- 5
- id: flour
  amount: '1.5'
  unit: Cups
  item: flour
- id: sugar
  amount: a lot
  unit: tablespoons
  item: sugar
  scaling: sometimes
  estimated: yes please
- id: nothing
  unit: g
- amount: 2
  unit: ''
  item: Large Eggs
- id: [nested]
  item: salt
steps:
- Preheat the oven.
- text: Mix.
  timer_seconds: 0
- timer_seconds: 60
- [not, a, step]
art:
- ok row
- {a: dict}
art_colors: 7
art_palette: [a, b]
---

Notes survive.
"""


def test_a_malformed_recipe_reads_with_defaults_and_drops_bad_entries():
    read = recipes.recipe_from_content("recipes/Hand Written.md", BROKEN)
    assert read.title == "Hand Written"
    assert read.base_servings == 2
    assert read.prep_minutes is None
    assert read.cook_minutes == 20
    assert read.tags == ["pasta", "quick"]
    assert read.source == Source(type="typed", url="https://example.com/x")
    items = [(i.id, i.amount, i.unit, i.item, i.scaling, i.estimated) for i in read.ingredients]
    assert items == [
        ("flour", 1.5, "cup", "flour", "linear", False),
        ("sugar", None, "", "sugar", "linear", False),
        ("large-eggs", 2.0, "", "Large Eggs", "linear", False),
        ("salt", None, "", "salt", "linear", False),
    ]
    assert [(s.text, s.timer_seconds) for s in read.steps] == [
        ("Preheat the oven.", None),
        ("Mix.", None),
    ]
    assert read.art == "" and read.art_colors == "" and read.art_palette == {}
    assert read.notes == "Notes survive."
    # An unquoted timestamp loads as a datetime; it reads back as the app's form.
    assert read.created == "2026-10-01T08:00:00Z"


def test_garbage_of_every_type_never_raises():
    for value in (None, 0, "x", [], {}, [None], [[1]], {"a": 1}, 3.5, True):
        meta = {
            "recipe": 1, "title": value, "servings": value, "prep_minutes": value,
            "cook_minutes": value, "recipe_tags": value, "source_type": value,
            "source_url": value, "thumbnail": value, "ingredients": value, "steps": value,
            "art": value, "art_colors": value, "art_palette": value, "created": value,
            "modified": value,
        }
        content = fm.serialize(meta, "")
        read = recipes.recipe_from_content("recipes/Garbage.md", content)
        assert read.title
        fields = recipes.card_fields(meta, "", "Garbage")
        assert fields["title"]


def test_unloadable_frontmatter_is_not_a_recipe():
    content = "---\nrecipe: 1\ntitle: a: b\n---\n\nbody\n"
    with pytest.raises(ValueError):
        recipes.recipe_from_content("recipes/x.md", content)


def test_overlong_art_degrades_to_none():
    rows = ["x" * 100] * 50
    content = fm.serialize({"recipe": 1, "title": "Big", "art": rows}, "")
    assert recipes.recipe_from_content("recipes/Big.md", content).art == ""


# -- rewriting an existing note ------------------------------------------------


USER_NOTE = """---
title: Carbonara
# my own comment
created: '2026-01-02T03:04:05Z'
modified: '2026-01-02T03:04:05Z'
zip: 01234
answer: yes
recipe: 1
servings: 2
mood:
  - cosy
  - quick   # inline comment
ingredients:
- id: eggs
  amount: 3.0
  unit: ''
  item: eggs
steps: []
art:
- '  ^^  '
- ' (  ) '
art_colors:
- '  aa  '
- ' a..a '
art_palette:
  a: '#ffcc00'
---

Old notes.
"""


def test_apply_keeps_user_keys_byte_for_byte_and_created():
    updated = recipes.apply_recipe(
        USER_NOTE,
        RecipeIn(title="Better Carbonara", base_servings=4, tags=["Rome"], notes="New notes."),
    )
    for line in (
        "# my own comment\n",
        "created: '2026-01-02T03:04:05Z'\n",
        "zip: 01234\n",
        "answer: yes\n",
        "mood:\n  - cosy\n  - quick   # inline comment\n",
    ):
        assert line in updated
    meta, body = fm.parse(updated)
    assert meta["title"] == "Better Carbonara"
    assert meta["servings"] == 4
    assert meta["recipe_tags"] == ["rome"]
    assert meta["ingredients"] == [] and meta["steps"] == []
    assert body == "New notes.\n"
    # The user's keys keep their place, ahead of the recipe keys added after them.
    assert updated.index("zip: 01234") < updated.index("recipe: 1")


def test_apply_keeps_the_drawing_when_the_update_carries_none():
    updated = recipes.apply_recipe(USER_NOTE, RecipeIn(title="Carbonara"))
    block = USER_NOTE[USER_NOTE.index("art:\n"):USER_NOTE.index("---\n\nOld")]
    assert block in updated
    read = recipes.recipe_from_content("recipes/Carbonara.md", updated)
    assert read.art == "  ^^  \n (  ) "
    assert read.art_palette == {"a": "#ffcc00"}


def test_apply_replaces_the_drawing_when_the_update_carries_one():
    updated = recipes.apply_recipe(
        USER_NOTE, RecipeIn(title="Carbonara", art="new\nart ", art_palette={})
    )
    meta, _ = fm.parse(updated)
    assert meta["art"] == ["new", "art "]
    assert "art_colors" not in meta and "art_palette" not in meta


def test_apply_is_a_no_op_for_unchanged_recipe_keys():
    recipe = recipes.recipe_from_content("recipes/Carbonara.md", USER_NOTE)
    updated = recipes.apply_recipe(USER_NOTE, RecipeIn.model_validate(as_in(recipe)))
    assert updated == USER_NOTE


def test_apply_refuses_unloadable_frontmatter():
    with pytest.raises(ValueError):
        recipes.apply_recipe("---\ntitle: a: b\n---\n\n", RecipeIn(title="x"))


def test_apply_art_sets_and_clears_the_drawing_only():
    drawn = recipes.apply_art(USER_NOTE, Drawing(art="ab \ncd ", art_colors="a. \n.a ", art_palette={"a": "#000000"}))
    meta, body = fm.parse(drawn)
    assert meta["art"] == ["ab ", "cd "]
    assert meta["art_colors"] == ["a. ", ".a "]
    assert meta["art_palette"] == {"a": "#000000"}
    assert body == "Old notes.\n"
    assert "zip: 01234\n" in drawn and "mood:\n  - cosy\n" in drawn

    cleared = recipes.apply_art(drawn, Drawing(art=""))
    meta, _ = fm.parse(cleared)
    assert not {"art", "art_colors", "art_palette"} & set(meta)

    with pytest.raises(ValueError):
        recipes.apply_art(USER_NOTE, Drawing(art="x" * 4001))


# -- index rows ------------------------------------------------------------------


def test_card_fields_are_ready_index_values():
    content = recipes.build_recipe_content(full_recipe(notes="Use Pecorino Romano."), created="2026-10-04T12:00:00Z")
    meta, body = fm.parse(content)
    fields = recipes.card_fields(meta, body, "fallback")
    assert fields["title"] == "Spaghetti Carbonara"
    assert json.loads(fields["tags"]) == ["pasta", "quick"]
    assert fields["servings"] == 2
    assert fields["total_minutes"] == 25
    assert fields["thumbnail"] == "/api/attachments/abc.jpg"
    for word in ("spaghetti", "butter", "eggs", "pasta", "pecorino"):
        assert word in fields["search"]
    assert json.loads(fields["art"]) == {"art": ART, "art_colors": COLORS, "art_palette": {"a": "#e8c070"}}
    assert fields["created"] == "2026-10-04T12:00:00Z"
    assert fields["modified"] == meta["modified"]


def test_card_total_minutes_counts_whichever_times_exist():
    def total(**kw):
        meta = {"recipe": 1, **kw}
        return recipes.card_fields(meta, "", "x")["total_minutes"]

    assert total() is None
    assert total(prep_minutes=5) == 5
    assert total(cook_minutes=0) == 0
    assert total(prep_minutes=5, cook_minutes=7) == 12
