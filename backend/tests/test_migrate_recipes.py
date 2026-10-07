"""scripts/migrate_recipes.py, the one-off move from the standalone recipe app:
the old record maps to a body the real RecipeIn accepts, and the old menu order
reads the way the old app showed it. The script is standard library only and
lives outside the package, so it is imported by path."""
import base64
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from app.recipe_models import RecipeIn

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "migrate_recipes.py"
_spec = importlib.util.spec_from_file_location("migrate_recipes", SCRIPT)
migrate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(migrate)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16


def data_url(data: bytes, media_type: str = "image/png") -> str:
    return f"data:{media_type};base64," + base64.b64encode(data).decode()


def old_record(**overrides) -> dict:
    """A record as the old app stored it: a RecipeIn plus id and timestamps."""
    record = {
        "id": "x1y2z3",
        "created_at": "2026-09-01T10:00:00.000+00:00",
        "updated_at": "2026-09-02T10:00:00.000+00:00",
        "title": "Garlic butter noodles",
        "base_servings": 2,
        "prep_minutes": 5,
        "cook_minutes": None,
        "ingredients": [
            {"id": "butter", "amount": 2.0, "unit": "tbsp", "item": "butter", "prep": "melted",
             "scaling": "linear", "estimated": False, "group": "Sauce"},
            {"id": "salt", "amount": None, "unit": "", "item": "salt", "prep": "",
             "scaling": "to_taste", "estimated": True, "group": None},
        ],
        "steps": [{"text": "Melt {butter}.", "timer_seconds": 60}, {"text": "Season.", "timer_seconds": None}],
        "notes": "Any thin noodle works.",
        "tags": ["dinner", "pasta"],
        "source": {"type": "website", "url": "https://example.com/n", "thumbnail": data_url(PNG)},
        "art": "  .-.  \n (   ) ",
        "art_colors": "  aaa  \n a...a ",
        "art_palette": {"a": "#e8c070"},
    }
    record.update(overrides)
    return record


def test_a_sound_record_carries_over_whole_without_its_identity():
    record = old_record()
    body, warnings = migrate.to_recipe_in(record)
    assert warnings == []
    assert body == {k: v for k, v in record.items() if k not in ("id", "created_at", "updated_at")}
    # Already in the stored shape: the real model takes it unchanged.
    assert RecipeIn.model_validate(body).model_dump() == body


def test_an_awkward_record_degrades_to_what_the_model_accepts():
    record = {
        "title": "  Tomato \n soup ",
        "base_servings": 0,
        "prep_minutes": "15",
        "ingredients": [
            {"item": "Tomatoes", "amount": "800", "unit": "grams"},
            {"id": "basil", "item": "basil", "amount": 1, "unit": "handful"},
            {"id": "cream", "item": "cream", "amount": "a splash", "unit": "Tbsp", "scaling": "double"},
            {"id": "orphan"},
            "salt",
        ],
        "steps": [{"text": "Simmer.", "timer_seconds": 0}, {"text": ""}, "Blend."],
        "tags": "Soup, Vegetarian ,soup",
        "source": {"type": "instagram", "thumbnail": "data:image/png;base64,@@@"},
        "art": "x" * 4001,
        "art_palette": {"a": "#fff"},
    }
    body, warnings = migrate.to_recipe_in(record)
    RecipeIn.model_validate(body)
    assert body["title"] == "Tomato soup"
    assert body["base_servings"] == 2 and body["prep_minutes"] == 15 and body["cook_minutes"] is None
    tomatoes, basil, cream = body["ingredients"]
    assert (tomatoes["id"], tomatoes["amount"], tomatoes["unit"]) == ("tomatoes", 800.0, "g")
    assert (basil["unit"], basil["item"]) == ("", "handful basil")  # unknown unit kept in the item
    assert (cream["amount"], cream["unit"], cream["scaling"]) == (None, "tbsp", "linear")
    assert body["steps"] == [{"text": "Simmer.", "timer_seconds": None}, {"text": "Blend.", "timer_seconds": None}]
    assert body["tags"] == ["soup", "vegetarian"]
    assert body["source"] == {"type": "typed", "url": None, "thumbnail": None}
    assert (body["art"], body["art_colors"], body["art_palette"]) == ("", "", {})
    joined = "; ".join(warnings)
    for fragment in ('unit "handful"', "ingredient 4 dropped", "ingredient 5 dropped", "step 2 dropped",
                     "timer 0", "instagram", "thumbnail dropped", "drawing dropped", "servings 0"):
        assert fragment in joined


