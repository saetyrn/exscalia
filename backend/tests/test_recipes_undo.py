"""Undo of OMNIA's recipe writes once the drawing has landed.

A new recipe is drawn in the background (recipe_art) and the drawing lands in
the same note's frontmatter seconds after OMNIA's action recorded it. Undo
must look past that drawing, and only that drawing: any other change since
the action still refuses the undo, for recipes and for every other note.
"""
import asyncio
import json

import pytest

from app import media_theme, recipe_ai, recipe_art, recipes
from app.overseer.agent import Overseer
from app.overseer.provider import FakeProvider
from app.overseer.tools import execute_tool
from app.recipe_models import Drawing, RecipeIn
from app.vault import fm
from app.vault.notes import NoteError
from tests.test_recipes_omnia import calls, carbonara, done

DRAWING = Drawing(
    art=" (__) \n/____\\",
    art_colors=" aaaa \nbbbbbb",
    art_palette={"a": "#e8c070", "b": "#7fbf6a"},
)
CONFLICT = "Undo conflicts with newer vault changes"

OATS = {
    "title": "Overnight Oats",
    "base_servings": 1,
    "ingredients": [
        {"id": "oats", "amount": 50, "unit": "g", "item": "rolled oats"},
        {"id": "milk", "amount": 120, "unit": "ml", "item": "milk"},
    ],
    "steps": [{"text": "Stir {oats} into {milk}; chill overnight."}],
    "source": {"type": "typed"},
}


class Painter:
    """Stands in for recipe_ai.draw. While `gate` is set and not yet open,
    every drawing waits for it."""

    def __init__(self):
        self.gate: asyncio.Event | None = None
        self.calls = 0

    async def __call__(self, state, recipe):
        self.calls += 1
        if self.gate is not None:
            await self.gate.wait()
        return DRAWING


@pytest.fixture
def kitchen(acting_state):
    acting_state.timezone = "UTC"
    acting_state.settings.timezone = "UTC"
    return acting_state


@pytest.fixture
def painter(kitchen, monkeypatch):
    paint = Painter()
    monkeypatch.setattr(recipe_ai, "available", lambda state: True)
    monkeypatch.setattr(recipe_ai, "draw", paint)
    return paint


async def settle(state) -> None:
    """Wait for every drawing of this state to finish."""
    jobs = recipe_art._jobs(state)
    while jobs.running:
        await asyncio.gather(*list(jobs.running.values()), return_exceptions=True)


async def omnia(state, *script) -> tuple[str, dict]:
    """One scripted turn; (session, the turn's one action)."""
    result = await Overseer(FakeProvider([*script, done()]), state).chat(
        "do it", ui_context={"route": "/recipes"}
    )
    assert all(item["ok"] for item in result["trace"]), result["trace"]
    [action] = result["actions"]
    return result["session_id"], action


async def omnia_create(state, painter: Painter, recipe: dict) -> tuple[str, dict]:
    """OMNIA adds a recipe; its drawing lands after the action is recorded,
    as a real one does seconds later, unless the caller holds `painter.gate`."""
    hold = painter.gate is not None
    if not hold:
        painter.gate = asyncio.Event()
    session, action = await omnia(state, calls(("create_recipe", {"recipe": recipe})))
    if not hold:
        painter.gate.set()
        await settle(state)
    return session, action


async def omnia_update(state, seeded, changes: dict) -> tuple[str, dict]:
    return await omnia(
        state,
        calls(("update_recipe", {"path": seeded.path, "base_revision": seeded.revision, "recipe": changes})),
    )


def status(state, path: str) -> str:
    """`git status` for one path: '' when the working tree matches HEAD."""
    return state.vault.repo.git.status("--porcelain", "--", path)


def recipe_rows(state) -> list[str]:
    rows = asyncio.run(state.db.execute_fetchall("SELECT path FROM recipes ORDER BY path"))
    return [row["path"] for row in rows]


def assert_undo_commit(state, action: dict) -> None:
    """HEAD is OMNIA's undo, and the undo touched nothing but the recipe."""
    head = state.vault.repo.head.commit
    assert head.author.name == "OMNIA"
    assert head.message == f"Undo OMNIA action: {action['summary']}"
    assert sorted(head.stats.files) == action["paths"]


# --- the drawing is set aside -----------------------------------------------------------


