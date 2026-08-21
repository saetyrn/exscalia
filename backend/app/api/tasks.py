from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel

from .. import task_boards
from ..task_service import (
    create_task,
    delete_task,
    public_task,
    reorder_tasks,
    update_task,
)
from ..vault.internal import iso_utc
from ..vault.notes import NoteError

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def _week_start(request: Request) -> str:
    """Midnight on the Monday of the current week, as a UTC stamp.

    The board is a weekly one: a task completed during the week stays in Done
    as a record of the week's work, and the column clears itself once Sunday
    night passes. Computed in the user's timezone — "Sunday night" is local,
    and on UTC it would land mid-Sunday for most of the Americas.
    """
    try:
        tz = ZoneInfo(request.app.state.timezone)
    except (ZoneInfoNotFoundError, AttributeError):
        tz = ZoneInfo("America/New_York")
    now = datetime.now(tz)
    monday = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return iso_utc(monday.astimezone(timezone.utc))


class CreateTaskBody(BaseModel):
    text: str
    # A folder's board note, resolved server-side — the client knows the
    # folder, never the note path (which may be numbered or renamed).
    folder: str | None = None
    path: str = "Tasks Inbox.md"
    status: str = "todo"
    due: str | None = None
    due_visible: bool = False
    description: str | None = None


class UpdateTaskBody(BaseModel):
    text: str | None = None
    status: str | None = None
    due: str | None = None
    due_set: bool = False
    due_visible: bool | None = None
    description: str | None = None
    description_set: bool = False


@router.get("")
async def list_tasks(
    request: Request,
    include_completed: bool = False,
    q: str | None = Query(default=None),
    folder: str | None = Query(default=None),
):
    clauses = ["path NOT LIKE 'templates/%'"]
    params: list[object] = []
    if folder:
        # A folder board shows its own open tasks — that is the whole point of
        # it — so the board clause below is deliberately not applied here.
        board = await task_boards.board_for_folder(request.app.state.db, folder)
        if not board:
            raise NoteError(404, f"No task board in {folder}")
        clauses.append("path = ?")
        params.append(board)
    else:
        # The universal board means "everything on my plate". A folder board's
        # open tasks live on that board instead; completed ones rejoin here,
        # and still clear on Monday like everything else.
        clauses.append("(board = 0 OR status = 'done')")
    if not include_completed:
        clauses.append(
            "(status != 'done' OR completed_at IS NULL OR completed_at >= ?)"
        )
        params.append(_week_start(request))
    if q and q.strip():
        clauses.append("(text LIKE ? OR path LIKE ?)")
        needle = f"%{q.strip()}%"
        params.extend([needle, needle])
    rows = await request.app.state.db.execute_fetchall(
        # Hand-ordered cards (sort set by dragging) hold their position at the
        # top; everything else keeps the default due-date order below them.
        # A task is appended to its note, so an untouched column and a newly
        # created task both land exactly where they always did.
        f"""SELECT * FROM tasks WHERE {' AND '.join(clauses)}
            ORDER BY
              CASE status WHEN 'in_progress' THEN 0 WHEN 'todo' THEN 1 ELSE 2 END,
              CASE WHEN sort IS NULL THEN 1 ELSE 0 END,
              sort,
              CASE WHEN due IS NULL THEN 1 ELSE 0 END,
              due, path, line""",
        tuple(params),
    )
    return [public_task(dict(row)) for row in rows]


@router.get("/timeline")
async def task_timeline(request: Request, limit: int = 200):
    """Every task lifecycle event — created/started/completed from the live
    index, plus deletions from the tombstone ledger — newest first."""
    state = request.app.state
    milestones = (
        ("created", "created_at"),
        ("started", "started_at"),
        ("completed", "completed_at"),
        ("reopened", "reopened_at"),
    )
    events = []
    rows = await state.db.execute_fetchall(
        "SELECT id, text, path, created_at, started_at, completed_at, reopened_at FROM tasks"
        " WHERE path NOT LIKE 'templates/%'"
    )
    for row in rows:
        r = dict(row)
        for kind, key in milestones:
            if r.get(key):
                events.append(
                    {"at": r[key], "kind": kind, "id": r["id"], "text": r["text"]}
                )
    for tomb in state.task_tombstones.all().values():
        for kind, key in (*milestones, ("deleted", "deleted_at")):
            if tomb.get(key):
                events.append(
                    {
                        "at": tomb[key],
                        "kind": kind,
                        "id": tomb.get("id"),
                        "text": tomb.get("text") or "",
                        "deleted": True,
                    }
                )
    events.sort(key=lambda e: str(e["at"]), reverse=True)
    return events[: max(1, min(limit, 500))]


# Task writes rewrite note files, so they take the same mutation lock OMNIA's
# direct tools hold across their multi-step edits — a board click can no
# longer interleave into the middle of a bulk OMNIA operation. The lock is
# taken HERE and not inside task_service: the OMNIA tools call task_service
# while already holding this (non-reentrant) lock.
def _mutations(request: Request):
    return request.app.state.actions.mutation_lock


@router.post("")
async def add_task(request: Request, body: CreateTaskBody):
    path = body.path
    if body.folder:
        board = await task_boards.board_for_folder(request.app.state.db, body.folder)
        if not board:
            raise NoteError(404, f"No task board in {body.folder}")
        path = board
    async with _mutations(request):
        return await create_task(
            request.app.state,
            text=body.text,
            path=path,
            status=body.status,
            due=body.due,
            due_visible=body.due_visible,
            description=body.description,
        )


@router.patch("/{task_id}")
async def patch_task(request: Request, task_id: str, body: UpdateTaskBody):
    due: str | None | object = body.due if body.due_set else ...
    description: str | None | object = (
        body.description if body.description_set else ...
    )
    async with _mutations(request):
        return await update_task(
            request.app.state,
            task_id,
            text=body.text,
            status=body.status,
            due=due,
            due_visible=body.due_visible,
            description=description,
        )


class ReorderBody(BaseModel):
    # The column's complete order after the drag, top card first.
    ids: list[str]


@router.post("/reorder")
async def reorder(request: Request, body: ReorderBody):
    async with _mutations(request):
        return await reorder_tasks(request.app.state, body.ids)


@router.delete("/{task_id}")
async def remove_task(request: Request, task_id: str):
    async with _mutations(request):
        return await delete_task(request.app.state, task_id)
