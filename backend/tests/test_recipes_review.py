"""Recipes, from an independent review of the backend: the races between a
read, an await and a write, names the filesystem calls the same, index rows a
rollback leaves behind, and where the OMNIA tools had drifted from the routes.
"""
import asyncio
import json
import time

from app import media_lookup, recipe_ai, recipe_art, recipe_import, recipe_parse, recipes
from app.index.db import content_hash
from app.overseer import tool_expansion as tx
from app.overseer.agent import Overseer
from app.overseer.provider import FakeProvider, ProviderReply
from app.overseer.tools import RECIPES_DOMAIN, TOOL_DOMAINS, execute_tool
from app.recipe_models import Drawing, RecipeIn, Source
from app.vault import fm

RECIPE_TOOLS = TOOL_DOMAINS[RECIPES_DOMAIN][1]


def run(coro):
    return asyncio.run(coro)


# -- names ------------------------------------------------------------------------


def test_a_new_name_steps_past_one_the_store_would_call_the_same():
    # The store refuses a sibling that matches once NFC-normalized and
    # case-folded (names.identity_key); lower() alone missed these two.
    assert recipes.next_recipe_path("Straße", {"recipes/Strasse.md"}) == "recipes/Straße 2.md"
    assert recipes.next_recipe_path("Café", {"recipes/Café.md"}) == "recipes/Café 2.md"


def test_creating_a_recipe_named_like_an_existing_one_is_never_a_409(state):
    nfd = "recipes/Café.md"
    state.store.create(nfd, recipes.build_recipe_content(RecipeIn(title="Café")), check_name=False)
    first = run(recipes.create_recipe(state, RecipeIn(title="Strasse")))
    second = run(recipes.create_recipe(state, RecipeIn(title="Straße")))
    third = run(recipes.create_recipe(state, RecipeIn(title="Café")))
    assert first.path == "recipes/Strasse.md"
    assert second.path == "recipes/Straße 2.md"
    assert third.path == "recipes/Café 2.md"


# -- the index ----------------------------------------------------------------------


def test_a_full_sync_drops_a_recipe_row_whose_note_is_gone(state):
    """A version without the recipes table (a rollback) deletes or moves a
    recipe note: it drops the notes row and leaves the recipes row. The next
    boot's sync must not keep showing that card."""
    made = run(recipes.create_recipe(state, RecipeIn(title="Ghost Soup")))
    kept = run(recipes.create_recipe(state, RecipeIn(title="Real Soup")))
    (state.settings.vault_dir / made.path).unlink()

    async def older_version_forgets_the_note():
        await state.db.execute("DELETE FROM notes WHERE path = ?", (made.path,))
        await state.db.commit()

    run(older_version_forgets_the_note())
    run(state.indexer.sync_all(force=True))
    paths = [row["path"] for row in run(state.db.execute_fetchall("SELECT path FROM recipes"))]
    assert paths == [kept.path]
    assert [card.path for card in run(recipes.list_cards(state))] == [kept.path]


# -- reads, awaits and writes --------------------------------------------------------


def test_an_edit_made_while_a_thumbnail_downloads_is_never_overwritten(state, monkeypatch):
    """update_recipe reads, downloads the thumbnail, then writes: the note it
    writes over is read again after the download, so an edit that landed in
    the meantime turns the update into a 409 rather than vanishing."""
    made = run(recipes.create_recipe(state, RecipeIn(title="Pie", notes="v1")))

    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_ingest(_state, url, name_hint="cover", **_kw):
            started.set()
            await release.wait()
            return {"url": "/api/attachments/pie-0000.jpg"}

        monkeypatch.setattr(media_lookup, "ingest_cover", slow_ingest)
        update = asyncio.create_task(
            recipes.update_recipe(
                state,
                made.path,
                RecipeIn(title="Pie", notes="from the form",
                         source=Source(type="website", thumbnail="https://img.example.com/p.jpg")),
                base_revision=made.revision,
            )
        )
        await started.wait()
        # The owner edits the note in the editor meanwhile.
        edited = made.model_copy(update={"notes": "from the editor"})
        await recipes.update_recipe(
            state, made.path, RecipeIn.model_validate(edited.model_dump()), base_revision=made.revision
        )
        release.set()
        try:
            await update
        except recipes.NoteError as exc:
            return exc.status
        return None

    assert run(scenario()) == 409
    assert run(recipes.get_recipe(state, made.path)).notes == "from the editor"


