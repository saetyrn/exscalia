"""The calendar files notes under the user's day, not the UTC one.

Notes are stamped in UTC, and bucketing by the stamp's leading date put
anything written after 8 pm Eastern on the next day's cell, while the tasks in
the same response were already converted to the configured zone.
"""
import asyncio
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import calendar as calendar_api
from app.vault.notes import NoteError

TZ = "America/New_York"


def make_client(state) -> TestClient:
    app = FastAPI()
    app.state = state
    app.state.timezone = TZ
    state.task_tombstones = SimpleNamespace(all=lambda: {})
    state.activities = SimpleNamespace(range_totals=lambda first, last: {})
    state.events = SimpleNamespace(occurrences=lambda first, last: [])

    @app.exception_handler(NoteError)
    async def handler(_request, exc: NoteError):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=exc.status, content={"detail": exc.detail})

    app.include_router(calendar_api.router)
    return TestClient(app)


def only_notes(state, *rows) -> None:
    """Replace the seeded vault's rows, which are stamped with today's date."""

    async def run():
        await state.db.execute("DELETE FROM notes")
        for path, created, modified in rows:
            await state.db.execute(
                "INSERT INTO notes(path, title, created, modified, mtime, hash, pinned, task_board)"
                " VALUES (?,?,?,?,0,'',0,0)",
                (path, path, created, modified),
            )
        await state.db.commit()

    asyncio.run(run())


def paths(response) -> list[str]:
    return [note["path"] for note in response.json()["notes"]]


def test_an_evening_note_belongs_to_its_local_day(state):
    # 23:30 on the 21st in New York.
    only_notes(state, ("late.md", "2026-09-22T03:30:00Z", "2026-09-22T03:30:00Z"))
    client = make_client(state)

    day = client.get("/api/calendar/day/2026-09-21").json()
    assert [note["path"] for note in day["notes"]] == ["late.md"]
    assert day["notes"][0]["created_today"]
    assert "late.md" not in paths(client.get("/api/calendar/day/2026-09-22"))

    ranged = client.get("/api/calendar/range?start=2026-09-21&end=2026-09-21")
    assert paths(ranged) == ["late.md"]
    assert paths(client.get("/api/calendar/range?start=2026-09-22&end=2026-09-22")) == []

    assert client.get("/api/calendar/2026/9").json()["activity"] == {"2026-09-21": 1}


def test_a_note_edited_on_the_day_is_listed_as_modified(state):
    only_notes(state, ("old.md", "2026-09-01T12:00:00Z", "2026-09-22T02:00:00Z"))
    client = make_client(state)

    day = client.get("/api/calendar/day/2026-09-21").json()
    assert [note["path"] for note in day["notes"]] == ["old.md"]
    assert not day["notes"][0]["created_today"]

    activity = client.get("/api/calendar/2026/9").json()["activity"]
    assert activity == {"2026-09-01": 1, "2026-09-21": 1}


def test_a_hand_written_date_keeps_its_day():
    """`created: 2024-05-01` in frontmatter is already a local date; reading it
    as midnight in the server's zone would move it a day west of UTC."""
    far_west = ZoneInfo("Pacific/Pago_Pago")
    assert calendar_api._note_day("2026-09-21", far_west) == "2026-09-21"
    assert calendar_api._note_day("2026-09-21T10:00:00", far_west) == "2026-09-21"
    assert calendar_api._note_day("2026-09-22T03:30:00Z", ZoneInfo(TZ)) == "2026-09-21"
