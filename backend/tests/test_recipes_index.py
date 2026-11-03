"""The `recipes` index table: filled from `recipe: 1` notes, kept in step with
every save, rename and delete, and rebuilt when INDEX_VERSION moves."""
import asyncio
import json
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from app import recipes
from app.index import indexer as indexer_module
from app.index.db import INDEX_VERSION
from app.recipe_models import Ingredient, RecipeIn

from conftest import test_settings


def run(coro):
    return asyncio.run(coro)


def rows(state, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in run(state.db.execute_fetchall(sql, params))]


def write(state, path: str, content: str) -> str:
    if state.store.exists(path):
        written = state.store.write(path, content)
    else:
        written = state.store.create(path, content, check_name=False)
    run(state.indexer.index_note(path, written))
    return written


def recipe_note(title: str = "Pancakes", **kw) -> str:
    values = dict(
        title=title,
        prep_minutes=5,
        cook_minutes=10,
        tags=["Breakfast"],
        ingredients=[Ingredient(id="flour", amount=200, unit="g", item="flour")],
        art="ab \ncd ",
    )
    values.update(kw)
    return recipes.build_recipe_content(RecipeIn(**values))


def test_a_saved_recipe_is_indexed_with_its_card_fields(state):
    write(state, "recipes/Pancakes.md", recipe_note())
    [row] = rows(state, "SELECT * FROM recipes")
    assert row["path"] == "recipes/Pancakes.md"
    assert row["title"] == "Pancakes"
    assert json.loads(row["tags"]) == ["breakfast"]
    assert row["servings"] == 2
    assert row["total_minutes"] == 15
    assert "flour" in row["search"] and "pancakes" in row["search"]
    assert json.loads(row["art"])["art"] == "ab \ncd "
    assert row["created"] and row["modified"]


def test_an_ordinary_note_gets_no_row(state):
    write(state, "Plain.md", "---\ntitle: Plain\nrecipe: true\n---\n\nhello\n")
    write(state, "media/Dune.md", "---\ntitle: Dune\nmedia: book\n---\n\n")
    # The marker outside recipes/ is not a recipe either.
    write(state, "Pancakes.md", recipe_note())
    write(state, "Food/recipes/Pancakes.md", recipe_note())
    assert rows(state, "SELECT * FROM recipes") == []


def test_a_change_reindexes_the_row(state):
    content = write(state, "recipes/Pancakes.md", recipe_note())
    write(state, "recipes/Pancakes.md", recipes.apply_recipe(content, RecipeIn(title="Crepes", base_servings=4)))
    [row] = rows(state, "SELECT title, servings, total_minutes FROM recipes")
    assert row == {"title": "Crepes", "servings": 4, "total_minutes": None}


def test_a_note_that_stops_being_a_recipe_loses_its_row(state):
    content = write(state, "recipes/Pancakes.md", recipe_note())
    write(state, "recipes/Pancakes.md", content.replace("recipe: 1\n", "recipe: true\n"))
    assert rows(state, "SELECT * FROM recipes") == []
    assert rows(state, "SELECT path FROM notes WHERE path = 'recipes/Pancakes.md'")


def test_a_deleted_recipe_loses_its_row(state):
    write(state, "recipes/Pancakes.md", recipe_note())
    state.trash.trash_note("recipes/Pancakes.md")
    run(state.indexer.remove_note("recipes/Pancakes.md"))
    assert rows(state, "SELECT * FROM recipes") == []


def move(state, old: str, new: str) -> None:
    state.store.rename(old, new)
    run(state.indexer.rename_note(old, new))


def test_a_recipe_moved_within_the_folder_moves_its_row(state):
    write(state, "recipes/Pancakes.md", recipe_note())
    move(state, "recipes/Pancakes.md", "recipes/Breakfast/Pancakes.md")
    assert [r["path"] for r in rows(state, "SELECT path FROM recipes")] == ["recipes/Breakfast/Pancakes.md"]


