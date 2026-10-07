"""Drawings of the dish: the one a new recipe gets, the redraw route, and the
status the recipe page polls. Ported from the recipe app's test_art.py; the
drawing itself is faked except where the scripted provider draws it."""
import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from app import recipe_ai, recipe_art, recipes
from app.api import recipes as recipes_api
from app.overseer.provider import FakeProvider, ProviderReply
from app.overseer.usage import UsageLedger
from app.recipe_ai import RecipeAIError
from app.recipe_models import Drawing, Ingredient, RecipeIn

from conftest import make_client

PAL = {"y": "#e8c26a", "g": "#7fbf6a"}


def ai_off(state) -> None:
    for name in ("anthropic_api_key", "openai_api_key", "google_api_key",
                 "anthropic_model", "openai_model", "google_model"):
        setattr(state.settings, name, None)
    state.settings.ai_provider = "anthropic"
    state.ai_config = {"provider": "anthropic"}


def ai_on(state) -> None:
    ai_off(state)
    state.settings.anthropic_api_key = "sk-ant-x"
    state.ai_config = {"provider": "anthropic", "keys": {"anthropic": "sk-ant-x"}}


class Painter:
    """Stands in for recipe_ai.draw: counts calls, can hold every drawing
    until released, and draws what `art` says (or raises it)."""

    def __init__(self, art="[{title}]"):
        self.art = art
        self.calls: list[str] = []
        self.running = 0
        self.peak = 0
        self.release: asyncio.Event | None = None

    async def __call__(self, state, recipe):
        self.calls.append(recipe.title)
        self.running += 1
        self.peak = max(self.peak, self.running)
        try:
            if self.release is not None:
                await self.release.wait()
            else:
                await asyncio.sleep(0.01)
            if isinstance(self.art, Exception):
                raise self.art
            return Drawing(art=self.art.format(title=recipe.title), art_colors="", art_palette={})
        finally:
            self.running -= 1


@pytest.fixture
def painter(state, monkeypatch):
    ai_on(state)
    paint = Painter()
    monkeypatch.setattr(recipe_ai, "draw", paint)
    return paint


async def settle(state) -> None:
    """Wait for every drawing of this state to finish."""
    jobs = recipe_art._jobs(state)
    while jobs.running:
        await asyncio.gather(*list(jobs.running.values()), return_exceptions=True)


def run(coro):
    return asyncio.run(coro)


async def create(state, title: str, **kw):
    return await recipes.create_recipe(state, RecipeIn(title=title, **kw))


# -- a new recipe is drawn ---------------------------------------------------------


def test_a_new_recipe_is_drawn_in_the_background(state, painter):
    async def scenario():
        made = await create(state, "Lemon Shrimp")
        assert made.art == ""
        assert recipe_art.status(state, made.path) == {"drawing": True, "error": None}
        await settle(state)
        assert recipe_art.status(state, made.path) == {"drawing": False, "error": None}
        drawn = await recipes.get_recipe(state, made.path)
        assert drawn.art == "[Lemon Shrimp]"
        # Not an edit.
        assert drawn.modified == made.modified
        return made

    made = run(scenario())
    rows = run(state.db.execute_fetchall("SELECT art FROM recipes WHERE path = ?", (made.path,)))
    assert json.loads(rows[0]["art"])["art"] == "[Lemon Shrimp]"


def test_a_recipe_created_with_a_drawing_is_not_redrawn(state, painter):
    async def scenario():
        made = await create(state, "Toast", art="==", art_palette=PAL)
        assert recipe_art.status(state, made.path)["drawing"] is False
        await settle(state)
        return made

    made = run(scenario())
    assert painter.calls == []
    assert run(recipes.get_recipe(state, made.path)).art == "=="


def test_two_new_recipes_draw_at_the_same_time(state, painter):
    async def scenario():
        painter.release = asyncio.Event()
        a = await create(state, "A")
        b = await create(state, "B")
        for _ in range(20):
            if painter.running == 2:
                break
            await asyncio.sleep(0.01)
        assert recipe_art.status(state, a.path)["drawing"] and recipe_art.status(state, b.path)["drawing"]
        painter.release.set()
        await settle(state)

    run(scenario())
    assert painter.peak == 2 and sorted(painter.calls) == ["A", "B"]


def test_more_drawings_than_the_gate_wait_their_turn(state, painter, monkeypatch):
    monkeypatch.setattr(recipe_art, "DRAW_CONCURRENCY", 2)

    async def scenario():
        painter.release = asyncio.Event()
        made = [await create(state, title) for title in ("A", "B", "C")]
        await asyncio.sleep(0.05)
        assert painter.running == 2
        assert all(recipe_art.status(state, m.path)["drawing"] for m in made)
        painter.release.set()
        await settle(state)

    run(scenario())
    assert painter.peak == 2 and sorted(painter.calls) == ["A", "B", "C"]


