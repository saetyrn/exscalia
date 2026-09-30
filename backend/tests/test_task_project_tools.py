"""OMNIA's project-scale task operations: bulk edits, archiving, roll-over,
and one-shot project scaffolding. Each batches its writes into a single
undoable commit."""
import asyncio
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from app import alerts
from app.event_store import EventStore
from app.index import parse
from app.overseer import tool_expansion
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
    set_task_reminder,
    update_task,
)
from app.task_service import _next_occurrence, delete_task as delete_task_record, parse_user_date
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


def test_archive_rewrites_the_board_once(omnia_state, monkeypatch):
    omnia_state.store.create_folder("projects")
    board = asyncio.run(create_task_board(omnia_state, "t", {"folder": "projects"}))["path"]
    done = [add_task(omnia_state, f"shipped {n}", path=board, status="done") for n in range(4)]
    written: list[str] = []
    real = omnia_state.store.write

    def counting(path, content, **kwargs):
        written.append(path)
        return real(path, content, **kwargs)

    monkeypatch.setattr(omnia_state.store, "write", counting)
    result = asyncio.run(archive_completed_tasks(omnia_state, "t", {"path": board}))

    assert result["archived"] == 4
    assert written.count(board) == 1
    assert written.count(result["archive_path"]) == 1
    for task in done:
        assert asyncio.run(task_row(omnia_state, task["id"]))["path"] == result["archive_path"]


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


def test_roll_over_to_an_unpadded_date_compares_as_a_date(omnia_state):
    """'2030-12-01' < '2030-9-30' as text: an unpadded target used to pull
    not-yet-due tasks back to September."""
    september = add_task(omnia_state, "september", due="2030-09-01")
    december = add_task(omnia_state, "december", due="2030-12-01")

    result = asyncio.run(roll_over_tasks(omnia_state, "t", {"to": "2030-9-30", "path": INBOX}))

    assert result["to"] == "2030-09-30"
    assert asyncio.run(task_row(omnia_state, september["id"]))["due"] == "2030-09-30"
    assert asyncio.run(task_row(omnia_state, december["id"]))["due"] == "2030-12-01"


def remind_at(state, task: dict, when: str) -> None:
    asyncio.run(set_task_reminder(state, "t", {"task_id": task["id"], "remind_at": when}))


def test_roll_over_does_not_push_reminders_that_already_passed(omnia_state):
    """A reminder moves with its due date, and rolling overdue tasks to today
    can land it earlier today: a new ledger key inside the 24-hour grace, so
    the next tick pushed one stale reminder per task rolled."""
    today = datetime.now(timezone.utc).date()  # omnia_state runs in UTC
    old = (today - timedelta(days=5)).isoformat()
    tasks = [add_task(omnia_state, f"chore {n}", due=old) for n in range(3)]
    for task in tasks:
        remind_at(omnia_state, task, f"{old}T00:00")
    assert asyncio.run(alerts.task_due_reminders(omnia_state)) == []

    asyncio.run(roll_over_tasks(omnia_state, "t", {"to": today.isoformat()}))

    # Still on the due day, so a later move carries it on...
    rows = [asyncio.run(task_row(omnia_state, task["id"])) for task in tasks]
    assert {row["reminder_at"] for row in rows} == {f"{today}T00:00"}
    # ...but its time today has passed, so nothing is pushed for it.
    assert asyncio.run(alerts.task_due_reminders(omnia_state)) == []


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


def test_bulk_tools_with_nothing_to_do_say_why(omnia_state):
    board = school_board(omnia_state)

    edited = asyncio.run(bulk_edit_tasks(omnia_state, "t", {"path": "Scool", "delete": True}))
    assert edited["changed"] == 0
    assert board in edited["note"]

    wrong = asyncio.run(roll_over_tasks(omnia_state, "t", {"to": "2020-06-01", "path": "Scool"}))
    assert wrong["rolled"] == 0
    assert board in wrong["note"]
    right = asyncio.run(roll_over_tasks(omnia_state, "t", {"to": "2020-06-01", "path": "School"}))
    assert right["note"] == "No open task in School is due before 2020-06-01."

    archived = asyncio.run(archive_completed_tasks(omnia_state, "t", {"path": "School"}))
    assert archived["archived"] == 0
    assert archived["note"] == f"{board} has no completed tasks."


