"""Drawings of the dish, made in the background.

A new recipe with no art gets one (`recipes.create_recipe` asks for it, so
every creator does without knowing), and the recipe page can ask for a
redraw. Each drawing is one `recipe_ai.draw` call stored with
`recipes.store_art`: it may take seconds at low effort or minutes at max, so
the request that starts it answers at once and the page polls `status`.

One drawing per recipe at a time, a few recipes side by side. The last
failure per recipe is kept, as one short sentence, until the next attempt.
Nothing here may take the server down: a job that cannot run does nothing,
and a job that fails says so in `status` and the log.
"""
import asyncio
import logging

from . import recipe_ai, recipes
from .vault.notes import NoteError

log = logging.getLogger(__name__)

# Drawings that run at once; more wait their turn and still count as drawing.
DRAW_CONCURRENCY = 3

FAILED = "The drawing failed."

# Every job of every app on this process, for shutdown. Strong refs keep the
# tasks alive.
_TASKS: set[asyncio.Task] = set()


class _Jobs:
    """Per app state: the running job per recipe path, the last failure per
    path, and the gate. Created on first use, so a Semaphore that has made a
    caller wait stays on the loop it was made for."""

    def __init__(self) -> None:
        self.running: dict[str, asyncio.Task] = {}
        self.errors: dict[str, str] = {}
        self.gate = asyncio.Semaphore(DRAW_CONCURRENCY)


def _jobs(state) -> _Jobs:
    jobs = getattr(state, "recipe_drawings", None)
    if jobs is None:
        jobs = state.recipe_drawings = _Jobs()
    return jobs


def _can_draw(state) -> bool:
    stopping = getattr(state, "shutting_down", None)
    if stopping is not None and stopping.is_set():
        return False
    try:
        return recipe_ai.available(state)
    except Exception:
        log.warning("could not tell whether recipe drawings can run", exc_info=True)
        return False


async def _draw(state, path: str, force: bool) -> None:
    jobs = _jobs(state)
    try:
        async with jobs.gate:
            try:
                recipe = await recipes.get_recipe(state, path)
            except NoteError:
                return  # deleted, or no longer a recipe, while it waited
            if recipe.art and not force:
                return
            drawing = await recipe_ai.draw(state, recipe)
            await recipes.store_art(state, path, drawing, only_if_empty=not force)
    except recipe_ai.RecipeAIError as exc:
        jobs.errors[path] = str(exc)
        log.warning("recipe drawing for %s failed: %s", path, exc)
    except Exception:
        jobs.errors[path] = FAILED
        log.warning("recipe drawing for %s failed", path, exc_info=True)


def draw_soon(state, path: str, *, force: bool = False) -> bool:
    """Start drawing one recipe in the background; True when a drawing is now
    in progress for it. A second call while one runs starts nothing more.
    `force` replaces a drawing already there; without it a recipe that has
    one is left alone. Does nothing without an AI provider."""
    if not _can_draw(state):
        return False
    jobs = _jobs(state)
    if path in jobs.running:
        return True
    jobs.errors.pop(path, None)
    task = asyncio.create_task(_draw(state, path, force))
    jobs.running[path] = task

    def done(finished: asyncio.Task) -> None:
        # Here rather than in _draw: a job cancelled before it starts never
        # runs its own cleanup.
        if jobs.running.get(path) is finished:
            del jobs.running[path]
        _TASKS.discard(finished)

    _TASKS.add(task)
    task.add_done_callback(done)
    return True


def status(state, path: str) -> dict:
    """Whether a drawing is running or waiting for this recipe, and the last
    one's failure, if it failed."""
    jobs = _jobs(state)
    return {"drawing": path in jobs.running, "error": jobs.errors.get(path)}


async def shutdown() -> None:
    """Cancel every queued or running drawing; the app calls this before it
    closes the database the jobs write through."""
    loop = asyncio.get_running_loop()
    tasks = [task for task in _TASKS if task.get_loop() is loop]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
