"""A folder board's open tasks stay off the universal board and the calendar;
completed ones rejoin both."""
import asyncio
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import calendar as calendar_api
from app.api import notes as notes_api
from app.api import tasks as tasks_module
from app.vault.notes import NoteError

TZ = "America/New_York"


def make_client(state, router) -> TestClient:
    app = FastAPI()
    app.state = state
    app.state.timezone = TZ

    @app.exception_handler(NoteError)
    async def handler(_request, exc: NoteError):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=exc.status, content={"detail": exc.detail})

    app.include_router(router)
    return TestClient(app)


def seed(state, task_id, *, path, board, status="todo", completed_at=None, due=None):
    async def run():
        await state.db.execute(
            "INSERT INTO tasks (id, path, line, text, status, completed_at, due, board)"
            " VALUES (?, ?, 1, ?, ?, ?, ?, ?)",
            (task_id, path, f"task {task_id}", status, completed_at, due, board),
        )
        await state.db.commit()

    asyncio.run(run())


def test_universal_board_hides_open_board_tasks_but_keeps_finished_ones(state):
    from app.vault.internal import iso_utc
    from datetime import datetime, timezone as tz

    now = iso_utc(datetime.now(tz.utc))
    seed(state, "board-open", path="Projects/Tasks.md", board=1)
    seed(state, "board-done", path="Projects/Tasks.md", board=1, status="done", completed_at=now)
    seed(state, "normal-open", path="Tasks Inbox.md", board=0)

    client = make_client(state, tasks_module.router)
    visible = {t["id"] for t in client.get("/api/tasks").json()}
    assert "normal-open" in visible
    # Finished board work is still this week's work.
    assert "board-done" in visible
    assert "board-open" not in visible


def test_folder_board_shows_only_its_own_tasks(state):
    asyncio.run(state.indexer.index_note("Projects/Tasks.md", BOARD))
    asyncio.run(state.indexer.index_note("Ideas/Tasks.md", BOARD))
    seed(state, "mine", path="Projects/Tasks.md", board=1)
    seed(state, "theirs", path="Ideas/Tasks.md", board=1)
    seed(state, "global", path="Tasks Inbox.md", board=0)

    client = make_client(state, tasks_module.router)
    scoped = {t["id"] for t in client.get("/api/tasks?folder=Projects").json()}
    assert scoped == {"mine"}

    # A subfolder board is separate from its parent's.
    assert client.get("/api/tasks?folder=Nowhere").status_code == 404


BOARD = '---\ntitle: "Tasks"\ntask_board: true\n---\n\n# Tasks\n'


def test_board_task_contributes_only_a_completion_to_the_calendar(state):
    from zoneinfo import ZoneInfo

    rows = [
        {
            "id": "b",
            "path": "Projects/Tasks.md",
            "text": "board task",
            "status": "done",
            "created_at": "2026-07-20T12:00:00Z",
            "started_at": "2026-07-21T12:00:00Z",
            "completed_at": "2026-07-22T12:00:00Z",
            "due": "2026-07-25",
            "due_visible": 0,
            "board": 1,
        }
    ]
    events = calendar_api._events(rows, ZoneInfo(TZ))
    # Not three dots in the past, and no due marker while it was open.
    assert [e["type"] for e in events] == ["completed"]

    ordinary = [{**rows[0], "id": "n", "board": 0}]
    kinds = {e["type"] for e in calendar_api._events(ordinary, ZoneInfo(TZ))}
    assert kinds == {"created", "started", "completed", "due"}


def test_creating_a_board_is_one_per_folder(state):
    state.store.create_folder("Projects")
    client = make_client(state, notes_api.router)

    created = client.post("/api/notes/task-board", json={"folder": "Projects"}).json()
    assert created["path"] == "Projects/Tasks.md"

    again = client.post("/api/notes/task-board", json={"folder": "Projects"})
    assert again.status_code == 409

    # The vault root is the universal board's territory.
    assert client.post("/api/notes/task-board", json={"folder": ""}).status_code == 400

    listed = {n["path"]: n["board"] for n in client.get("/api/notes").json()}
    assert listed["Projects/Tasks.md"] is True