def test_a_second_request_while_one_runs_starts_nothing(state, painter):
    async def scenario():
        painter.release = asyncio.Event()
        made = await create(state, "Soup")
        await asyncio.sleep(0.01)
        assert recipe_art.draw_soon(state, made.path, force=True) is True
        assert recipe_art.draw_soon(state, made.path) is True
        painter.release.set()
        await settle(state)

    run(scenario())
    assert painter.calls == ["Soup"]


def test_a_forced_redraw_replaces_the_drawing_and_an_unforced_one_does_not(state, painter):
    async def scenario():
        made = await create(state, "Soup")
        await settle(state)
        painter.art = "redrawn {title}"
        assert recipe_art.draw_soon(state, made.path) is True
        await settle(state)
        assert (await recipes.get_recipe(state, made.path)).art == "[Soup]"
        assert recipe_art.draw_soon(state, made.path, force=True) is True
        await settle(state)
        assert (await recipes.get_recipe(state, made.path)).art == "redrawn Soup"

    run(scenario())
    # The unforced request saw the drawing and never asked for another.
    assert painter.calls == ["Soup", "Soup"]


def test_a_failed_drawing_reports_its_message_until_the_next_attempt(state, painter):
    async def scenario():
        made = await create(state, "Soup")
        await settle(state)
        painter.art = RecipeAIError("No drawing came back.")
        recipe_art.draw_soon(state, made.path, force=True)
        await settle(state)
        assert recipe_art.status(state, made.path) == {"drawing": False, "error": "No drawing came back."}
        # The old drawing stays.
        assert (await recipes.get_recipe(state, made.path)).art == "[Soup]"

        painter.release = asyncio.Event()
        painter.art = "again"
        recipe_art.draw_soon(state, made.path, force=True)
        assert recipe_art.status(state, made.path) == {"drawing": True, "error": None}
        painter.release.set()
        await settle(state)
        assert recipe_art.status(state, made.path) == {"drawing": False, "error": None}

        # Anything else that breaks is one plain sentence, and the server goes on.
        painter.art = RuntimeError("database is locked")
        recipe_art.draw_soon(state, made.path, force=True)
        await settle(state)
        assert recipe_art.status(state, made.path)["error"] == "The drawing failed."

    run(scenario())


def test_an_edit_keeps_the_drawing_and_draws_nothing(state, painter):
    async def scenario():
        made = await create(state, "Pancakes", ingredients=[Ingredient(id="flour", amount=1, unit="cup", item="flour")])
        await settle(state)
        drawn = await recipes.get_recipe(state, made.path)
        updated = await recipes.update_recipe(
            state, made.path, RecipeIn(title="Pancakes", notes="fluffier"), base_revision=drawn.revision
        )
        assert recipe_art.status(state, made.path)["drawing"] is False
        await settle(state)
        return updated

    updated = run(scenario())
    assert updated.art == "[Pancakes]" and updated.notes == "fluffier"
    assert painter.calls == ["Pancakes"]


def test_a_recipe_deleted_while_it_is_drawn_is_left_alone(state, painter):
    async def scenario():
        painter.release = asyncio.Event()
        made = await create(state, "Soup")
        await asyncio.sleep(0.01)
        await recipes.delete_recipe(state, made.path)
        painter.release.set()
        await settle(state)
        assert recipe_art.status(state, made.path) == {"drawing": False, "error": None}
        return made

    made = run(scenario())
    assert not state.store.exists(made.path)


def test_without_ai_nothing_is_drawn(state, monkeypatch):
    ai_off(state)
    paint = Painter()
    monkeypatch.setattr(recipe_ai, "draw", paint)

    async def scenario():
        made = await create(state, "Soup")
        assert recipe_art.draw_soon(state, made.path, force=True) is False
        assert recipe_art.status(state, made.path) == {"drawing": False, "error": None}
        await settle(state)

    run(scenario())
    assert paint.calls == []


def test_a_broken_ai_check_never_fails_a_create(state, monkeypatch):
    def broken(_state):
        raise RuntimeError("settings unreadable")

    monkeypatch.setattr(recipe_ai, "available", broken)
    made = run(create(state, "Soup"))
    assert state.store.exists(made.path)
    assert recipe_art.status(state, made.path)["drawing"] is False