def test_missing_fields_take_their_defaults_and_a_remote_thumbnail_passes_through():
    body, warnings = migrate.to_recipe_in(
        {"title": "Toast", "source": {"type": "website", "thumbnail": "https://example.com/t.jpg"}}
    )
    assert warnings == []
    assert body["base_servings"] == 2 and body["ingredients"] == [] and body["steps"] == []
    assert body["source"]["thumbnail"] == "https://example.com/t.jpg"
    RecipeIn.model_validate(body)


def test_the_title_column_stands_in_and_no_title_at_all_is_invalid():
    assert migrate.to_recipe_in({"title": ""}, "Pancakes")[0]["title"] == "Pancakes"
    with pytest.raises(migrate.InvalidRecord):
        migrate.to_recipe_in({"title": "   "}, "")
    with pytest.raises(migrate.InvalidRecord):
        migrate.to_recipe_in(["not", "a", "record"])


def test_data_url_thumbnails_decode_to_an_attachment_type():
    assert migrate.decode_data_url(data_url(PNG)) == (PNG, ".png")
    assert migrate.decode_data_url(data_url(JPEG, "image/jpeg")) == (JPEG, ".jpg")
    assert migrate.decode_data_url(data_url(JPEG, "")) == (JPEG, ".jpg")  # sniffed
    for bad in ("data:image/svg+xml;base64," + base64.b64encode(b"<svg/>").decode(),
                "data:image/png;base64,", "data:image/png;base64,!!", "https://example.com/a.png"):
        with pytest.raises(ValueError):
            migrate.decode_data_url(bad)


def test_menu_order_reads_as_the_old_app_showed_it(tmp_path):
    db = tmp_path / "recipes.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE recipes (id TEXT PRIMARY KEY, title TEXT NOT NULL, data TEXT NOT NULL,"
        " search TEXT NOT NULL, tags TEXT NOT NULL, created_at TEXT NOT NULL,"
        " updated_at TEXT NOT NULL, position REAL)"
    )
    # rowid 1..5. Position ascending, ties newest row first, unnumbered rows
    # last and newest edit first.
    for rid, position, updated in (("a", 1, "1"), ("b", 0, "1"), ("c", 0, "1"), ("d", None, "1"), ("e", None, "2")):
        conn.execute("INSERT INTO recipes VALUES (?, ?, ?, '', '[]', '', ?, ?)",
                     (rid, rid.upper(), json.dumps({"title": rid.upper()}), updated, position))
    conn.commit()
    conn.execute("PRAGMA journal_mode=WAL")  # the old app's mode; must not stop a read
    conn.close()
    assert [row.id for row in migrate.read_old_recipes(str(db))] == ["c", "b", "a", "e", "d"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["recipes.db"]  # nothing left beside it


def test_existing_titles_pair_once_each_so_duplicates_both_migrate():
    rows = [migrate.OldRow(i, str(i), "", json.dumps({"title": t})) for i, t in enumerate(["Pancakes", "Soup", "Pancakes"])]
    plans = migrate.plan_rows(rows)
    others = migrate.match_existing(plans, [
        {"path": "recipes/Toast.md", "title": "Toast"},
        {"path": "recipes/Pancakes.md", "title": "Pancakes"},
    ])
    assert [p.existing for p in plans] == ["recipes/Pancakes.md", None, None]
    assert others == ["recipes/Toast.md"]
