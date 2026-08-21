"""OMNIA's project-scale task operations: bulk edits, archiving, roll-over,
and one-shot project scaffolding. Each batches its writes into a single
undoable commit."""
import asyncio

import pytest

from app.event_store import EventStore
from app.overseer.tool_expansion import (
    archive_completed_tasks,
    board_digest,
    bulk_edit_tasks,
    create_recurring_task,
    create_task,
    create_task_board,
    list_tasks,
    roll_over_tasks,
    scaffold_project,
    update_task,
)
from app.vault.notes import NoteError

INBOX = "Tasks Inbox.md"


async def tasks_in(state, path: str) -> list[dict]:
    rows = await state.db.execute_fetchall("SELECT * FROM tasks WHERE path = ?", (path,))
    return [dict(row) for row in rows]


@pytest.fixture
def omnia_state(acting_state):
    acting_state.timezone = "UTC"
    acting_state.events = EventStore(acting_state.settings.vault_dir)
    return acting_state


def add_task(state, text: str, path: str = INBOX, **extra) -> dict:
    return asyncio.run(create_task(state, "t", {"text": text, "path": path, **extra}))["task"]


async def task_row(state, task_id: str) -> dict | None:
    cur = await state.db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
    row = await cur.fetchone()
    return dict(row) if row else None


# --- bulk_edit_tasks -------------------------------------------------------


def test_bulk_edit_sets_status_across_a_filter(omnia_state):
    a = add_task(omnia_state, "review the draft")
    b = add_task(omnia_state, "review the budget")
    add_task(omnia_state, "unrelated")

    result = asyncio.run(
        bulk_edit_tasks(omnia_state, "t", {"text": "review", "set_status": "done"})
    )

    assert result["changed"] == 2
    assert asyncio.run(task_row(omnia_state, a["id"]))["status"] == "done"
    assert asyncio.run(task_row(omnia_state, b["id"]))["status"] == "done"


def test_bulk_edit_requires_a_filter(omnia_state):
    add_task(omnia_state, "loose task")
    with pytest.raises(NoteError):
        asyncio.run(bulk_edit_tasks(omnia_state, "t", {"set_status": "done"}))


def test_bulk_edit_requires_exactly_one_change(omnia_state):
    add_task(omnia_state, "a task")
    with pytest.raises(NoteError):
        asyncio.run(
            bulk_edit_tasks(
                omnia_state, "t", {"path": INBOX, "set_status": "done", "delete": True}
            )
        )


def test_bulk_edit_can_delete(omnia_state):
    a = add_task(omnia_state, "purge me one")
    b = add_task(omnia_state, "purge me two")

    result = asyncio.run(bulk_edit_tasks(omnia_state, "t", {"text": "purge", "delete": True}))

    assert result["changed"] == 2
    assert asyncio.run(task_row(omnia_state, a["id"])) is None
    assert asyncio.run(task_row(omnia_state, b["id"])) is None


def test_bulk_edit_can_move(omnia_state):
    omnia_state.store.create_folder("projects")
    board = asyncio.run(create_task_board(omnia_state, "t", {"folder": "projects"}))["path"]
    a = add_task(omnia_state, "relocate alpha")
    add_task(omnia_state, "stays here")

    result = asyncio.run(
        bulk_edit_tasks(omnia_state, "t", {"text": "relocate", "move_to": board})
    )

    assert result["changed"] == 1
    assert asyncio.run(task_row(omnia_state, a["id"]))["path"] == board


# --- archive_completed_tasks -----------------------------------------------


def test_archive_moves_only_completed_tasks(omnia_state):
    omnia_state.store.create_folder("projects")
    board = asyncio.run(create_task_board(omnia_state, "t", {"folder": "projects"}))["path"]
    done = add_task(omnia_state, "shipped", path=board, status="done")
    open_task = add_task(omnia_state, "still open", path=board)

    result = asyncio.run(archive_completed_tasks(omnia_state, "t", {"path": board}))

    assert result["archived"] == 1
    assert asyncio.run(task_row(omnia_state, done["id"]))["path"] == result["archive_path"]
    assert asyncio.run(task_row(omnia_state, open_task["id"]))["path"] == board


def test_archive_with_nothing_done_is_a_noop(omnia_state):
    add_task(omnia_state, "open task")
    result = asyncio.run(archive_completed_tasks(omnia_state, "t", {"path": INBOX}))
    assert result["archived"] == 0