def test_board_folder_casing_is_canonicalized(state):
    """A wrong-case folder must resolve to the on-disk spelling — on macOS the
    file lands in the existing directory either way, and indexing the flag
    under the requested spelling orphans it (exact-path set match in
    /api/notes). A folder that doesn't exist at all is a 404, not a silent
    new folder."""
    state.store.create_folder("ideas")
    client = make_client(state, notes_api.router)

    created = client.post("/api/notes/task-board", json={"folder": "IDEAS"}).json()
    assert created["folder"] == "ideas"
    assert created["path"] == "ideas/Tasks.md"
    listed = {n["path"]: n["board"] for n in client.get("/api/notes").json()}
    assert listed["ideas/Tasks.md"] is True

    assert (
        client.post("/api/notes/task-board", json={"folder": "nope"}).status_code
        == 404
    )


def test_board_name_steps_past_an_existing_note(state):
    state.store.create_folder("Projects")
    state.store.create("Projects/Tasks.md", "---\ntitle: Tasks\n---\n\nmine\n")
    client = make_client(state, notes_api.router)

    created = client.post("/api/notes/task-board", json={"folder": "Projects"}).json()
    assert created["path"] == "Projects/Tasks 2.md"


def test_digest_skips_open_board_tasks(state):
    from app.overseer.tools import get_digest_data

    seed(state, "board-open", path="Projects/Tasks.md", board=1, due=date.today().isoformat())
    seed(state, "normal-open", path="Tasks Inbox.md", board=0, due=date.today().isoformat())

    state.timezone = TZ
    digest = asyncio.run(get_digest_data(state, "t", {}))
    texts = {
        t["text"]
        for bucket in digest["tasks"].values()
        for t in bucket
    }
    assert "task normal-open" in texts
    assert "task board-open" not in texts


def test_a_conflict_copy_of_a_board_is_not_a_second_board(state):
    """The copy lands in the board's folder with the client's frontmatter. It
    used to keep `task_board: true`, and the folder's board then flipped to
    whichever of the two was indexed last."""
    from app import task_boards
    from app.vault import fm

    state.store.create_folder("Projects")
    client = make_client(state, notes_api.router)
    board = client.post("/api/notes/task-board", json={"folder": "Projects"}).json()["path"]
    content = client.get(f"/api/notes/{board}").json()["content"]

    saved = client.put(
        f"/api/notes/{board}",
        json={"content": content + "\n- [ ] from the other device\n", "base_revision": "stale"},
    ).json()
    assert saved["status"] == "conflict_copy"
    copy = saved["conflict_path"]
    assert copy.startswith("Projects/")

    assert asyncio.run(task_boards.board_for_folder(state.db, "Projects")) == board
    assert task_boards.BOARD_KEY not in fm.parse(state.store.read(copy))[0]
    listed = {n["path"]: n["board"] for n in client.get("/api/notes").json()}
    assert listed[board] is True
    assert listed[copy] is False


def test_the_oldest_board_in_a_folder_wins(state):
    """Two boards in one folder resolve the same way whichever was saved
    last, and by age rather than by path."""
    from app import task_boards

    older = '---\ntitle: "Z"\ncreated: "2026-01-01T00:00:00Z"\ntask_board: true\n---\n\n# Z\n'
    newer = '---\ntitle: "A"\ncreated: "2026-06-01T00:00:00Z"\ntask_board: true\n---\n\n# A\n'
    for order in (
        (("Projects/Z.md", older), ("Projects/A.md", newer)),
        (("Projects/A.md", newer), ("Projects/Z.md", older)),
    ):
        for path, content in order:
            asyncio.run(state.indexer.index_note(path, content))
        assert asyncio.run(task_boards.board_map(state.db))["Projects"] == "Projects/Z.md"


def test_a_flagged_copy_sharing_the_boards_created_stamp_does_not_take_over(state):
    """Copies made before conflict copies and duplicates dropped the flag keep
    `task_board: true` and the original's `created`. The path tie-break then
    sorted 'Tasks copy.md' (a space) ahead of 'Tasks.md' (a dot)."""
    from app import task_boards

    board = '---\ntitle: "Tasks"\ncreated: "2026-01-01T00:00:00Z"\ntask_board: true\n---\n\n# Tasks\n'
    paths = (
        "Projects/Tasks (conflict 2026-09-01 1200).md",
        "Projects/Tasks copy.md",
        "Projects/Tasks.md",
    )
    for order in (paths, paths[::-1]):
        for path in order:
            asyncio.run(state.indexer.index_note(path, board))
        assert asyncio.run(task_boards.board_map(state.db))["Projects"] == "Projects/Tasks.md"