def test_an_edit_made_while_the_drawing_runs_survives_the_drawing(state, monkeypatch):
    for name in ("openai_api_key", "google_api_key", "anthropic_model", "openai_model", "google_model"):
        setattr(state.settings, name, None)
    state.settings.anthropic_api_key = "sk-ant-x"
    state.settings.ai_provider = "anthropic"
    state.ai_config = {"provider": "anthropic", "keys": {"anthropic": "sk-ant-x"}}
    release = None

    async def slow_draw(_state, recipe):
        await release.wait()
        return Drawing(art=f"[{recipe.title}]")

    monkeypatch.setattr(recipe_ai, "draw", slow_draw)

    async def scenario():
        nonlocal release
        release = asyncio.Event()
        made = await recipes.create_recipe(state, RecipeIn(title="Stew", notes="v1"))
        assert recipe_art.status(state, made.path)["drawing"] is True
        edited = await recipes.update_recipe(
            state, made.path, RecipeIn(title="Beef Stew", notes="v2"), base_revision=made.revision
        )
        release.set()
        while recipe_art._jobs(state).running:
            await asyncio.gather(*recipe_art._jobs(state).running.values(), return_exceptions=True)
        return edited

    edited = run(scenario())
    final = run(recipes.get_recipe(state, edited.path))
    assert final.title == "Beef Stew" and final.notes == "v2"
    # Stored on what is there now, whichever version it was drawn from.
    assert final.art in ("[Stew]", "[Beef Stew]")
    assert final.modified == edited.modified


# -- OMNIA ---------------------------------------------------------------------------


class RecordingProvider(FakeProvider):
    def __init__(self, script):
        super().__init__(script)
        self.tool_sets: list[set[str]] = []

    async def chat(self, system, messages, tools, on_text=None, allow_tools=True, context=None):
        self.tool_sets.append({t.name for t in tools})
        return await super().chat(system, messages, tools, on_text, allow_tools, context)


def test_a_marked_note_outside_the_recipes_folder_is_not_this_recipe(acting_state):
    """The open note is "this recipe" only when the recipe routes would serve
    it; a `recipe: 1` note elsewhere answers 404 to get_recipe."""
    acting_state.timezone = "UTC"
    content = recipes.build_recipe_content(RecipeIn(title="Pie"))
    acting_state.store.create("Notes/Pie.md", content, check_name=False)
    provider = RecordingProvider([ProviderReply(text="ok", stop_reason="end_turn")])
    run(Overseer(provider, acting_state).chat("what's in this?", context_note="Notes/Pie.md"))
    assert "has the recipe" not in (provider.contexts[0] or "")
    assert not RECIPE_TOOLS <= provider.tool_sets[0]


def _tool(state, name: str, inp: dict) -> dict:
    return json.loads(run(execute_tool(state, "t", name, inp)))


def test_omnia_imports_an_http_link_over_https_as_the_add_box_does(acting_state, monkeypatch):
    fetched: list[str] = []
    page = recipe_import.FetchedPage(
        blocks=[{"@type": "Recipe", "name": "Lemon Drizzle", "recipeIngredient": ["2 lemons"]}],
        text="Lemon Drizzle",
        url="https://example.com/lemon",
        title="Lemon Drizzle",
    )

    async def fake_fetch(url):
        fetched.append(url)
        return page

    monkeypatch.setattr(recipe_import, "fetch", fake_fetch)
    monkeypatch.setattr(tx, "_user_wrote_link", lambda *_a: True)
    out = _tool(acting_state, "import_recipe", {"url": "http://example.com/lemon"})
    assert out["draft"]["title"] == "Lemon Drizzle", out
    assert fetched == ["https://example.com/lemon"]


def test_omnia_parses_pasted_text_off_the_event_loop_and_cut_as_the_add_box_does(acting_state, monkeypatch):
    """The local parser matches every ingredient against every step; the add
    box cuts a paste and runs it in a thread so one paste cannot stall every
    other request. The tool now does the same."""
    seen: list[int] = []
    real = recipe_parse.parse_text

    def recording(text):
        seen.append(len(text))
        time.sleep(0.3)
        return real(text)

    monkeypatch.setattr(recipe_parse, "parse_text", recording)
    huge = "Ingredients\n" + "\n".join(f"{i} g item{i}" for i in range(20_000))

    async def scenario():
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1

        beat = asyncio.create_task(ticker())
        out = json.loads(await execute_tool(acting_state, "t", "import_recipe", {"text": huge}))
        beat.cancel()
        return out, ticks

    out, ticks = run(scenario())
    assert out["draft"]["ingredients"], out
    assert seen and max(seen) <= 50_000
    assert any("was cut" in line for line in out["missing"])
    # The loop kept turning while the parser ran.
    assert ticks >= 5


# -- the store's own writes ------------------------------------------------------------


def test_the_revision_a_save_answers_is_the_one_the_next_save_matches(state):
    """Notes are read with universal newlines, so a body written with CR line
    breaks read back as different text: the revision create and update
    answered never matched again and the next save was a 409."""
    made = run(recipes.create_recipe(state, RecipeIn(title="Pie", notes="Roll it.\r\nBake it.\rEat it.")))
    assert made.revision == run(recipes.get_recipe(state, made.path)).revision
    assert made.notes == "Roll it.\nBake it.\nEat it."
    updated = run(
        recipes.update_recipe(
            state, made.path, RecipeIn(title="Pie", notes="Cool it.\r\nSlice it."), base_revision=made.revision
        )
    )
    again = run(
        recipes.update_recipe(state, made.path, RecipeIn(title="Pie", notes="Serve it."), base_revision=updated.revision)
    )
    assert again.notes == "Serve it."
    assert again.revision == content_hash(state.store.read(made.path))