# --- status filters ----------------------------------------------------------
# A named status is the whole status filter: status='done' ANDed with the
# default open-only clause matched nothing, so "clear the done column" said 0.


def test_bulk_edit_by_done_status_needs_no_include_completed(omnia_state):
    board = school_board(omnia_state)
    done = [add_task(omnia_state, f"finished {n}", path=board, status="done") for n in range(2)]

    result = asyncio.run(
        bulk_edit_tasks(omnia_state, "t", {"path": "School", "status": "done", "delete": True})
    )

    assert result["changed"] == 2
    for task in done:
        assert asyncio.run(task_row(omnia_state, task["id"])) is None
    assert {row["status"] for row in asyncio.run(tasks_in(omnia_state, board))} == {"todo"}


def test_list_tasks_drops_done_tasks_before_the_limit(omnia_state, monkeypatch):
    """Done rows were dropped after `LIMIT 500`; old dated ones sort first,
    so a long-lived vault's open tasks fell off the end."""
    monkeypatch.setattr(tool_expansion, "LIST_TASKS_LIMIT", 5)
    for n in range(6):
        add_task(omnia_state, f"old done {n}", due=f"2001-01-0{n + 1}", status="done")
    open_ids = {add_task(omnia_state, f"open undated {n}")["id"] for n in range(2)}

    open_only = asyncio.run(
        list_tasks(omnia_state, "t", {"path": INBOX, "include_completed": False})
    )
    assert {task["id"] for task in open_only["tasks"]} == open_ids
    assert "truncated" not in open_only

    everything = asyncio.run(list_tasks(omnia_state, "t", {"path": INBOX}))
    assert len(everything["tasks"]) == 5
    assert everything["truncated"] is True


# --- validate before the first write ------------------------------------------
# Every task operation refuses a task without its metadata comment, so a bulk
# change used to stop at the first one with every row before it changed.


def bare_note(state, path: str, *lines: str) -> None:
    """A note whose tasks carry no metadata comment (indexed as `legacy:`)."""
    stored = state.store.create(path, "# Old\n\n" + "".join(f"{line}\n" for line in lines))
    asyncio.run(state.indexer.index_note(path, stored))


@pytest.mark.parametrize(
    "change", [{"set_status": "done"}, {"set_due": "2030-01-01"}, {"delete": True}]
)
def test_bulk_edit_refuses_bare_tasks_before_changing_any(omnia_state, change):
    fine = add_task(omnia_state, "review alpha")  # "Tasks Inbox.md" sorts first
    bare_note(omnia_state, "old.md", "- [ ] review beta")
    before = asyncio.run(task_row(omnia_state, fine["id"]))

    with pytest.raises(NoteError) as err:
        asyncio.run(bulk_edit_tasks(omnia_state, "t", {"text": "review", **change}))

    assert err.value.status == 409
    assert "old.md" in err.value.detail
    assert asyncio.run(task_row(omnia_state, fine["id"])) == before


def test_archive_refuses_bare_tasks_before_creating_the_archive(omnia_state):
    bare_note(omnia_state, "old.md", "- [x] shipped long ago")

    with pytest.raises(NoteError):
        asyncio.run(archive_completed_tasks(omnia_state, "t", {"path": "old.md"}))

    assert not omnia_state.store.exists("archive/old Done.md")


def test_archive_failing_after_creating_its_note_records_the_note(omnia_state, monkeypatch):
    board = school_board(omnia_state)
    add_task(omnia_state, "handed in", path=board, status="done")

    async def conflict(state, task_ids, target):
        raise NoteError(409, "Task moved or changed; refresh and try again")

    monkeypatch.setattr(tool_expansion, "move_tasks", conflict)
    result = asyncio.run(archive_completed_tasks(omnia_state, "t", {"path": "School"}))

    assert result["error"] == (
        "Failed with 0 of 1 applied: Task moved or changed; refresh and try again"
    )
    assert result["applied"] == 0
    assert result["_action"]["paths"] == ["archive/Tasks Done.md"]
    assert omnia_state.store.exists("archive/Tasks Done.md")


