"""Dragging a card to a new place in its column pins the whole column's order.

The rank lives in the task's metadata comment, so markdown stays authoritative
and the order survives a reindex — that is the property worth defending here.
"""
import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import tasks as tasks_module
from app.index import parse
from app.task_service import create_task, reorder_tasks
from app.vault import fm
from app.vault.notes import NoteError

BOARD = "Tasks Inbox.md"


def make_client(state) -> TestClient:
    app = FastAPI()
    app.state = state
    app.state.timezone = "America/New_York"
    app.include_router(tasks_module.router)
    return TestClient(app)


async def add(state, text: str, path: str = BOARD, due: str | None = None) -> str:
    task = await create_task(state, text=text, path=path, due=due)
    return task["id"]


def board_order(client, *ids: str) -> list[str]:
    """The board's order, narrowed to the tasks a test created — the seeded
    vault ships its own tasks and they are not what is under test."""
    wanted = set(ids)
    return [t["id"] for t in client.get("/api/tasks").json() if t["id"] in wanted]


def sorts_in_markdown(state, path: str = BOARD) -> dict[str, float | None]:
    content = state.store.read(path)
    _meta, body = fm.parse(content)
    offset = parse.frontmatter_line_count(content, body)
    return {task.id: task.sort for task in parse.extract_tasks(body, offset)}


def test_reorder_pins_the_column_and_survives_a_reindex(state):
    async def go():
        first = await add(state, "first")
        second = await add(state, "second")
        third = await add(state, "third")
        client = make_client(state)

        # Nothing dragged yet: the board's own default, which for tasks with
        # no due date is the order they were appended to the note.
        assert board_order(client, first, second, third) == [first, second, third]

        # Drag "first" to the top.
        await reorder_tasks(state, [first, third, second])
        assert board_order(client, first, second, third) == [first, third, second]

        # The rank is in the file, not just the cache: throw the cache away
        # and rebuild it from markdown alone.
        await state.indexer.sync_all(force=True)
        assert board_order(client, first, second, third) == [first, third, second]

    asyncio.run(go())


def test_rank_is_written_into_the_task_metadata(state):
    async def go():
        one = await add(state, "one")
        two = await add(state, "two")
        await reorder_tasks(state, [two, one])
        ranks = sorts_in_markdown(state)
        assert ranks[two] == 0.0
        assert ranks[one] == 1.0

    asyncio.run(go())


def test_manual_order_beats_due_dates(state):
    async def go():
        soon = await add(state, "soon", due="2026-01-01")
        later = await add(state, "later", due="2030-01-01")
        client = make_client(state)
        # Due date decides while the column is untouched…
        assert board_order(client, soon, later) == [soon, later]
        # …and stops deciding the moment the user says otherwise.
        await reorder_tasks(state, [later, soon])
        assert board_order(client, soon, later) == [later, soon]

    asyncio.run(go())


def test_a_new_task_lands_below_a_hand_ordered_column(state):
    async def go():
        one = await add(state, "one")
        two = await add(state, "two")
        await reorder_tasks(state, [two, one])
        fresh = await add(state, "fresh")
        client = make_client(state)
        # A new task is appended to the note, so it belongs after the cards
        # the user placed by hand — not jumbled into the middle of them.
        assert board_order(client, one, two, fresh) == [two, one, fresh]

    asyncio.run(go())


def test_order_spans_notes_on_the_universal_board(state):
    async def go():
        here = await add(state, "here", path=BOARD)
        there = await add(state, "there", path="Other.md")
        client = make_client(state)
        await reorder_tasks(state, [there, here])
        assert board_order(client, here, there) == [there, here]
        assert sorts_in_markdown(state, "Other.md")[there] == 0.0
        assert sorts_in_markdown(state, BOARD)[here] == 1.0

    asyncio.run(go())


def test_unknown_id_renumbers_nothing(state):
    async def go():
        one = await add(state, "one")
        two = await add(state, "two")
        with pytest.raises(NoteError) as caught:
            await reorder_tasks(state, [two, "no-such-task", one])
        assert caught.value.status == 404
        assert sorts_in_markdown(state) == {one: None, two: None}

    asyncio.run(go())


def test_a_task_missing_from_markdown_aborts_before_any_note_is_written(state):
    """The validate-everything-first pass earns its keep here: the stale task
    is in the *second* note, so a naive write-as-you-go would already have
    renumbered the first one before noticing."""
    async def go():
        one = await add(state, "one", path=BOARD)
        two = await add(state, "two", path=BOARD)
        stale = await add(state, "stale", path="Other.md")

        # Delete the task from the file behind the index's back.
        content = state.store.read("Other.md")
        kept = [line for line in content.splitlines() if "stale" not in line and "exscalia-task" not in line]
        state.store.write("Other.md", "\n".join(kept) + "\n")

        with pytest.raises(NoteError) as caught:
            await reorder_tasks(state, [two, one, stale])
        assert caught.value.status == 409
        # The first note must be untouched.
        assert sorts_in_markdown(state) == {one: None, two: None}

    asyncio.run(go())


def test_duplicate_ids_are_rejected(state):
    async def go():
        one = await add(state, "one")
        with pytest.raises(NoteError) as caught:
            await reorder_tasks(state, [one, one])
        assert caught.value.status == 400

    asyncio.run(go())


@pytest.mark.parametrize("value", ['"first"', "null", "true", '{"a":1}'])
def test_a_hand_edited_sort_that_is_not_a_number_degrades_to_unordered(state, value):
    """The metadata comment is plain text in the user's own vault; a garbage
    sort must not take indexing down with it."""
    async def go():
        task_id = await add(state, "one")
        content = state.store.read(BOARD)
        patched = content.replace('"reopened_at"', f'"sort":{value},"reopened_at"')
        assert patched != content
        state.store.write(BOARD, patched)
        await state.indexer.sync_all(force=True)
        assert sorts_in_markdown(state)[task_id] is None
        client = make_client(state)
        assert board_order(client, task_id) == [task_id]

    asyncio.run(go())