# --- roll_over_tasks -------------------------------------------------------


def test_roll_over_bumps_overdue_tasks(omnia_state):
    overdue = add_task(omnia_state, "way overdue", due="2000-01-01")
    future = add_task(omnia_state, "later", due="2999-01-01")

    result = asyncio.run(roll_over_tasks(omnia_state, "t", {"to": "2020-06-01"}))

    assert result["rolled"] == 1
    assert asyncio.run(task_row(omnia_state, overdue["id"]))["due"] == "2020-06-01"
    assert asyncio.run(task_row(omnia_state, future["id"]))["due"] == "2999-01-01"


def test_roll_over_with_nothing_overdue(omnia_state):
    add_task(omnia_state, "no due date")
    result = asyncio.run(roll_over_tasks(omnia_state, "t", {"to": "2020-06-01"}))
    assert result["rolled"] == 0


# --- path scope: folders and boards, not just exact note paths --------------
# Regression for "delete all tasks in school" finding 0: the model passes the
# folder or board name it heard, not the board note's path.


def school_board(state) -> str:
    state.store.create_folder("School")
    board = asyncio.run(create_task_board(state, "t", {"folder": "School"}))["path"]
    add_task(state, "ACCT 5000 — MBC 1", path=board)
    add_task(state, "MARK 3000 — SB2", path=board)
    return board


def test_list_tasks_accepts_a_folder_name(omnia_state):
    school_board(omnia_state)
    add_task(omnia_state, "not school")

    listed = asyncio.run(list_tasks(omnia_state, "t", {"path": "School"}))["tasks"]

    assert {t["text"] for t in listed} == {"ACCT 5000 — MBC 1", "MARK 3000 — SB2"}


def test_list_tasks_folder_match_is_case_insensitive(omnia_state):
    school_board(omnia_state)
    listed = asyncio.run(list_tasks(omnia_state, "t", {"path": "school"}))["tasks"]
    assert len(listed) == 2


def test_list_tasks_bare_name_finds_the_note(omnia_state):
    add_task(omnia_state, "inbox task")
    listed = asyncio.run(list_tasks(omnia_state, "t", {"path": "Tasks Inbox"}))["tasks"]
    assert [t["text"] for t in listed] == ["inbox task"]


def test_bulk_delete_accepts_a_folder_name(omnia_state):
    school_board(omnia_state)
    keeper = add_task(omnia_state, "not school")

    result = asyncio.run(
        bulk_edit_tasks(omnia_state, "t", {"path": "School", "delete": True})
    )

    assert result["changed"] == 2
    assert asyncio.run(task_row(omnia_state, keeper["id"])) is not None


def test_board_digest_accepts_a_folder_name(omnia_state):
    board = school_board(omnia_state)
    digest = asyncio.run(board_digest(omnia_state, "t", {"path": "School"}))
    assert digest["board"] == board
    assert digest["open"] == 2


def test_archive_accepts_a_folder_name(omnia_state):
    board = school_board(omnia_state)
    done = add_task(omnia_state, "finished", path=board, status="done")

    result = asyncio.run(archive_completed_tasks(omnia_state, "t", {"path": "School"}))

    assert result["archived"] == 1
    assert asyncio.run(task_row(omnia_state, done["id"]))["path"] == result["archive_path"]


def test_roll_over_scopes_to_a_folder_name(omnia_state):
    board = school_board(omnia_state)
    overdue = add_task(omnia_state, "late homework", path=board, due="2000-01-01")
    elsewhere = add_task(omnia_state, "also late", due="2000-01-01")

    result = asyncio.run(
        roll_over_tasks(omnia_state, "t", {"to": "2020-06-01", "path": "School"})
    )

    assert result["rolled"] == 1
    assert asyncio.run(task_row(omnia_state, overdue["id"]))["due"] == "2020-06-01"
    assert asyncio.run(task_row(omnia_state, elsewhere["id"]))["due"] == "2000-01-01"


# --- empty results explain themselves ---------------------------------------
# The retry reflex: an empty list_tasks must say whether the query or the
# vault is empty, and name where open tasks actually live.


def test_empty_list_tasks_with_a_filter_names_notes_with_open_tasks(omnia_state):
    board = school_board(omnia_state)

    out = asyncio.run(list_tasks(omnia_state, "t", {"path": "Nonexistent"}))

    assert out["tasks"] == []
    assert board in out["note"]
    assert "(2 open)" in out["note"]
    assert 'path="Nonexistent"' in out["note"]


