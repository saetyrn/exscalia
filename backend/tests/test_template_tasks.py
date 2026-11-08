"""Notes made from a template get their own tasks.

A template saved in the editor carries task-metadata comments like any note.
render() used to copy them verbatim, so every note made from it shared the
template's task ids, and the indexer's INSERT OR IGNORE dropped the copies:
the tasks existed in markdown and nowhere else.
"""
import asyncio
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import meta as meta_api
from app.api import notes as notes_api
from app.index import parse
from app.vault import templates as tpl
from app.vault.notes import NoteError

TZ = "America/New_York"

CHECK = (
    "## Checklist\n\n"
    '<!-- exscalia-task:{"created_at":"2026-01-01T00:00:00Z","due":null,'
    '"due_visible":false,"id":"same-a","reminder_at":"2030-01-01T09:00"} -->\n'
    "- [ ] Stretch\n"
    '<!-- exscalia-task:{"created_at":"2026-01-01T00:00:00Z","due":null,'
    '"description":"Any book","due_visible":false,"id":"same-b","sort":3} -->\n'
    "- [ ] Read 10 pages\n\n"
    "~~~\n"
    '<!-- exscalia-task:{"id":"fenced"} -->\n'
    "~~~\n"
)

DAILY = (
    "# {{date}}\n\n"
    '<!-- exscalia-task:{"created_at":"2026-01-01T00:00:00Z","due":null,'
    '"due_visible":false,"id":"daily-a"} -->\n'
    "- [ ] Morning pages\n"
)


def make_client(state, *routers) -> TestClient:
    app = FastAPI()
    app.state = state
    app.state.timezone = TZ

    @app.exception_handler(NoteError)
    async def handler(_request, exc: NoteError):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=exc.status, content={"detail": exc.detail})

    for router in routers:
        app.include_router(router)
    return TestClient(app)


def ids(content: str) -> list[str]:
    found = []
    for line in content.splitlines():
        match = parse.TASK_META_RE.match(line)
        if match:
            found.append(json.loads(match.group(1))["id"])
    return found


def add_template(state, path: str, content: str) -> None:
    if state.store.exists(path):
        written = state.store.write(path, content)
    else:
        written = state.store.create(path, content)
    asyncio.run(state.indexer.index_note(path, written))


def test_render_mints_fresh_task_metadata(tmp_path):
    vault = tmp_path / "vault"
    (vault / "templates").mkdir(parents=True)
    (vault / "templates" / "check.md").write_text(CHECK, encoding="utf-8")

    first = tpl.render(vault, "check", "one")
    second = tpl.render(vault, "check", "two")

    for rendered in (first, second):
        assert "same-a" not in rendered and "same-b" not in rendered
        # A template's reminder must not fire once per note made from it.
        assert "reminder_at" not in rendered
        assert '"sort"' not in rendered
        # What the task says travels with it; its bookkeeping does not.
        assert '"description":"Any book"' in rendered
        # Code is left alone.
        assert '<!-- exscalia-task:{"id":"fenced"} -->' in rendered
    first_ids = [i for i in ids(first) if i != "fenced"]
    second_ids = [i for i in ids(second) if i != "fenced"]
    assert len(first_ids) == len(second_ids) == 2
    assert not set(first_ids) & set(second_ids)


def test_template_metadata_stripping_keeps_line_separators(tmp_path):
    """str.splitlines also broke on \\r, \\x0c, U+2028 and the rest, and
    joining with \\n turned each of those characters into a newline in every
    note made from the template."""
    template = (
        "Intro\u2028line\r\n"
        '<!-- exscalia-task:{"description":"Any book","id":"same-a"} -->\r\n'
        "- [ ] Read\x0c10 pages\r\n"
        '<!-- exscalia-task:{"id":"same-b"} -->\r\n'
        "- [ ] Stretch\r\n"
    )
    assert tpl._strip_task_metadata(template) == (
        "Intro\u2028line\r\n"
        '<!-- exscalia-task:{"description":"Any book"} -->\r\n'
        "- [ ] Read\x0c10 pages\r\n"
        "- [ ] Stretch\r\n"
    )

    vault = tmp_path / "vault"
    (vault / "templates").mkdir(parents=True)
    (vault / "templates" / "odd.md").write_bytes(template.encode("utf-8"))
    rendered = tpl.render(vault, "odd", "one")
    # Read like any note (universal newlines), the rest kept as written.
    assert rendered.startswith("Intro\u2028line\n")
    assert "\n- [ ] Read\x0c10 pages\n" in rendered
    assert len(ids(rendered)) == 2


def test_notes_from_one_template_index_their_own_tasks(state):
    add_template(state, "templates/check.md", CHECK)
    client = make_client(state, notes_api.router)

    for path in ("n1.md", "n2.md"):
        response = client.post("/api/notes", json={"path": path, "template": "check"})
        assert response.status_code == 200

    rows = asyncio.run(
        state.db.execute_fetchall(
            "SELECT id, path FROM tasks WHERE path IN ('n1.md', 'n2.md')"
        )
    )
    assert len(rows) == 4
    assert len({row["id"] for row in rows}) == 4
    assert {row["path"] for row in rows} == {"n1.md", "n2.md"}


def test_daily_notes_from_the_template_index_their_own_tasks(state):
    add_template(state, "templates/daily.md", DAILY)
    client = make_client(state, meta_api.router)

    for day in ("2030-09-20", "2030-09-21"):
        assert client.post(f"/api/daily/{day}").json()["created"] is True

    rows = asyncio.run(
        state.db.execute_fetchall(
            "SELECT id, path FROM tasks WHERE path LIKE 'daily/%' ORDER BY path"
        )
    )
    assert [row["path"] for row in rows] == [
        "daily/2030-09-20.md",
        "daily/2030-09-21.md",
    ]
    assert len({row["id"] for row in rows}) == 2
    assert "daily-a" not in {row["id"] for row in rows}
