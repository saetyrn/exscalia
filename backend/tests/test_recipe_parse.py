"""The local recipe text parser, ported with the recipe app's own cases."""
import time

import pytest

from app.recipe_parse import parse_ingredient_line, parse_number, parse_text
from app.recipe_units import default_scaling, unit_from_word


def test_numbers():
    assert parse_number("2") == 2
    assert parse_number("1/2") == 0.5
    assert parse_number("1 1/2") == 1.5
    assert parse_number("½") == 0.5
    assert parse_number("1½") == 1.5
    assert parse_number("0.75") == 0.75
    assert parse_number("0,75") == 0.75


def test_ingredient_lines():
    i = parse_ingredient_line("2 tbsp butter, melted")
    assert (i.amount, i.unit, i.item, i.prep, i.scaling) == (2, "tbsp", "butter", "melted", "linear")

    i = parse_ingredient_line("200g noodles")
    assert (i.amount, i.unit, i.item) == (200, "g", "noodles")

    i = parse_ingredient_line("⅔ cup chicken stock")
    assert i.unit == "cup" and abs(i.amount - 2 / 3) < 1e-9

    i = parse_ingredient_line("1 egg")
    assert (i.amount, i.unit, i.item, i.scaling) == (1, "", "egg", "whole")

    i = parse_ingredient_line("1/2 tsp salt")
    assert (i.amount, i.unit, i.scaling) == (0.5, "tsp", "to_taste")

    i = parse_ingredient_line("Salt and pepper, to taste")
    assert i.amount is None and i.scaling == "to_taste" and i.item == "Salt and pepper"

    i = parse_ingredient_line("2 cloves garlic (minced)")
    assert (i.amount, i.unit, i.item, i.prep) == (2, "clove", "garlic", "minced")

    i = parse_ingredient_line("1-2 tbsp olive oil")
    assert i.amount == 1 and i.estimated is True

    i = parse_ingredient_line("a splash of soy sauce")
    assert i.amount is None and i.estimated is True and i.item == "soy sauce"

    i = parse_ingredient_line("Butter - 2 tbsp")
    assert (i.amount, i.unit, i.item) == (2, "tbsp", "Butter")

    i = parse_ingredient_line("2 T sugar")
    assert i.unit == "tbsp"
    i = parse_ingredient_line("2 t vanilla")
    assert i.unit == "tsp"

    i = parse_ingredient_line("4 chicken thighs")
    assert i.scaling == "whole"

    i = parse_ingredient_line("1 cup salted butter")
    assert i.scaling == "linear"  # "salted" is not "salt"


TYPED = """Garlic butter noodles
Serves 2 · Prep 5 min · Cook 10 min

Ingredients
200 g noodles
2 tbsp butter, melted
4 tsp soy sauce
⅔ cup stock
1 egg
½ tsp salt
For the topping:
2 scallions, sliced

Method
1. Boil the noodles for 8 minutes, then drain.
2. Melt 2 tbsp butter in a pan and add the noodles.
3. Season with salt and top with scallions.

Notes
Any thin noodle works.
"""


def test_parse_text_full_layout():
    d = parse_text(TYPED)
    r = d.recipe
    assert r.title == "Garlic butter noodles"
    assert r.base_servings == 2
    assert (r.prep_minutes, r.cook_minutes) == (5, 10)
    assert [i.item for i in r.ingredients] == ["noodles", "butter", "soy sauce", "stock", "egg", "salt", "scallions"]
    assert r.ingredients[-1].group == "topping"
    assert r.ingredients[4].scaling == "whole"
    assert r.ingredients[5].scaling == "to_taste"
    assert len(r.steps) == 3
    assert r.steps[0].timer_seconds == 480
    assert "{butter}" in r.steps[1].text
    assert r.notes == "Any thin noodle works."
    assert d.missing == []
    assert d.method == "local"
    assert r.source.type == "text"


def test_parse_text_without_headings():
    text = "Quick eggs\n2 eggs\n1 tbsp butter\nSalt to taste\n\nWhisk the eggs. Melt the butter in a pan and cook the eggs gently for 2 minutes."
    d = parse_text(text)
    r = d.recipe
    assert r.title == "Quick eggs"
    assert [i.item for i in r.ingredients] == ["eggs", "butter", "Salt"]
    assert len(r.steps) == 1
    assert r.steps[0].timer_seconds == 120
    assert any("Servings" in m for m in d.missing)
    assert r.base_servings == 4


