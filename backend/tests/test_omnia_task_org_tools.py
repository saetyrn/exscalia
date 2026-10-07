"""OMNIA Round 2 — task-by-text and organization conveniences."""
import asyncio

import pytest

from app.overseer import tool_expansion
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
from app.vault.notes import NoteError

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


def test_complete_task_with_done_never_reopens(acting_state):
    """A repeated or late "check off X" must not flip a done task back."""
    task = make_task(acting_state, "file taxes")
    asyncio.run(complete_task(acting_state, "t", {"text": "file taxes", "done": True}))

    again = asyncio.run(complete_task(acting_state, "t", {"text": "file taxes", "done": True}))

    assert again["already_done"] is True
    assert "_action" not in again
    assert asyncio.run(status_of(acting_state, task["id"])) == "done"


def test_complete_task_done_false_reopens_the_done_match(acting_state):
    tuesday = make_task(acting_state, "call mom tuesday")
    friday = make_task(acting_state, "call mom friday")
    asyncio.run(complete_task(acting_state, "t", {"text": "call mom tuesday"}))

    result = asyncio.run(complete_task(acting_state, "t", {"text": "call mom", "done": False}))

    assert result["task"]["id"] == tuesday["id"]
    assert asyncio.run(status_of(acting_state, tuesday["id"])) == "todo"
    assert asyncio.run(status_of(acting_state, friday["id"])) == "todo"
    already = asyncio.run(complete_task(acting_state, "t", {"text": "call mom friday", "done": False}))
    assert already["already_open"] is True


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


async def texts_like(state, pattern: str) -> list[str]:
    rows = await state.db.execute_fetchall(
        "SELECT text FROM tasks WHERE text LIKE ? ORDER BY text", (pattern,)
    )
    return [row["text"] for row in rows]


def test_add_tasks_checks_every_due_before_writing(acting_state):
    """A bad due on item 2 used to fail the call with item 1 already written,
    and the model's retry of the whole list duplicated it."""
    items = [{"text": "batch one"}, {"text": "batch two", "due": "next friday"}, {"text": "batch three"}]

    with pytest.raises(NoteError) as err:
        asyncio.run(add_tasks(acting_state, "t", {"tasks": items, "path": INBOX}))

    assert "“batch two”" in err.value.detail
    assert asyncio.run(texts_like(acting_state, "batch %")) == []


def test_add_tasks_failing_part_way_is_one_partial_undoable_action(acting_state, monkeypatch):
    real = tool_expansion.create_task_record
    calls = 0

    async def flaky(state, **fields):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("disk full")
        return await real(state, **fields)

    monkeypatch.setattr(tool_expansion, "create_task_record", flaky)
    items = [{"text": f"item {n}"} for n in range(4)]

    result = asyncio.run(add_tasks(acting_state, "t", {"tasks": items, "path": INBOX}))

    assert result["error"].startswith("Applied 2 of 4 (one undoable action; do not resend")
    assert result["error"].endswith("failed at “item 2”: Tool failed: disk full")
    assert result["failed_at"] == "item 2"
    assert result["_action"]["summary"] == "Added 2 tasks (partial: 2 of 4)"
    assert result["_action"]["paths"] == [INBOX]
    assert asyncio.run(texts_like(acting_state, "item %")) == ["item 0", "item 1"]


def test_a_failure_before_any_write_still_raises(acting_state, monkeypatch):
    async def broken(state, **fields):
        raise NoteError(400, "nope")

    monkeypatch.setattr(tool_expansion, "create_task_record", broken)
    with pytest.raises(NoteError):
        asyncio.run(add_tasks(acting_state, "t", {"tasks": [{"text": "x"}], "path": INBOX}))


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
