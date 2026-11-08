"""/api/recipes: the menu, search, menu order, and the item CRUD routes."""
import asyncio
import json

from app import recipes, sidebar_order
from app.api import notes as notes_api
from app.api import recipes as recipes_api
from app.index.db import content_hash
from app.recipe_models import RecipeIn
from app.vault import fm

from conftest import make_client


def run(coro):
    return asyncio.run(coro)


def client_for(state):
    return make_client(state, recipes_api.router, notes_api.router)


def body(title: str, **kw) -> dict:
    return RecipeIn(title=title, **kw).model_dump()


def create(client, title: str, **kw) -> dict:
    resp = client.post("/api/recipes", json=body(title, **kw))
    assert resp.status_code == 201, resp.text
    return resp.json()


def seed(state, title: str, created: str, **kw) -> str:
    """A recipe note with a chosen `created`, as an older one would have."""
    content = recipes.build_recipe_content(RecipeIn(title=title, **kw), created=created)
    path = recipes.next_recipe_path(title, {n.path for n in state.store.list_notes()})
    written = state.store.create(path, content, check_name=False)
    run(state.indexer.index_note(path, written))
    return path


def events(queue) -> list[dict]:
    out = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


def config(state) -> dict:
    path = state.settings.vault_dir / ".exscalia" / "config.json"
    return json.loads(path.read_text()) if path.exists() else {}


# -- create, get -----------------------------------------------------------------


def test_create_writes_a_note_and_answers_the_recipe(state, monkeypatch):
    from app import media_lookup

    fetched = []

    async def ingest(_state, url, name_hint="cover", **_kw):
        fetched.append((url, name_hint))
        return {"path": "attachments/c-0123456789abcdef.jpg", "url": "/api/attachments/c-0123456789abcdef.jpg"}

    monkeypatch.setattr(media_lookup, "ingest_cover", ingest)
    queue = state.live.subscribe()
    client = client_for(state)
    payload = body(
        "Spaghetti: Carbonara",
        base_servings=3,
        tags=["Pasta", "quick"],
        ingredients=[{"id": "eggs", "amount": 3, "unit": "", "item": "eggs"}],
        steps=[{"text": "Whisk {eggs}."}],
        notes="Use guanciale.",
        source={"type": "website", "url": "https://example.com/c", "thumbnail": "https://img.example.com/c.jpg"},
    )
    resp = client.post("/api/recipes", json={**payload, "client_id": "tab-1"})
    assert resp.status_code == 201, resp.text
    recipe = resp.json()
    assert recipe["path"] == "recipes/Spaghetti Carbonara.md"
    assert recipe["title"] == "Spaghetti: Carbonara"
    assert recipe["tags"] == ["pasta", "quick"]
    assert recipe["notes"] == "Use guanciale."
    # A remote thumbnail is stored as an attachment (test_recipes_import_api.py).
    assert recipe["source"]["thumbnail"] == "/api/attachments/c-0123456789abcdef.jpg"
    assert fetched == [("https://img.example.com/c.jpg", "Spaghetti Carbonara")]
    content = state.store.read(recipe["path"])
    assert recipe["revision"] == content_hash(content)
    assert recipe["created"] and recipe["modified"]
    assert set(recipe) == {
        "title", "base_servings", "prep_minutes", "cook_minutes", "ingredients", "steps",
        "notes", "tags", "source", "art", "art_colors", "art_palette",
        "path", "revision", "created", "modified",
    }
    assert events(queue) == [
        {"type": "note", "path": recipe["path"], "revision": recipe["revision"], "origin": "tab-1"}
    ]
    assert [c["path"] for c in client.get("/api/recipes").json()["recipes"]] == [recipe["path"]]


def test_create_steps_past_a_taken_name_and_refuses_a_blank_title(state):
    client = client_for(state)
    assert create(client, "Soup")["path"] == "recipes/Soup.md"
    assert create(client, "Soup")["path"] == "recipes/Soup 2.md"
    assert client.post("/api/recipes", json=body("   ")).status_code == 400
    assert client.post("/api/recipes", json={"title": ""}).status_code == 422