def test_parse_duplicate_ids():
    d = parse_text("Two butters\nServes 1\n1 tbsp butter\n2 tbsp butter, browned\nStir.")
    ids = [i.id for i in d.recipe.ingredients]
    assert ids == ["butter", "butter-2"]


def test_units_and_scaling_rules():
    assert unit_from_word("T") == "tbsp" and unit_from_word("t") == "tsp"
    assert unit_from_word("Tablespoons") == "tbsp"
    assert unit_from_word("onion") is None
    assert default_scaling("tsp", "smoked paprika") == "to_taste"
    assert default_scaling("g", "salt") == "linear"  # weighed salt scales
    assert default_scaling("", "bay leaves") == "whole"


def test_total_time_becomes_cook_time():
    d = parse_text("Stew\nServes 4\nPrep: 20 min\nTotal time: 1 hr 30 min\n2 onions\nSimmer it all.")
    assert (d.recipe.prep_minutes, d.recipe.cook_minutes) == (20, 70)


@pytest.mark.parametrize(
    "text",
    [
        "Cookies\nMakes 200 cookies\n2 cups flour\nBake.",  # more servings than the model allows
        "Serves 0\n1 egg\nCook it.",
        "Title\n-\n-\n",  # bare bullets
        ":\n#\n",
        "Odd\n1/0 cup flour\n2 0/0 eggs\nMix.",
        "Big\nServes " + "9" * 5000 + "\n" + "9" * 5000 + " minutes\nPrep " + "9" * 5000 + " min",
        "W" * 300,
        "Long ids\n2 " + "x" * 80 + " " + "y" * 80 + "\n3 " + "x" * 80 + " " + "y" * 80 + "\nMix.",
        "",
    ],
)
def test_parse_text_never_raises(text):
    d = parse_text(text)
    assert 1 <= d.recipe.base_servings <= 100
    assert d.recipe.title


def test_servings_are_clamped_not_dropped():
    assert parse_text("Cookies\nMakes 200 cookies\n2 cups flour\nBake.").recipe.base_servings == 100


def test_long_digit_runs_stay_fast():
    started = time.monotonic()
    parse_text("Title\n" + "1" * 50_000 + "\n1. Wait " + "2" * 50_000 + " minutes.")
    assert time.monotonic() - started < 2


# Text at the import route's cap (MAX_TEXT) that used to cost minutes: a
# whitespace run in front of a pattern with two optional \s* (serves, prep,
# to taste, ingredient links tried every split of the run: 10,000 spaces took
# 8.5 s, so 50,000 about 210 s), and brackets that never close.
_RUN = " " * 49_000


@pytest.mark.parametrize(
    "text",
    [
        "Title\nserves" + _RUN + "x",
        "Title\nprep" + _RUN + "x",
        "Title\nIngredients\n1 g salt" + _RUN + "x",
        "Title\nIngredients\n1 g " + "(" * 49_000,
        "Title\nIngredients\n1 g q\n2 g r\n3 g s\nMethod\n1. 1" + _RUN + "z",
        "Title\nIngredients\n1 g q\nMethod\n1. 1" + "\u2003" * 49_000 + "z",
        # thousands of ingredients, each once a 2 ms pattern compile
        "Title\nIngredients\n" + "\n".join(f"1 g i{n}" for n in range(5_000)) + "\nMethod\n1. Add 1 g i1.",
    ],
)
def test_hostile_text_at_the_cap_stays_fast(text):
    from app.recipe_parse import MAX_TEXT

    assert len(text) <= MAX_TEXT
    started = time.monotonic()
    parse_text(text)
    assert time.monotonic() - started < 2


def test_runs_of_spaces_inside_a_line_read_as_one():
    d = parse_text("Pasta\nServes   4\nIngredients\n200  g   spaghetti\nMethod\n1.  Boil  200 g  spaghetti.")
    assert d.recipe.base_servings == 4
    assert d.recipe.ingredients[0].item == "spaghetti"
    assert d.recipe.steps[0].text == "Boil {spaghetti}."