def test_bulk_edit_failing_part_way_records_what_it_changed(omnia_state, monkeypatch):
    tasks = [add_task(omnia_state, f"review {n}") for n in range(3)]
    real = tool_expansion.update_task_record
    calls = 0

    async def flaky(state, task_id, **changes):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise NoteError(409, "Task changed; refresh and try again")
        return await real(state, task_id, **changes)

    monkeypatch.setattr(tool_expansion, "update_task_record", flaky)
    result = asyncio.run(
        bulk_edit_tasks(omnia_state, "t", {"text": "review", "set_status": "done"})
    )

    assert result["error"].startswith("Applied 1 of 3")
    assert result["applied"] == 1
    assert result["failed_at"] == "review 1"
    action = result["_action"]
    assert action["summary"] == "Bulk set status to done on 1 task (partial: 1 of 3)"
    assert action["undoable"] is True
    statuses = [asyncio.run(task_row(omnia_state, t["id"]))["status"] for t in tasks]
    assert statuses == ["done", "todo", "todo"]

    asyncio.run(omnia_state.actions.undo(action["id"], "t"))
    assert "- [ ] review 0" in omnia_state.store.read(INBOX)


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
    task = make_recurring(omnia_state, "water plants", "2030-01-01", {"frequency": "daily", "interval": 3})

    complete(omnia_state, task["id"])

    rows = asyncio.run(tasks_in(omnia_state, INBOX))
    plants = [r for r in rows if r["text"] == "water plants"]
    assert {(r["status"], r["due"]) for r in plants} == {
        ("done", "2030-01-01"),
        ("todo", "2030-01-04"),
    }


def test_monthly_recurrence_advances_by_month(omnia_state):
    task = make_recurring(omnia_state, "pay rent", "2030-01-15", {"frequency": "monthly", "interval": 1})

    complete(omnia_state, task["id"])

    rows = asyncio.run(tasks_in(omnia_state, INBOX))
    assert any(r["status"] == "todo" and r["due"] == "2030-02-15" for r in rows if r["text"] == "pay rent")


def test_recurrence_stops_at_until(omnia_state):
    task = make_recurring(
        omnia_state, "finite", "2026-01-01", {"frequency": "daily", "interval": 1, "until": "2026-01-01"}
    )

    complete(omnia_state, task["id"])

    rows = asyncio.run(tasks_in(omnia_state, INBOX))
    assert not [r for r in rows if r["text"] == "finite" and r["status"] == "todo"]


def test_recurrence_until_is_stored_canonical(omnia_state):
    task = make_recurring(
        omnia_state, "compact", "2030-01-01", {"frequency": "daily", "until": "20300905"}
    )
    assert task_meta(omnia_state, task["id"])["recurrence"]["until"] == "2030-09-05"


def test_completing_a_plain_task_does_not_spawn(omnia_state):
    task = add_task(omnia_state, "one off")

    complete(omnia_state, task["id"])

    rows = asyncio.run(tasks_in(omnia_state, INBOX))
    assert len([r for r in rows if r["text"] == "one off"]) == 1


def reopen(state, task_id: str) -> None:
    asyncio.run(update_task(state, "t", {"task_id": task_id, "status": "todo"}))


def task_meta(state, task_id: str, path: str = INBOX) -> dict:
    for line in state.store.read(path).splitlines():
        match = parse.TASK_META_RE.match(line)
        if match and json.loads(match.group(1)).get("id") == task_id:
            return json.loads(match.group(1))
    raise AssertionError(f"no metadata for {task_id}")


def open_copies(state, text: str) -> list[dict]:
    rows = asyncio.run(tasks_in(state, INBOX))
    return [r for r in rows if r["text"] == text and r["status"] == "todo"]