def test_get_answers_the_recipe_and_404s_anything_else(state):
    client = client_for(state)
    made = create(client, "Soup", notes="Hot.")
    got = client.get(f"/api/recipes/item/{made['path']}")
    assert got.status_code == 200
    assert got.json() == made

    state.store.create("Plain.md", "---\ntitle: Plain\nrecipe: true\n---\n\nhi\n")
    assert client.get("/api/recipes/item/Plain.md").status_code == 404
    assert client.get("/api/recipes/item/recipes/Missing.md").status_code == 404
    assert client.get("/api/recipes/item/recipes/x.txt").status_code == 400


# -- update ----------------------------------------------------------------------


def test_update_with_a_fresh_revision_rewrites_the_note(state):
    queue = state.live.subscribe()
    client = client_for(state)
    made = create(client, "Soup", art="~~\n~~ ", art_palette={"a": "#fff"})
    events(queue)
    resp = client.put(
        f"/api/recipes/item/{made['path']}",
        json={
            "recipe": body("Better Soup", tags=["Winter"], notes="Hotter."),
            "base_revision": made["revision"],
            "client_id": "tab-2",
        },
    )
    assert resp.status_code == 200, resp.text
    updated = resp.json()
    assert updated["path"] == made["path"]  # renaming changes the title only
    assert updated["title"] == "Better Soup"
    assert updated["tags"] == ["winter"]
    assert updated["notes"] == "Hotter."
    assert updated["created"] == made["created"]
    # No art in the update: the stored drawing stays.
    assert updated["art"] == "~~\n~~ " and updated["art_palette"] == {"a": "#fff"}
    assert updated["revision"] == content_hash(state.store.read(made["path"]))
    assert events(queue) == [
        {"type": "note", "path": made["path"], "revision": updated["revision"], "origin": "tab-2"}
    ]
    card = client.get("/api/recipes").json()["recipes"][0]
    assert card["title"] == "Better Soup" and card["tags"] == ["winter"]


def test_update_with_a_stale_revision_is_a_409_and_writes_nothing(state):
    client = client_for(state)
    made = create(client, "Soup")
    before = state.store.read(made["path"])
    resp = client.put(
        f"/api/recipes/item/{made['path']}",
        json={"recipe": body("Other"), "base_revision": "0" * 64},
    )
    assert resp.status_code == 409
    assert state.store.read(made["path"]) == before
    # Without a base revision the write goes through.
    resp = client.put(f"/api/recipes/item/{made['path']}", json={"recipe": body("Other")})
    assert resp.status_code == 200 and resp.json()["title"] == "Other"


def test_update_keeps_keys_the_user_added(state):
    client = client_for(state)
    made = create(client, "Soup")
    content = state.store.read(made["path"])
    meta, note_body = fm.parse(content)
    meta["rating"] = "five stars"
    state.store.write(made["path"], fm.serialize(meta, note_body))
    resp = client.put(f"/api/recipes/item/{made['path']}", json={"recipe": body("Soup", base_servings=6)})
    assert resp.status_code == 200
    assert "rating: five stars\n" in state.store.read(made["path"])


def test_update_of_a_note_that_is_no_recipe_is_a_404(state):
    client = client_for(state)
    state.store.create("Plain.md", "plain\n")
    resp = client.put("/api/recipes/item/Plain.md", json={"recipe": body("Plain")})
    assert resp.status_code == 404
    assert state.store.read("Plain.md").endswith("plain\n")


# -- delete ----------------------------------------------------------------------


def test_delete_sends_the_recipe_to_trash(state):
    queue = state.live.subscribe()
    client = client_for(state)
    made = create(client, "Soup")
    events(queue)
    resp = client.delete(f"/api/recipes/item/{made['path']}", headers={"X-Client-Id": "tab-3"})
    assert resp.status_code == 204
    assert resp.content == b""
    assert not state.store.exists(made["path"])
    assert client.get("/api/recipes").json() == {"recipes": [], "tags": []}
    assert events(queue) == [{"type": "note_removed", "path": made["path"], "origin": "tab-3"}]
    [item] = state.trash.list()
    assert item["original_path"] == made["path"]

    # Restored, it is a recipe again.
    state.trash.restore(item["id"])
    run(state.indexer.index_note(made["path"], state.store.read(made["path"])))
    assert [c["path"] for c in client.get("/api/recipes").json()["recipes"]] == [made["path"]]