def test_shutdown_cancels_running_drawings(state, painter):
    async def scenario():
        painter.release = asyncio.Event()  # never set
        made = await create(state, "Soup")
        await asyncio.sleep(0.01)
        assert painter.running == 1
        await recipe_art.shutdown()
        assert painter.running == 0
        assert recipe_art.status(state, made.path) == {"drawing": False, "error": None}
        # Once the app is stopping, nothing new starts.
        state.shutting_down = asyncio.Event()
        state.shutting_down.set()
        assert recipe_art.draw_soon(state, made.path, force=True) is False

    run(scenario())
    assert recipe_art._TASKS == set()


# -- the routes --------------------------------------------------------------------


def wait_until_drawn(client, path, timeout=5.0) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        status = client.get(f"/api/recipes/art/{path}").json()
        if not status["drawing"]:
            return status
        time.sleep(0.01)
    raise AssertionError("the drawing never finished")


def test_the_redraw_route_and_status(state, painter):
    with TestClient(make_client(state, recipes_api.router).app) as client:
        made = client.post("/api/recipes", json=RecipeIn(title="Lemon Shrimp").model_dump()).json()
        assert wait_until_drawn(client, made["path"]) == {"drawing": False, "error": None}
        assert client.get(f"/api/recipes/item/{made['path']}").json()["art"] == "[Lemon Shrimp]"

        painter.art = "redrawn"
        resp = client.post(f"/api/recipes/art/{made['path']}")
        assert resp.status_code == 202 and resp.json() == {"drawing": True}
        assert wait_until_drawn(client, made["path"]) == {"drawing": False, "error": None}
        assert client.get(f"/api/recipes/item/{made['path']}").json()["art"] == "redrawn"

        painter.art = RecipeAIError("No drawing came back.")
        assert client.post(f"/api/recipes/art/{made['path']}").status_code == 202
        assert wait_until_drawn(client, made["path"]) == {"drawing": False, "error": "No drawing came back."}
        assert client.get(f"/api/recipes/item/{made['path']}").json()["art"] == "redrawn"


def test_the_art_routes_404_anything_that_is_no_recipe(state, painter):
    client = make_client(state, recipes_api.router)
    content = recipes.build_recipe_content(RecipeIn(title="Pie"))
    run(state.indexer.index_note("Notes/Pie.md", state.store.create("Notes/Pie.md", content)))
    for path in ("recipes/Missing.md", "Notes/Pie.md"):
        assert client.post(f"/api/recipes/art/{path}").status_code == 404
        assert client.get(f"/api/recipes/art/{path}").status_code == 404
    assert painter.calls == []


def test_the_redraw_route_needs_the_ai(state):
    ai_off(state)
    client = make_client(state, recipes_api.router)
    made = client.post("/api/recipes", json=RecipeIn(title="Soup").model_dump()).json()
    resp = client.post(f"/api/recipes/art/{made['path']}")
    assert resp.status_code == 503 and resp.json()["detail"] == "No AI provider is configured."
    assert client.get(f"/api/recipes/art/{made['path']}").json() == {"drawing": False, "error": None}


# -- through the real AI step ------------------------------------------------------


def scripted(monkeypatch, *texts):
    provider = FakeProvider(
        [ProviderReply(text=t, stop_reason="end_turn", usage={"input": 10, "output": 5}) for t in texts]
    )
    built: dict = {}

    def build(settings, **kwargs):
        built.update(kwargs)
        return provider

    monkeypatch.setattr(recipe_ai, "build_provider", build)
    return provider, built


ART = {
    "palette": [{"key": "y", "color": "#e8c26a", "name": "pancake gold"}],
    "rows": ["  ______", " (======)"],
    "colors": ["  yyyyyy", " yyyyyyyy"],
}


def test_the_scripted_provider_draws_a_new_recipe_at_low_effort(state, monkeypatch):
    ai_on(state)
    state.usage = UsageLedger(state.settings.vault_dir)
    provider, built = scripted(monkeypatch, json.dumps(ART))

    async def scenario():
        made = await create(state, "Pancakes")
        await settle(state)
        return await recipes.get_recipe(state, made.path)

    drawn = run(scenario())
    assert drawn.art == " ______\n(======)"
    assert drawn.art_palette == {"y": "#e8c26a"}
    assert built["reasoning"] == "low"
    assert "Dish: Pancakes" in provider.seen[0][0]["content"]


def test_a_drawing_paused_by_the_budget_says_so(state, monkeypatch):
    ai_on(state)
    state.usage = UsageLedger(state.settings.vault_dir)
    state.ai_config["monthly_token_budget"] = 100
    state.usage.record("chat", {"input": 500, "output": 500})
    provider, _built = scripted(monkeypatch, json.dumps(ART))

    async def scenario():
        made = await create(state, "Pancakes")
        await settle(state)
        return made

    made = run(scenario())
    assert recipe_art.status(state, made.path) == {
        "drawing": False,
        "error": "The monthly token budget has paused drawings.",
    }
    assert provider.seen == []