def test_a_recipe_moved_out_loses_its_row_and_moved_back_regains_it(state):
    write(state, "recipes/Pancakes.md", recipe_note())
    move(state, "recipes/Pancakes.md", "Breakfast/Pancakes.md")
    assert rows(state, "SELECT * FROM recipes") == []
    assert rows(state, "SELECT path FROM notes WHERE path = 'Breakfast/Pancakes.md'")
    move(state, "Breakfast/Pancakes.md", "recipes/Pancakes.md")
    [row] = rows(state, "SELECT path, title FROM recipes")
    assert row == {"path": "recipes/Pancakes.md", "title": "Pancakes"}


def test_a_full_sync_indexes_only_the_recipes_folder(state):
    vault = state.settings.vault_dir
    for rel in ("recipes/A.md", "recipes/Nested/B.md", "Elsewhere/C.md", "D.md"):
        (vault / rel).parent.mkdir(parents=True, exist_ok=True)
        (vault / rel).write_text(recipe_note(Path(rel).stem), encoding="utf-8")
    run(state.indexer.sync_all())
    got = sorted(r["path"] for r in rows(state, "SELECT path FROM recipes"))
    assert got == ["recipes/A.md", "recipes/Nested/B.md"]


BROKEN = """---
title: Broken
recipe: 1
servings: many
ingredients:
- 5
- id: x
steps: not a list
art: {a: b}
---

still notes
"""


def test_a_malformed_recipe_note_never_stops_indexing(state):
    vault = state.settings.vault_dir
    (vault / "recipes").mkdir()
    (vault / "recipes" / "Broken.md").write_text(BROKEN, encoding="utf-8")
    (vault / "recipes" / "Good.md").write_text(recipe_note("Good"), encoding="utf-8")
    (vault / "Other.md").write_text("plain #tag\n", encoding="utf-8")
    run(state.indexer.sync_all())
    got = {r["path"]: r for r in rows(state, "SELECT * FROM recipes")}
    assert set(got) == {"recipes/Broken.md", "recipes/Good.md"}
    assert got["recipes/Broken.md"]["servings"] == 2
    assert got["recipes/Broken.md"]["title"] == "Broken"
    assert rows(state, "SELECT tag FROM tags WHERE path = 'Other.md'") == [{"tag": "tag"}]


def test_a_recipe_row_that_cannot_be_built_still_indexes_the_note(state, monkeypatch):
    def explode(meta, body, fallback_title):
        raise RuntimeError("boom")

    monkeypatch.setattr(indexer_module.recipes, "card_fields", explode)
    write(state, "recipes/Pancakes.md", recipe_note() + "\n#tagged\n")
    assert rows(state, "SELECT * FROM recipes") == []
    assert rows(state, "SELECT path FROM notes WHERE path = 'recipes/Pancakes.md'")
    assert rows(state, "SELECT tag FROM tags WHERE path = 'recipes/Pancakes.md'") == [{"tag": "tagged"}]


# --- the index version ------------------------------------------------------------


def _boot(settings) -> None:
    from app import main as main_module

    with TestClient(main_module.app) as client:
        assert client.get("/api/health").status_code == 200


def _read(index_dir: Path, sql: str) -> list[tuple]:
    with sqlite3.connect(index_dir / "metadata.db") as db:
        return [tuple(r) for r in db.execute(sql)]


def test_boot_after_the_version_bump_fills_the_table(tmp_path, monkeypatch):
    """A database from before the recipes table: the schema adds the table
    empty, and only the version mismatch makes boot re-derive the notes that
    the mtime check would otherwise skip."""
    from app import main as main_module

    assert INDEX_VERSION >= 3
    settings = test_settings(tmp_path)
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    (settings.vault_dir / "recipes").mkdir(parents=True)
    (settings.vault_dir / "recipes" / "Pancakes.md").write_text(recipe_note(), encoding="utf-8")

    _boot(settings)
    assert _read(settings.index_dir, "SELECT path FROM recipes") == [("recipes/Pancakes.md",)]

    with sqlite3.connect(settings.index_dir / "metadata.db") as db:
        db.execute("DROP TABLE recipes")
        db.execute("PRAGMA user_version = 2")
    _boot(settings)
    assert _read(settings.index_dir, "SELECT path, title FROM recipes") == [
        ("recipes/Pancakes.md", "Pancakes")
    ]
    assert _read(settings.index_dir, "PRAGMA user_version") == [(INDEX_VERSION,)]