def test_a_drawing_stored_on_a_hand_edited_note_keeps_every_user_line(state):
    content = (
        "---\n"
        "ingredients:\n- id: a\n  item: apple\n  amount: 2.0\n  unit: ''\n"
        "# a comment between keys\n"
        "title: Pie\n"
        "zip: 01234\n"
        "recipe: 1\n"
        "steps: []\n"
        "created: '2026-10-01T00:00:00Z'\n"
        "modified: '2026-10-01T00:00:00Z'\n"
        "---\n\nBody.\n"
    )
    # Written as an editor outside the app would leave it.
    (state.settings.vault_dir / "recipes").mkdir()
    (state.settings.vault_dir / "recipes" / "Pie.md").write_text(content, encoding="utf-8")
    run(state.indexer.index_note("recipes/Pie.md", content))
    stored = run(recipes.store_art(state, "recipes/Pie.md", Drawing(art="ab\ncd")))
    on_disk = state.store.read("recipes/Pie.md")
    head = content.split("\n---\n")[0]
    assert on_disk.startswith(head + "\n")
    assert stored.art == "ab\ncd" and stored.notes == "Body."
    assert stored.modified == "2026-10-01T00:00:00Z"
    assert stored.revision == content_hash(on_disk)
    assert fm.parse(on_disk)[0]["art"] == ["ab", "cd"]


def test_omnia_commits_a_downloaded_thumbnail_with_its_recipe_so_undo_takes_both(acting_state, monkeypatch):
    """As log_media does with a cover: the image the save pulled into
    attachments/ rides in the action's scoped commit. Left out, it was swept
    into the next autosave under the owner's name and Undo left it behind."""
    acting_state.timezone = "UTC"
    acting_state.settings.timezone = "UTC"
    names = iter(["attachments/pie-0001.jpg", "attachments/pie-0002.jpg"])

    async def fake_ingest(state, url, name_hint="cover", **_kw):
        rel = next(names)
        target = state.store.resolve_attachment(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"jpeg")
        return {"path": rel, "url": f"/api/{rel}"}

    monkeypatch.setattr(media_lookup, "ingest_cover", fake_ingest)
    monkeypatch.setattr(tx, "_OFFERED_IMAGE_URLS", {})
    tx._offer_image_urls("t", ["https://img.example.com/pie.jpg", "https://img.example.com/pie2.jpg"])
    vault = acting_state.settings.vault_dir

    made = _tool(acting_state, "create_recipe", {"recipe": {
        "title": "Pie", "source": {"type": "website", "url": "https://example.com/pie",
                                   "thumbnail": "https://img.example.com/pie.jpg"},
    }})
    assert "attachments/pie-0001.jpg" in made["_action"]["paths"], made
    assert acting_state.vault.repo.git.status("--porcelain", "--", "attachments", "recipes") == ""

    current = run(recipes.get_recipe(acting_state, made["path"]))
    edited = _tool(acting_state, "update_recipe", {
        "path": made["path"], "base_revision": current.revision,
        "recipe": {"source": {"type": "website", "url": "https://example.com/pie",
                              "thumbnail": "https://img.example.com/pie2.jpg"}},
    })
    assert "attachments/pie-0002.jpg" in edited["_action"]["paths"], edited
    assert acting_state.vault.repo.git.status("--porcelain", "--", "attachments", "recipes") == ""

    run(acting_state.actions.undo(edited["_action"]["id"], "t"))
    assert not (vault / "attachments/pie-0002.jpg").exists()
    run(acting_state.actions.undo(made["_action"]["id"], "t"))
    assert not (vault / "attachments/pie-0001.jpg").exists()
    assert not (vault / made["path"]).exists()


def test_every_write_touches_the_autocommitter(state):
    """The brief's promise for every write, which no test held: without the
    touch a change waits for some other save to be committed."""
    touches: list[str] = []
    state.autocommit = type("Touch", (), {"touch": lambda self, min_delay=None: touches.append("t")})()

    def count(coro) -> int:
        before = len(touches)
        run(coro)
        return len(touches) - before

    made = run(recipes.create_recipe(state, RecipeIn(title="Pie")))
    assert touches
    assert count(recipes.update_recipe(state, made.path, RecipeIn(title="Tart"), base_revision=None)) >= 1
    assert count(recipes.store_art(state, made.path, Drawing(art="()"))) >= 1
    assert count(recipes.set_order(state, [made.path])) >= 1
    assert count(recipes.delete_recipe(state, made.path)) >= 1
