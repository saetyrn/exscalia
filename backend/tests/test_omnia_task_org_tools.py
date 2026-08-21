"""OMNIA Round 2 — task-by-text and organization conveniences."""
import asyncio

from app.overseer.tool_expansion import (
    CONFIG_PATH,
    _config,
    add_tasks,
    archive_note,
    complete_task,
    create_note,
    create_task,
    move_notes,
    reorder_sidebar,
    reschedule_task,
)
from app.vault.internal import atomic_json

INBOX = "Tasks Inbox.md"


def make_task(state, text: str) -> dict:
    return asyncio.run(create_task(state, "t", {"text": text, "path": INBOX}))["task"]


async def status_of(state, task_id: str) -> str:
    rows = await state.db.execute_fetchall("SELECT status FROM tasks WHERE id = ?", (task_id,))
    return rows[0]["status"]


# --- complete_task / reschedule_task (by text) -----------------------------


def test_complete_task_picks_the_exact_match(acting_state):
    milk = make_task(acting_state, "buy milk")
    shake = make_task(acting_state, "buy milkshake")

    asyncio.run(complete_task(acting_state, "t", {"text": "buy milk"}))

    assert asyncio.run(status_of(acting_state, milk["id"])) == "done"
    assert asyncio.run(status_of(acting_state, shake["id"])) == "todo"


def test_complete_task_reopens_when_already_done(acting_state):
    task = make_task(acting_state, "wash car")
    asyncio.run(complete_task(acting_state, "t", {"text": "wash car"}))
    assert asyncio.run(status_of(acting_state, task["id"])) == "done"

    asyncio.run(complete_task(acting_state, "t", {"text": "wash car"}))
    assert asyncio.run(status_of(acting_state, task["id"])) == "todo"


def test_reschedule_task_by_text(acting_state):
    task = make_task(acting_state, "pay rent")

    asyncio.run(reschedule_task(acting_state, "t", {"text": "pay rent", "due": "2030-01-01"}))

    rows = asyncio.run(acting_state.db.execute_fetchall("SELECT due FROM tasks WHERE id = ?", (task["id"],)))
    assert rows[0]["due"] == "2030-01-01"


# --- add_tasks -------------------------------------------------------------


def test_add_tasks_creates_several(acting_state):
    result = asyncio.run(
        add_tasks(
            acting_state,
            "t",
            {"tasks": [{"text": "one"}, {"text": "two", "due": "2030-02-02"}], "path": INBOX},
        )
    )

    assert len(result["tasks"]) == 2
    rows = asyncio.run(acting_state.db.execute_fetchall("SELECT text FROM tasks WHERE path = ?", (INBOX,)))
    assert {"one", "two"} <= {r["text"] for r in rows}


# --- reorder_sidebar -------------------------------------------------------


def test_reorder_sidebar_splices_and_preserves_unlisted(acting_state):
    atomic_json(acting_state.settings.vault_dir / CONFIG_PATH, {"folder_order": ["a", "x", "y"]})

    asyncio.run(reorder_sidebar(acting_state, "t", {"kind": "folders", "order": ["b", "a"]}))

    # b lands first, a moves up, x/y keep their place — nothing collapses.
    assert _config(acting_state)["folder_order"] == ["b", "a", "x", "y"]


# --- archive_note / move_notes ---------------------------------------------


def test_archive_note(acting_state):
    asyncio.run(create_note(acting_state, "t", {"path": "junk.md", "content": "# Junk"}))

    result = asyncio.run(archive_note(acting_state, "t", {"path": "junk.md"}))

    assert result["path"] == "archive/junk.md"
    assert not acting_state.store.exists("junk.md")
    assert acting_state.store.exists("archive/junk.md")


def test_move_notes_bulk(acting_state):
    acting_state.store.create_folder("dest")
    asyncio.run(create_note(acting_state, "t", {"path": "a.md", "content": "# A"}))
    asyncio.run(create_note(acting_state, "t", {"path": "b.md", "content": "# B"}))

    result = asyncio.run(move_notes(acting_state, "t", {"paths": ["a.md", "b.md"], "folder": "dest"}))

    assert len(result["moved"]) == 2
    assert acting_state.store.exists("dest/a.md")
    assert acting_state.store.exists("dest/b.md")