def test_delete_refuses_a_note_that_is_no_recipe(state):
    client = client_for(state)
    state.store.create("Plain.md", "plain\n")
    assert client.delete("/api/recipes/item/Plain.md").status_code == 404
    assert state.store.exists("Plain.md")
    assert client.delete("/api/recipes/item/recipes/Missing.md").status_code == 404


# -- the menu: order, search, tags ---------------------------------------------------


def test_menu_order_is_newest_first_until_an_order_is_saved(state):
    client = client_for(state)
    a = seed(state, "Apple Pie", "2026-01-01T00:00:00Z")
    b = seed(state, "Borscht", "2026-02-01T00:00:00Z")
    c = seed(state, "Curry", "2026-03-01T00:00:00Z")
    listed = lambda: [r["path"] for r in client.get("/api/recipes").json()["recipes"]]  # noqa: E731
    assert listed() == [c, b, a]

    # Unlisted recipes first, newest first; then the saved ones in order.
    resp = client.put("/api/recipes/order", json={"order": [a, "recipes/Gone.md", b, a]})
    assert resp.status_code == 200
    assert resp.json() == {"order": [c, a, b]}
    assert config(state)["recipe_order"] == [a, b]
    assert listed() == [c, a, b]

    # A new recipe goes on top without a write to the list.
    d = create(client, "Dal")["path"]
    assert listed() == [d, c, a, b]
    assert config(state)["recipe_order"] == [a, b]

    # A deleted one leaves a stale path behind, which the menu ignores.
    assert client.delete(f"/api/recipes/item/{a}").status_code == 204
    assert listed() == [d, c, b]
    assert client.put("/api/recipes/order", json={"order": [b, c, d]}).json() == {"order": [b, c, d]}

    # An empty order clears the key and falls back to newest first.
    assert client.put("/api/recipes/order", json={"order": []}).json() == {"order": [d, c, b]}
    assert "recipe_order" not in config(state)


def test_order_ignores_paths_that_are_not_recipes_and_keeps_other_config(state):
    client = client_for(state)
    config_path = state.settings.vault_dir / ".exscalia" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps({"media_profile": {"name": "Me"}, "note_order": ["x.md"]}))
    state.store.create("Plain.md", "plain\n")
    a = seed(state, "Apple Pie", "2026-01-01T00:00:00Z")
    assert client.put("/api/recipes/order", json={"order": ["Plain.md", a]}).json() == {"order": [a]}
    saved = config(state)
    assert saved["recipe_order"] == [a]
    assert saved["media_profile"] == {"name": "Me"} and saved["note_order"] == ["x.md"]


def test_a_stale_path_in_a_hand_edited_order_is_ignored(state):
    client = client_for(state)
    a = seed(state, "Apple Pie", "2026-01-01T00:00:00Z")
    b = seed(state, "Borscht", "2026-02-01T00:00:00Z")
    config_path = state.settings.vault_dir / ".exscalia" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps({"recipe_order": ["recipes/Gone.md", a, 7, b]}))
    assert [r["path"] for r in client.get("/api/recipes").json()["recipes"]] == [a, b]


def test_the_order_follows_a_moved_recipe(state):
    a = seed(state, "Apple Pie", "2026-01-01T00:00:00Z")
    b = seed(state, "Borscht", "2026-02-01T00:00:00Z")
    run(recipes.set_order(state, [a, b]))
    moved = "recipes/Desserts/Apple Pie.md"
    assert sidebar_order.rename_paths(state.settings.vault_dir, notes=[(a, moved)])
    assert config(state)["recipe_order"] == [moved, b]