@pytest.mark.parametrize("committed", [False, True], ids=["drawing-unsaved", "drawing-autosaved"])
def test_undo_of_a_create_removes_the_recipe_after_its_drawing_landed(kitchen, painter, committed):
    async def scenario():
        session, action = await omnia_create(kitchen, painter, OATS)
        [path] = action["paths"]
        # OMNIA's commit holds the recipe without the drawing.
        assert "art:" not in kitchen.vault.commit_content_sync(action["commit"], path)
        assert (await recipes.get_recipe(kitchen, path)).art == DRAWING.art
        if committed:
            await kitchen.vault.commit_all("autosave")
        undone = await kitchen.actions.undo(action["id"], session)
        return path, action, undone

    path, action, undone = asyncio.run(scenario())
    assert undone["ok"] is True and undone["skipped"] == []
    assert not kitchen.store.exists(path)
    assert recipe_rows(kitchen) == []
    assert status(kitchen, path) == ""
    assert_undo_commit(kitchen, action)
    assert kitchen.vault.head_content_sync(path) is None
    # As a revert leaves it, the folder the only recipe was in goes too.
    assert "recipes" not in kitchen.store.list_folders()


def test_a_drawing_still_running_when_the_create_is_undone_ends_quietly(kitchen, painter):
    async def scenario():
        painter.gate = asyncio.Event()
        session, action = await omnia_create(kitchen, painter, OATS)
        [path] = action["paths"]
        assert recipe_art.status(kitchen, path)["drawing"] is True
        await kitchen.actions.undo(action["id"], session)
        painter.gate.set()
        await settle(kitchen)
        return path, action

    path, action = asyncio.run(scenario())
    assert painter.calls == 1
    assert recipe_art.status(kitchen, path) == {"drawing": False, "error": None}
    assert not kitchen.store.exists(path)
    assert recipe_rows(kitchen) == []
    assert status(kitchen, path) == ""
    assert_undo_commit(kitchen, action)


@pytest.mark.parametrize("committed", [False, True], ids=["drawing-unsaved", "drawing-autosaved"])
def test_undo_of_an_update_made_before_the_drawing_landed_keeps_the_drawing(kitchen, painter, committed):
    async def scenario():
        # Created in the app; its drawing is still being made when OMNIA edits it.
        painter.gate = asyncio.Event()
        seeded = await recipes.create_recipe(kitchen, carbonara())
        await kitchen.vault.commit_all("seed")
        before = kitchen.store.read(seeded.path)
        steps = [s.model_dump() for s in seeded.steps]
        steps[-1]["text"] = "Whisk {eggs} with {pecorino}, then {pepper}; toss off the heat."
        session, action = await omnia_update(kitchen, seeded, {"title": "Carbonara", "steps": steps})
        painter.gate.set()
        await settle(kitchen)
        drawn = kitchen.store.read(seeded.path)
        assert recipes.recipe_from_content(seeded.path, drawn).art == DRAWING.art
        if committed:
            await kitchen.vault.commit_all("autosave")
        undone = await kitchen.actions.undo(action["id"], session)
        return seeded, before, drawn, action, undone

    seeded, before, drawn, action, undone = asyncio.run(scenario())
    assert undone["ok"] is True and undone["skipped"] == []
    content = kitchen.store.read(seeded.path)
    # The recipe as it was before OMNIA's edit, wearing the drawing made since.
    assert content == recipes.apply_art(before, DRAWING)
    restored = recipes.recipe_from_content(seeded.path, content)
    assert restored.title == "Spaghetti Carbonara" and restored.steps == seeded.steps
    assert restored.modified == seeded.modified
    assert (restored.art, restored.art_colors, restored.art_palette) == (
        DRAWING.art, DRAWING.art_colors, DRAWING.art_palette,
    )
    # The index follows.
    rows = asyncio.run(kitchen.db.execute_fetchall("SELECT title FROM recipes"))
    assert [row["title"] for row in rows] == ["Spaghetti Carbonara"]
    # The drawing has its own commit and the undo reverts only OMNIA's edit.
    assert status(kitchen, seeded.path) == ""
    assert_undo_commit(kitchen, action)
    assert kitchen.vault.commit_content_sync("HEAD^", seeded.path) == drawn
    assert kitchen.vault.head_content_sync(seeded.path) == content


# --- anything else still refuses ---------------------------------------------------------


def owner_edits_notes(state, path: str) -> None:
    current = asyncio.run(recipes.get_recipe(state, path))
    changed = RecipeIn(**{**current.model_dump(include=set(RecipeIn.model_fields)), "notes": "Mine now."})
    asyncio.run(recipes.update_recipe(state, path, changed, base_revision=current.revision))


def owner_adds_a_key(state, path: str) -> None:
    meta, body = fm.parse(state.store.read(path))
    meta["rating"] = 5
    state.store.write(path, fm.serialize(meta, body), preserve_modified=True)