def test_completing_again_after_an_undo_does_not_spawn_twice(omnia_state):
    """be-tasks-calendar-misc#5: the board's completion toast offers Undo, and
    done -> todo -> done spawned a second identical next occurrence."""
    task = make_recurring(omnia_state, "water plants", "2030-02-01", {"frequency": "weekly", "interval": 1})

    complete(omnia_state, task["id"])
    reopen(omnia_state, task["id"])
    complete(omnia_state, task["id"])

    spawned = open_copies(omnia_state, "water plants")
    assert [r["due"] for r in spawned] == ["2030-02-08"]
    assert task_meta(omnia_state, task["id"])["spawned_next"] == spawned[0]["id"]


def test_a_deleted_spawn_is_replaced_on_the_next_completion(omnia_state):
    task = make_recurring(omnia_state, "water plants", "2030-02-01", {"frequency": "weekly", "interval": 1})
    complete(omnia_state, task["id"])
    first = open_copies(omnia_state, "water plants")[0]

    asyncio.run(delete_task_record(omnia_state, first["id"]))
    reopen(omnia_state, task["id"])
    complete(omnia_state, task["id"])

    spawned = open_copies(omnia_state, "water plants")
    assert len(spawned) == 1 and spawned[0]["id"] != first["id"]
    assert spawned[0]["due"] == "2030-02-08"
    assert task_meta(omnia_state, task["id"])["spawned_next"] == spawned[0]["id"]


def test_monthly_anchor_survives_a_short_month():
    anchored = {"frequency": "monthly", "interval": 1, "anchor_day": 31}

    assert _next_occurrence("2026-01-31", anchored, today=date(2026, 1, 31)) == "2026-02-28"
    # The clamped 28th is not the new base: March is back on the 31st.
    assert _next_occurrence("2026-02-28", anchored, today=date(2026, 2, 28)) == "2026-03-31"
    assert _next_occurrence("2026-03-31", anchored, today=date(2026, 3, 31)) == "2026-04-30"


def test_yearly_leap_day_anchor_returns_in_the_next_leap_year():
    anchored = {"frequency": "yearly", "interval": 1, "anchor_day": 29, "anchor_month": 2}

    assert _next_occurrence("2028-02-29", anchored, today=date(2028, 2, 29)) == "2029-02-28"
    assert _next_occurrence("2031-02-28", anchored, today=date(2031, 2, 28)) == "2032-02-29"
    # Catching up from a common year's 28th lands on the leap day too.
    assert _next_occurrence("2029-02-28", anchored, today=date(2031, 6, 1)) == "2032-02-29"


def test_next_occurrence_rolls_past_today():
    daily = {"frequency": "daily", "interval": 1}
    assert _next_occurrence("2026-01-01", daily, today=date(2026, 1, 11)) == "2026-01-12"
    every_three = {"frequency": "daily", "interval": 3}
    assert _next_occurrence("2026-01-01", every_three, today=date(2026, 1, 11)) == "2026-01-13"
    monthly = {"frequency": "monthly", "interval": 1, "anchor_day": 15}
    assert _next_occurrence("2026-01-15", monthly, today=date(2026, 4, 15)) == "2026-05-15"
    # The until bound still ends a series that catching up runs past.
    bounded = {"frequency": "daily", "interval": 1, "until": "2026-01-05"}
    assert _next_occurrence("2026-01-01", bounded, today=date(2026, 1, 11)) is None


def test_a_monthly_chain_keeps_its_anchor_through_completions(omnia_state):
    task = make_recurring(omnia_state, "pay rent", "2030-01-31", {"frequency": "monthly", "interval": 1})
    assert task_meta(omnia_state, task["id"])["recurrence"]["anchor_day"] == 31

    complete(omnia_state, task["id"])
    february = open_copies(omnia_state, "pay rent")
    assert [r["due"] for r in february] == ["2030-02-28"]
    complete(omnia_state, february[0]["id"])

    assert [r["due"] for r in open_copies(omnia_state, "pay rent")] == ["2030-03-31"]