def test_search_ands_words_over_title_ingredients_tags_and_notes(state):
    client = client_for(state)
    seed(state, "Crème Brûlée", "2026-01-01T00:00:00Z", tags=["Dessert"],
         ingredients=[{"id": "cream", "unit": "ml", "amount": 500, "item": "double cream"}])
    seed(state, "Tomato Soup", "2026-02-01T00:00:00Z", tags=["soup", "quick"],
         notes="Serve with basil.")
    seed(state, "Cream of Tomato", "2026-03-01T00:00:00Z", tags=["soup"])

    def titles(**params):
        resp = client.get("/api/recipes", params=params)
        assert resp.status_code == 200
        return [r["title"] for r in resp.json()["recipes"]]

    assert titles(q="cream") == ["Cream of Tomato", "Crème Brûlée"]
    assert titles(q="CRÈME") == ["Crème Brûlée"]
    assert titles(q="tomato soup") == ["Cream of Tomato", "Tomato Soup"]
    assert titles(q="tomato basil") == ["Tomato Soup"]
    assert titles(q="dessert") == ["Crème Brûlée"]
    assert titles(q="tomato pudding") == []
    assert titles(tag="Soup") == ["Cream of Tomato", "Tomato Soup"]
    assert titles(q="cream", tag="soup") == ["Cream of Tomato"]
    assert titles(tag="sou") == []
    # Tag counts cover the whole library, whatever the filter.
    resp = client.get("/api/recipes", params={"q": "basil"}).json()
    assert resp["tags"] == [
        {"tag": "soup", "count": 2},
        {"tag": "dessert", "count": 1},
        {"tag": "quick", "count": 1},
    ]


def test_cards_carry_what_the_menu_shows(state):
    client = client_for(state)
    made = create(
        client, "Soup", prep_minutes=5, cook_minutes=20, tags=["b", "a"],
        source={"type": "photo", "thumbnail": "/api/attachments/x.jpg"},
        art="o \n o", art_colors="a \n a", art_palette={"a": "#123456"},
    )
    [card] = client.get("/api/recipes").json()["recipes"]
    assert card == {
        "path": made["path"],
        "title": "Soup",
        "total_minutes": 25,
        "thumbnail": "/api/attachments/x.jpg",
        "tags": ["a", "b"],
        "modified": made["modified"],
        "art": "o \n o",
        "art_colors": "a \n a",
        "art_palette": {"a": "#123456"},
    }


def test_a_broken_recipe_note_does_not_break_the_list(state):
    client = client_for(state)
    good = create(client, "Soup")
    vault = state.settings.vault_dir
    (vault / "recipes" / "Broken.md").write_text(
        "---\ntitle: Broken\nrecipe: 1\ningredients: 7\nsteps:\n- {}\nservings: x\n---\n\n",
        encoding="utf-8",
    )
    run(state.indexer.sync_all())
    resp = client.get("/api/recipes")
    assert resp.status_code == 200
    assert {r["path"] for r in resp.json()["recipes"]} == {good["path"], "recipes/Broken.md"}
    broken = client.get("/api/recipes/item/recipes/Broken.md").json()
    assert broken["ingredients"] == [] and broken["steps"] == [] and broken["base_servings"] == 2


# -- store_art (the AI slice calls it) -------------------------------------------


def test_store_art_keeps_modified_and_respects_only_if_empty(state):
    from app.recipe_models import Drawing

    queue = state.live.subscribe()
    client = client_for(state)
    made = create(client, "Soup")
    events(queue)
    stored = run(recipes.store_art(state, made["path"], Drawing(art="ab ", art_palette={"a": "#000"})))
    assert stored.art == "ab " and stored.modified == made["modified"]
    assert events(queue) == [
        {"type": "note", "path": made["path"], "revision": stored.revision, "origin": None}
    ]
    kept = run(recipes.store_art(state, made["path"], Drawing(art="zz"), only_if_empty=True))
    assert kept.art == "ab "
    assert json.loads(run(state.db.execute_fetchall("SELECT art FROM recipes"))[0]["art"])["art"] == "ab "
    assert run(recipes.store_art(state, "recipes/Gone.md", Drawing(art="x"))) is None