@pytest.mark.parametrize("committed", [False, True], ids=["edit-unsaved", "edit-autosaved"])
@pytest.mark.parametrize("edit", [owner_edits_notes, owner_adds_a_key], ids=["notes", "own-key"])
def test_an_owner_edit_after_a_create_still_refuses_the_undo(kitchen, painter, edit, committed):
    async def scenario():
        return await omnia_create(kitchen, painter, OATS)

    session, action = asyncio.run(scenario())
    [path] = action["paths"]
    edit(kitchen, path)
    if committed:
        asyncio.run(kitchen.vault.commit_all("autosave"))
    edited = kitchen.store.read(path)
    head = kitchen.vault.repo.head.commit.hexsha

    with pytest.raises(NoteError) as refused:
        asyncio.run(kitchen.actions.undo(action["id"], session))
    assert refused.value.status == 409 and CONFLICT in refused.value.detail
    assert kitchen.store.read(path) == edited
    assert kitchen.vault.repo.head.commit.hexsha == head
    assert kitchen.actions.get(action["id"])["undone_at"] is None


@pytest.mark.parametrize("committed", [False, True], ids=["edit-unsaved", "edit-autosaved"])
def test_an_owner_edit_after_an_update_still_refuses_the_undo(kitchen, painter, committed):
    async def scenario():
        painter.gate = asyncio.Event()
        seeded = await recipes.create_recipe(kitchen, carbonara())
        await kitchen.vault.commit_all("seed")
        steps = [s.model_dump() for s in seeded.steps]
        steps[-1]["text"] = "Whisk {eggs} with {pecorino}, then {pepper}."
        session, action = await omnia_update(kitchen, seeded, {"steps": steps})
        painter.gate.set()
        await settle(kitchen)
        # The owner rewrites the same step in the form.
        current = await recipes.get_recipe(kitchen, seeded.path)
        mine = [s.model_copy() for s in current.steps]
        mine[-1] = mine[-1].model_copy(update={"text": "Whisk {eggs} and {pecorino} with {pepper}."})
        await recipes.update_recipe(
            kitchen, seeded.path, RecipeIn(**{**current.model_dump(include=set(RecipeIn.model_fields)), "steps": mine}),
            base_revision=current.revision,
        )
        if committed:
            await kitchen.vault.commit_all("autosave")
        return seeded, session, action

    seeded, session, action = asyncio.run(scenario())
    edited = kitchen.store.read(seeded.path)
    head = kitchen.vault.repo.head.commit.hexsha

    with pytest.raises(NoteError) as refused:
        asyncio.run(kitchen.actions.undo(action["id"], session))
    assert refused.value.status == 409 and CONFLICT in refused.value.detail
    assert kitchen.store.read(seeded.path) == edited
    assert kitchen.vault.repo.head.commit.hexsha == head


# --- other notes are as they were -----------------------------------------------------------


def background_drawing(state, path: str) -> None:
    """What store_art writes, written into a note that is not a recipe."""
    state.store.write(path, recipes.apply_art(state.store.read(path), DRAWING), preserve_modified=True)


def background_backdrop(state, path: str) -> None:
    """What media_theme.enrich_item writes."""
    meta, body = fm.parse(state.store.read(path))
    meta["backdrop"] = "/api/attachments/backdrop.jpg"
    state.store.write(path, fm.serialize(meta, body))


@pytest.mark.parametrize(
    "path,content",
    [
        ("Pie.md", "A plain note.\n"),
        # A note in recipes/ without the marker, and the marker outside
        # recipes/, are ordinary notes.
        ("recipes/Pie.md", "A plain note.\n"),
        ("Notes/Pie.md", recipes.build_recipe_content(RecipeIn(title="Pie"))),
    ],
    ids=["plain", "unmarked-in-recipes", "marked-outside-recipes"],
)
def test_an_ordinary_note_still_refuses_after_a_background_write(kitchen, path, content):
    out = json.loads(
        asyncio.run(execute_tool(kitchen, "t", "create_note", {"path": path, "content": content}))
    )
    action = out["_action"]
    background_drawing(kitchen, path)
    written = kitchen.store.read(path)

    with pytest.raises(NoteError) as refused:
        asyncio.run(kitchen.actions.undo(action["id"], "t"))
    assert refused.value.status == 409 and CONFLICT in refused.value.detail
    assert kitchen.store.read(path) == written


@pytest.mark.parametrize("write", [background_backdrop, background_drawing], ids=["backdrop", "drawing"])
def test_a_media_note_still_refuses_after_a_background_write(kitchen, monkeypatch, write):
    monkeypatch.setattr(media_theme, "enrich_soon", lambda state, path: None)
    out = json.loads(
        asyncio.run(execute_tool(kitchen, "t", "log_media", {"title": "Heat", "media_type": "movie"}))
    )
    assert "_action" in out, out
    action = out["_action"]
    [path] = action["paths"]
    write(kitchen, path)
    asyncio.run(kitchen.vault.commit_all("autosave"))
    written = kitchen.store.read(path)

    with pytest.raises(NoteError) as refused:
        asyncio.run(kitchen.actions.undo(action["id"], "t"))
    assert refused.value.status == 409 and CONFLICT in refused.value.detail
    assert kitchen.store.read(path) == written