def test_empty_vault_note_says_world_empty(omnia_state):
    # The seeded vault ships a Welcome.md task — clear the table so this
    # exercises the true world-empty branch.
    async def clear():
        await omnia_state.db.execute("DELETE FROM tasks")
        await omnia_state.db.commit()

    asyncio.run(clear())
    out = asyncio.run(list_tasks(omnia_state, "t", {"path": "Anywhere"}))
    assert out["tasks"] == []
    assert out["note"] == "The vault has no open tasks at all."


def test_nonempty_list_tasks_carries_no_note(omnia_state):
    school_board(omnia_state)
    out = asyncio.run(list_tasks(omnia_state, "t", {"path": "School"}))
    assert out["tasks"]
    assert "note" not in out


# --- scaffold_project ------------------------------------------------------


def test_scaffold_creates_folder_board_and_seeds(omnia_state):
    result = asyncio.run(scaffold_project(omnia_state, "t", {"name": "job search"}))

    assert result["folder"] == "job search"
    assert omnia_state.store.exists(result["board"])
    assert "task_board: true" in omnia_state.store.read(result["board"])
    for seed in ("Brief", "Log", "Resources"):
        assert omnia_state.store.exists(f"job search/{seed}.md")


def test_scaffold_custom_seeds_and_parent(omnia_state):
    omnia_state.store.create_folder("work")

    result = asyncio.run(
        scaffold_project(
            omnia_state, "t", {"name": "q3", "parent": "work", "seed_notes": ["Plan"]}
        )
    )

    assert result["folder"] == "work/q3"
    assert omnia_state.store.exists("work/q3/Plan.md")
    assert not omnia_state.store.exists("work/q3/Brief.md")


def test_scaffold_refuses_an_existing_folder(omnia_state):
    omnia_state.store.create_folder("taken")
    with pytest.raises(NoteError):
        asyncio.run(scaffold_project(omnia_state, "t", {"name": "taken"}))


# --- create_recurring_task -------------------------------------------------


def make_recurring(state, text: str, due: str, recurrence: dict) -> dict:
    return asyncio.run(
        create_recurring_task(
            state, "t", {"text": text, "due": due, "path": INBOX, "recurrence": recurrence}
        )
    )["task"]


def complete(state, task_id: str) -> None:
    asyncio.run(update_task(state, "t", {"task_id": task_id, "status": "done"}))


def test_recurring_task_carries_recurrence_in_markdown(omnia_state):
    task = make_recurring(omnia_state, "water plants", "2026-01-01", {"frequency": "daily", "interval": 3})

    assert task["due"] == "2026-01-01"
    assert '"recurrence"' in omnia_state.store.read(INBOX)


def test_completing_a_recurring_task_spawns_the_next(omnia_state):
    task = make_recurring(omnia_state, "water plants", "2026-01-01", {"frequency": "daily", "interval": 3})

    complete(omnia_state, task["id"])

    rows = asyncio.run(tasks_in(omnia_state, INBOX))
    plants = [r for r in rows if r["text"] == "water plants"]
    assert {(r["status"], r["due"]) for r in plants} == {
        ("done", "2026-01-01"),
        ("todo", "2026-01-04"),
    }


def test_monthly_recurrence_advances_by_month(omnia_state):
    task = make_recurring(omnia_state, "pay rent", "2026-01-15", {"frequency": "monthly", "interval": 1})

    complete(omnia_state, task["id"])

    rows = asyncio.run(tasks_in(omnia_state, INBOX))
    assert any(r["status"] == "todo" and r["due"] == "2026-02-15" for r in rows if r["text"] == "pay rent")


def test_recurrence_stops_at_until(omnia_state):
    task = make_recurring(
        omnia_state, "finite", "2026-01-01", {"frequency": "daily", "interval": 1, "until": "2026-01-01"}
    )

    complete(omnia_state, task["id"])

    rows = asyncio.run(tasks_in(omnia_state, INBOX))
    assert not [r for r in rows if r["text"] == "finite" and r["status"] == "todo"]


def test_completing_a_plain_task_does_not_spawn(omnia_state):
    task = add_task(omnia_state, "one off")

    complete(omnia_state, task["id"])

    rows = asyncio.run(tasks_in(omnia_state, INBOX))
    assert len([r for r in rows if r["text"] == "one off"]) == 1