# -- identity: recipe: 1 under recipes/ -------------------------------------------


def move(state, old: str, new: str) -> None:
    state.store.rename(old, new)
    run(state.indexer.rename_note(old, new))


def test_a_marked_note_outside_the_recipes_folder_is_an_ordinary_note(state):
    from app.recipe_models import Drawing

    client = client_for(state)
    content = recipes.build_recipe_content(RecipeIn(title="Pie", tags=["dessert"]))
    written = state.store.create("Notes/Pie.md", content)
    run(state.indexer.index_note("Notes/Pie.md", written))

    listed = client.get("/api/recipes").json()
    assert listed == {"recipes": [], "tags": []}
    assert client.get("/api/recipes/item/Notes/Pie.md").status_code == 404
    resp = client.put("/api/recipes/item/Notes/Pie.md", json={"recipe": body("Cake")})
    assert resp.status_code == 404
    assert client.delete("/api/recipes/item/Notes/Pie.md").status_code == 404
    assert run(recipes.store_art(state, "Notes/Pie.md", Drawing(art="x"))) is None
    assert state.store.read("Notes/Pie.md") == written


def test_moving_a_recipe_out_and_back_drops_and_restores_it(state):
    client = client_for(state)
    made = create(client, "Soup", tags=["winter"])

    def paths():
        return [c["path"] for c in client.get("/api/recipes").json()["recipes"]]

    move(state, made["path"], "Archive/Soup.md")
    assert paths() == [] and client.get("/api/recipes").json()["tags"] == []
    assert client.get("/api/recipes/item/Archive/Soup.md").status_code == 404

    # Nested folders under recipes/ count as inside.
    move(state, "Archive/Soup.md", "recipes/Winter/Soup.md")
    assert paths() == ["recipes/Winter/Soup.md"]
    got = client.get("/api/recipes/item/recipes/Winter/Soup.md")
    assert got.status_code == 200 and got.json()["title"] == "Soup"
    assert client.get("/api/recipes").json()["tags"] == [{"tag": "winter", "count": 1}]


# -- folder protection -------------------------------------------------------------


def test_the_recipes_folder_is_built_in(state):
    client = client_for(state)
    create(client, "Soup")
    renamed = client.post("/api/notes/folders/rename", json={"path": "recipes", "new_path": "food"})
    assert renamed.status_code == 400
    assert client.post("/api/notes/folders/delete", json={"path": "Recipes/"}).status_code == 400
    assert state.store.exists("recipes/Soup.md")


def test_a_recipe_answers_to_one_spelling_of_its_path(state):
    """"recipes/./Soup.md" and "recipes//Soup.md" reach the file, and every
    write through them indexed a row of its own: a menu entry that stayed
    after the recipe was deleted, and through which the store's title rule
    renamed it to its file name."""
    client = client_for(state)
    made = create(client, "Soup")
    for alias in ("recipes/./Soup.md", "recipes//Soup.md", "./recipes/Soup.md", "recipes/Soup.md/"):
        try:
            run(recipes.get_recipe(state, alias))
        except recipes.NoteError as exc:
            assert exc.status in (400, 404), alias
        else:
            raise AssertionError(f"{alias} answered")
        for call in (
            recipes.update_recipe(state, alias, RecipeIn(title="Renamed"), base_revision=None),
            recipes.delete_recipe(state, alias),
            recipes.store_art(state, alias, recipes.Drawing(art="x")),
        ):
            try:
                result = run(call)
            except recipes.NoteError:
                continue
            assert result is None, alias  # store_art: as for a recipe that is gone
    assert client.put("/api/recipes/item/recipes//Soup.md", json={"recipe": body("Renamed")}).status_code == 404
    assert client.get("/api/recipes/item/recipes/%2E/Soup.md").status_code == 404
    assert [c["path"] for c in client.get("/api/recipes").json()["recipes"]] == [made["path"]]
    assert run(recipes.get_recipe(state, made["path"])).title == "Soup"