def test_completing_an_overdue_chore_schedules_the_next_ahead(omnia_state):
    today = datetime.now(timezone.utc).date()  # omnia_state runs in UTC
    task = make_recurring(
        omnia_state,
        "water plants",
        (today - timedelta(days=10)).isoformat(),
        {"frequency": "daily", "interval": 1},
    )

    complete(omnia_state, task["id"])

    spawned = open_copies(omnia_state, "water plants")
    assert [r["due"] for r in spawned] == [(today + timedelta(days=1)).isoformat()]


def test_the_next_occurrence_keeps_the_reminder(omnia_state):
    """gap-time-dst#6: a recurring chore reminded once, then the series went
    silent because the spawn dropped reminder_at."""
    task = make_recurring(omnia_state, "water plants", "2030-09-24", {"frequency": "weekly", "interval": 1})
    asyncio.run(
        set_task_reminder(
            omnia_state, "t", {"task_id": task["id"], "remind_at": "2030-09-24T08:00"}
        )
    )

    complete(omnia_state, task["id"])

    spawned = open_copies(omnia_state, "water plants")
    assert [(r["due"], r["reminder_at"]) for r in spawned] == [("2030-10-01", "2030-10-01T08:00")]


def test_a_next_occurrence_reminder_already_past_is_not_pushed(omnia_state):
    today = datetime.now(timezone.utc).date()  # omnia_state runs in UTC
    due = today - timedelta(days=10)
    task = make_recurring(omnia_state, "water plants", due.isoformat(), {"frequency": "daily", "interval": 1})
    # Midnight the day before: the spawn, due tomorrow, reminds at midnight today.
    remind_at(omnia_state, task, f"{due - timedelta(days=1)}T00:00")

    complete(omnia_state, task["id"])

    spawned = open_copies(omnia_state, "water plants")
    tomorrow = (today + timedelta(days=1)).isoformat()
    assert [(r["due"], r["reminder_at"]) for r in spawned] == [(tomorrow, f"{today}T00:00")]
    assert asyncio.run(alerts.task_due_reminders(omnia_state)) == []


# --- one canonical date form -------------------------------------------------


@pytest.mark.parametrize("raw", ["2026-09-05", "2026-9-5", " 2026-9-05 ", "20260905"])
def test_user_dates_are_read_into_one_canonical_form(raw):
    assert parse_user_date(raw) == "2026-09-05"


@pytest.mark.parametrize("raw", ["", "soon", "2026-13-01", "2026-02-30", "2026-09-05T10:00"])
def test_a_date_that_is_not_one_is_rejected(raw):
    with pytest.raises(NoteError) as caught:
        parse_user_date(raw, "Task due date")
    assert caught.value.status == 400
    assert "Task due date must be YYYY-MM-DD" in str(caught.value.detail)


def test_an_unpadded_due_is_stored_canonical(omnia_state):
    """The visible marker must match DUE_RE, or the parser folds the date into
    the task text; the row must sort and compare as a real date."""
    task = add_task(omnia_state, "essay", due="2026-9-5", due_visible=True)

    assert task["due"] == "2026-09-05"
    row = asyncio.run(task_row(omnia_state, task["id"]))
    assert row["due"] == "2026-09-05"
    assert row["text"] == "essay"
    line = next(l for l in omnia_state.store.read(INBOX).splitlines() if "essay" in l and "- [" in l)
    assert parse.DUE_RE.search(line).group(1) == "2026-09-05"


def test_bulk_due_filters_compare_as_dates(omnia_state):
    inside = add_task(omnia_state, "mid september", due="2030-09-15")
    before = add_task(omnia_state, "august", due="2030-08-15")

    result = asyncio.run(
        bulk_edit_tasks(omnia_state, "t", {"due_start": "2030-9-1", "set_status": "done"})
    )

    assert result["changed"] == 1
    assert asyncio.run(task_row(omnia_state, inside["id"]))["status"] == "done"
    assert asyncio.run(task_row(omnia_state, before["id"]))["status"] == "todo"
