"""Documents and boards on the server: kinds, orientation, paper colours, the
cover rule, boards with their own extent and the area a render shows.

Additive throughout: a client that names none of this (the iPad build on the
owner's device, the v0.1.30 desktop viewer) must see exactly what it always
did, and the tests that say so sit beside the ones for the new fields.
"""

import asyncio
import json
import sqlite3

import pytest

from app.index.db import open_db
from app.overseer.tool_expansion import open_in_app
from app.overseer.tools import execute_tool
from test_paper_api import PNG, make_client, new_notebook, put_page

A, B, C = "a" * 32, "b" * 32, "c" * 32
BOARD = "d" * 32
EXTENT = {"width": 16384.0, "height": 16384.0}
RECT = {"x": 7509.0, "y": 7680.0, "width": 1366.0, "height": 1024.0}
OTHER_PNG = PNG + b"1"

# notebook.json as an older client's create made it, and nothing more.
MANIFEST_KEYS = {
    "id", "title", "created", "modified", "template", "paperSize", "cover",
    "tags", "favorite", "pages", "seq", "path",
}


def board_part(**changes) -> dict:
    """A board's page part, as `pagePartJSON` sends one."""
    part = {
        "template": "grid",
        "size": EXTENT,
        "size_name": "board",
        "board_extent": EXTENT,
        "paper_color": "cream",
        "objects": [],
        "bookmarked": False,
    }
    part.update(changes)
    return part


def new_whiteboard(client, title="Plans", **body) -> dict:
    return new_notebook(client, title, kind="whiteboard", **body)


def save_board(client, wb, base=0, **parts):
    parts.setdefault("page", board_part())
    parts.setdefault("drawing", b"ink")
    response = put_page(client, wb["path"], BOARD, base, **parts)
    assert response.status_code == 200, response.text
    return response.json()["page"]


def page_json(state, notebook: str, page_id: str) -> dict:
    path = state.paper.dir / notebook / "pages" / page_id / "page.json"
    return json.loads(path.read_text())


def run_tool(state, name: str, inp: dict) -> dict:
    return json.loads(asyncio.run(execute_tool(state, "s", name, inp)))


# -- notebooks ---------------------------------------------------------------


def test_a_whiteboard_is_a_notebook_record_on_board_paper(state):
    client = make_client(state)
    wb = new_whiteboard(client, paperSize="a4", paperColor="cream")
    assert (wb["kind"], wb["paperSize"], wb["template"]) == ("whiteboard", "board", "grid")
    assert wb["paperColor"] == "cream"

    nb = new_notebook(client, "Ideas", orientation="landscape", paperColor="dark")
    assert (nb["orientation"], nb["paperColor"]) == ("landscape", "dark")
    assert "kind" not in nb  # written only when named; absent means notebook

    summaries = {s["title"]: s for s in client.get("/api/paper").json()["notebooks"]}
    assert summaries["Plans"]["kind"] == "whiteboard"
    assert summaries["Plans"]["paperColor"] == "cream"
    assert summaries["Ideas"]["kind"] == "notebook"
    assert summaries["Ideas"]["orientation"] == "landscape"
    manifest = client.get(f"/api/paper/notebooks/{wb['path']}").json()
    assert (manifest["kind"], manifest["orientation"]) == ("whiteboard", "portrait")


def test_a_client_naming_none_of_the_new_fields_gets_what_it_always_did(state):
    client = make_client(state)
    nb = new_notebook(client, "Ideas")
    assert set(nb) == MANIFEST_KEYS
    on_disk = json.loads((state.paper.dir / nb["path"] / "notebook.json").read_text())
    assert not {"kind", "orientation", "paperColor"} & set(on_disk)

    created = client.post(f"/api/paper/notebooks/{nb['path']}/pages", json={"id": A})
    assert created.status_code == 200, created.text
    saved = put_page(
        client,
        nb["path"],
        A,
        1,
        page={"template": "grid", "size": {"width": 1024.0, "height": 1366.0},
              "size_points": {"width": 1024.0, "height": 2048.0}, "objects": []},
        drawing=b"ink",
        render=PNG,
    )
    assert saved.status_code == 200, saved.text
    page = saved.json()["page"]
    for key in ("paper_color", "board_extent", "render_rect"):
        assert key not in page
        assert key not in page_json(state, nb["path"], A)
    assert page["size"] == {"width": 1024.0, "height": 1366.0}
    assert page["size_points"] == {"width": 1024.0, "height": 2048.0}
    # The 20,000-point cap on a page that is not a board is unchanged.
    too_big = put_page(
        client, nb["path"], A, 2,
        page={"size_points": {"width": 20000.0, "height": 100.0}},
    )
    assert too_big.status_code == 400


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "board"},
        {"kind": "Whiteboard"},
        {"orientation": "sideways"},
        {"paperColor": "red"},
        {"template": "lined"},
        {"paperSize": "a3"},
    ],
)
def test_unknown_names_are_refused(state, body):
    client = make_client(state)
    made = client.post("/api/paper/notebooks", json={"title": "X", **body})
    assert made.status_code == 400, made.text
    nb = new_notebook(client, "Y")
    patched = client.patch(f"/api/paper/notebooks/{nb['path']}", json=body)
    assert patched.status_code == 400, patched.text


@pytest.mark.parametrize(
    "part",
    [
        {"paper_color": "red"},
        {"size_name": "poster"},
        {"template": "lined"},
        {"board_extent": {"width": 1023, "height": 4096}},
        {"board_extent": {"width": 4096, "height": 32769}},
        {"board_extent": {"width": 4096}},
    ],
)
def test_unknown_page_values_are_refused_and_nothing_is_written(state, part):
    client = make_client(state)
    wb = new_whiteboard(client)
    response = put_page(client, wb["path"], BOARD, 0, page=board_part(**part), drawing=b"x")
    assert response.status_code == 400, response.text
    assert not (state.paper.dir / wb["path"] / "pages" / BOARD).exists()


def test_the_new_template_and_size_names_are_accepted(state):
    client = make_client(state)
    nb = new_notebook(client, "Covered", template="ruled")
    cover = put_page(
        client, nb["path"], A, 0,
        page={"template": "cover", "paper_color": "navy", "size_name": "iPad"},
        drawing=b"",
    )
    assert cover.status_code == 200, cover.text
    assert cover.json()["page"]["template"] == "cover"
    assert cover.json()["page"]["paper_color"] == "navy"
    made = client.post(
        f"/api/paper/notebooks/{nb['path']}/pages",
        json={"id": B, "size": "board", "size_name": "board", "template": "dotted"},
    )
    assert made.status_code == 200, made.text
    assert made.json()["board_extent"] == EXTENT


def test_patch_sets_kind_orientation_and_colour(state):
    client = make_client(state)
    nb = new_notebook(client, "Ideas")
    path = f"/api/paper/notebooks/{nb['path']}"

    patched = client.patch(path, json={"orientation": "landscape", "paperColor": "cream"})
    assert patched.status_code == 200, patched.text
    assert (patched.json()["orientation"], patched.json()["paperColor"]) == (
        "landscape",
        "cream",
    )
    converted = client.patch(path, json={"kind": "whiteboard", "template": "grid"})
    assert converted.json()["kind"] == "whiteboard"
    assert converted.json()["paperSize"] == "board"
    # A whiteboard stays on board paper whatever a patch says.
    assert client.patch(path, json={"paperSize": "a4"}).json()["paperSize"] == "board"
    summary = client.get("/api/paper").json()["notebooks"][0]
    assert (summary["kind"], summary["orientation"], summary["paperColor"]) == (
        "whiteboard",
        "landscape",
        "cream",
    )


# -- covers ------------------------------------------------------------------


def test_the_cover_is_the_first_page_and_a_cover_patch_is_ignored(state):
    client = make_client(state)
    nb = new_notebook(client, "Ideas")
    for page_id in (A, B, C):
        assert put_page(client, nb["path"], page_id, 0, drawing=b"x", render=PNG).status_code == 200
    path = f"/api/paper/notebooks/{nb['path']}"

    # Older clients still send `cover`; it is accepted, even unusable, and
    # changes nothing, not even the date or the seq.
    before = client.get(path).json()
    for cover in (C, "not a page id!"):
        response = client.patch(path, json={"cover": cover})
        assert response.status_code == 200, response.text
        assert response.json()["cover"] == A
        assert (response.json()["seq"], response.json()["modified"]) == (
            before["seq"],
            before["modified"],
        )
    # Beside another field, the other field lands and the cover is still A.
    starred = client.patch(path, json={"cover": B, "favorite": True}).json()
    assert starred["favorite"] is True and starred["cover"] == A

    client.patch(path, json={"pages": [C, A, B]})
    summary = client.get("/api/paper").json()["notebooks"][0]
    assert summary["cover"] == C
    assert f"/{C}/render.png" in summary["cover_url"]
    assert client.get(path).json()["cover"] == C

    client.delete(f"/api/paper/pages/{nb['path']}/{C}")
    assert client.get("/api/paper").json()["notebooks"][0]["cover"] == A


def test_a_cover_an_older_server_stored_reads_as_the_first_page(state):
    client = make_client(state)
    nb = new_notebook(client, "Ideas")
    for page_id in (A, B):
        put_page(client, nb["path"], page_id, 0, drawing=b"x", render=PNG)
    file = state.paper.dir / nb["path"] / "notebook.json"
    stored = json.loads(file.read_text())
    stored["cover"] = B
    file.write_text(json.dumps(stored))
    asyncio.run(state.paper_index.sync_all(force=True))

    assert client.get("/api/paper").json()["notebooks"][0]["cover"] == A
    assert client.get(f"/api/paper/notebooks/{nb['path']}").json()["cover"] == A
    row = asyncio.run(
        state.db.execute_fetchall("SELECT cover FROM paper_notebooks")
    )[0]
    assert row["cover"] == A


# -- boards ------------------------------------------------------------------


def test_a_board_reads_as_its_render_area_and_never_has_size_points(state):
    client = make_client(state)
    wb = new_whiteboard(client, paperColor="cream")
    page = save_board(client, wb, render=PNG, render_rect=RECT)

    assert page["size"] == {"width": 1366.0, "height": 1024.0}
    assert "size_points" not in page
    assert page["board_extent"] == EXTENT
    assert page["render_rect"] == RECT
    assert page["size_name"] == "board"
    # Every route a reader uses agrees.
    assert client.get(f"/api/paper/pages/{wb['path']}/{BOARD}").json() == page
    detail = client.get(f"/api/paper/notebooks/{wb['path']}").json()["pages_detail"]
    assert detail == [page]
    resolved = client.get(f"/api/paper/id/{wb['id']}/{BOARD}").json()["page"]
    assert resolved == page
    # On disk the page keeps what the device sent; only the reading changes.
    stored = page_json(state, wb["path"], BOARD)
    assert stored["size"] == EXTENT and stored["render_rect"] == RECT


def test_a_board_without_a_render_reads_as_its_extent(state):
    client = make_client(state)
    wb = new_whiteboard(client)
    small = {"width": 4096.0, "height": 2048.0}
    page = save_board(client, wb, page=board_part(board_extent=small, size=small))
    assert page["size"] == small
    assert page["board_extent"] == small
    # A rect sent with no render describes nothing and is not kept.
    page = save_board(client, wb, base=1, page=board_part(board_extent=small), render_rect=RECT)
    assert "render_rect" not in page and page["size"] == small


def test_a_render_without_a_rect_clears_the_old_one(state):
    client = make_client(state)
    wb = new_whiteboard(client)
    save_board(client, wb, render=PNG, render_rect=RECT)

    # The derived-only push: a render and no source.
    whole = put_page(client, wb["path"], BOARD, 1, render=OTHER_PNG, for_revision=1)
    assert whole.status_code == 200, whole.text
    assert "render_rect" not in whole.json()["page"]
    assert whole.json()["page"]["size"] == EXTENT

    framed = {"x": 0.0, "y": 0.0, "width": 2000.0, "height": 500.0}
    again = put_page(
        client, wb["path"], BOARD, 1, render=PNG, for_revision=1, render_rect=framed
    )
    assert again.json()["page"]["render_rect"] == framed
    assert again.json()["page"]["size"] == {"width": 2000.0, "height": 500.0}
    assert again.json()["revision"] == 1


def test_a_render_rect_sent_as_a_json_part_is_taken_too(state):
    client = make_client(state)
    wb = new_whiteboard(client)
    response = client.put(
        f"/api/paper/pages/{wb['path']}/{BOARD}",
        data={"base_revision": "0"},
        files={
            "page": ("page.json", json.dumps(board_part()), "application/json"),
            "render": ("render.png", PNG, "image/png"),
            "render_rect": ("render_rect.json", json.dumps(RECT), "application/json"),
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["page"]["render_rect"] == RECT


@pytest.mark.parametrize(
    "rect",
    [
        "not json",
        "[1, 2, 3, 4]",
        json.dumps({"x": 0, "y": 0, "width": 0, "height": 5}),
        json.dumps({"x": 0, "y": 0, "width": 5}),
        '{"x": NaN, "y": 0, "width": 5, "height": 5}',
    ],
)
def test_a_bad_render_rect_is_refused_before_anything_is_written(state, rect):
    client = make_client(state)
    wb = new_whiteboard(client)
    before = save_board(client, wb, render=PNG, render_rect=RECT)
    response = client.put(
        f"/api/paper/pages/{wb['path']}/{BOARD}",
        data={"base_revision": "1", "for_revision": "1", "render_rect": rect},
        files={"render": ("render.png", OTHER_PNG, "image/png")},
    )
    assert response.status_code == 400, response.text
    assert client.get(f"/api/paper/pages/{wb['path']}/{BOARD}").json() == before


def test_a_board_extent_is_held_to_its_own_range_not_the_page_cap(state):
    client = make_client(state)
    wb = new_whiteboard(client)
    largest = {"width": 32768.0, "height": 1024.0}
    page = save_board(
        client,
        wb,
        # An extent past the 20,000-point page cap, and a size_points that
        # cap would refuse: a board has none, so it is dropped, not checked.
        page=board_part(
            board_extent=largest,
            size=largest,
            size_points={"width": 30000.0, "height": 30000.0},
        ),
    )
    assert page["board_extent"] == largest and page["size"] == largest
    assert "size_points" not in page_json(state, wb["path"], BOARD)


def test_a_page_made_a_board_drops_its_size_points(state):
    client = make_client(state)
    nb = new_notebook(client, "Infinite", paperSize="infinite")
    grown = {"width": 1024.0, "height": 2048.0}
    put_page(
        client, nb["path"], A, 0,
        page={"size_name": "infinite", "size": grown, "size_points": grown},
        drawing=b"x",
    )
    # What a conversion sends: the page part has no size_points key at all.
    response = put_page(
        client, nb["path"], A, 1,
        page={"size_name": "board", "board_extent": EXTENT, "size": EXTENT, "template": "blank"},
    )
    assert response.status_code == 200, response.text
    stored = page_json(state, nb["path"], A)
    assert "size_points" not in stored and stored["board_extent"] == EXTENT
    assert response.json()["page"]["size"] == EXTENT


def test_pages_the_server_makes_follow_their_notebook(state):
    client = make_client(state)
    wb = new_whiteboard(client, paperColor="cream")
    made = client.post(f"/api/paper/notebooks/{wb['path']}/pages", json={"id": A}).json()
    assert (made["size_name"], made["template"], made["paper_color"]) == ("board", "grid", "cream")
    assert made["board_extent"] == EXTENT and "size_points" not in made
    # A save that creates a page with no page part is a board too.
    created = put_page(client, wb["path"], B, 0, drawing=b"x").json()["page"]
    assert created["size_name"] == "board" and created["board_extent"] == EXTENT

    landscape = new_notebook(client, "Wide", orientation="landscape", paperColor="dark")
    turned = client.post(f"/api/paper/notebooks/{landscape['path']}/pages", json={}).json()
    assert turned["size"] == turned["size_points"] == {"width": 842.0, "height": 595.0}
    assert (turned["size_name"], turned["paper_color"]) == ("a4", "dark")
    # A size the request names is the request's: never turned.
    named = client.post(
        f"/api/paper/notebooks/{landscape['path']}/pages", json={"size": "letter"}
    ).json()
    assert named["size"] == {"width": 612.0, "height": 792.0}
    assert "size_points" not in named


def test_a_conflict_on_a_board_parks_a_board(state):
    client = make_client(state)
    wb = new_whiteboard(client, paperColor="cream")
    save_board(client, wb, client_id="ipad-1")
    save_board(client, wb, base=1, drawing=b"newer", client_id="ipad-1")

    late = put_page(
        client, wb["path"], BOARD, 1,
        drawing=b"stale", render=PNG, render_rect=RECT, client_id="ipad-2",
    )
    assert late.status_code == 409, late.text
    sibling = late.json()["conflict"]["sibling"]
    assert sibling["size_name"] == "board"
    assert sibling["board_extent"] == EXTENT
    assert sibling["paper_color"] == "cream"
    assert sibling["render_rect"] == RECT
    public = client.get(f"/api/paper/pages/{wb['path']}/{sibling['id']}").json()
    assert public["size"] == {"width": 1366.0, "height": 1024.0}


# -- a board's whole life ----------------------------------------------------


def test_a_board_is_listed_pulled_and_in_the_feed(state):
    client = make_client(state)
    wb = new_whiteboard(client)
    save_board(client, wb, render=PNG, render_rect=RECT, text={"lines": [], "transcript": "launch plan"})

    summary = client.get("/api/paper").json()["notebooks"][0]
    assert (summary["kind"], summary["page_count"], summary["cover"]) == ("whiteboard", 1, BOARD)
    assert summary["cover_url"].startswith(f"/api/paper/blob/{wb['path']}/{BOARD}/render.png")
    assert client.get(summary["cover_url"]).content == PNG

    feed = client.get("/api/paper/changes").json()
    assert [row["id"] for row in feed["pages"]] == [BOARD]
    assert [nb["kind"] for nb in feed["notebooks"]] == ["whiteboard"]
    page = client.get(f"/api/paper/pages/{wb['path']}/{BOARD}").json()
    assert client.get(page["drawing_url"]).content == b"ink"

    hits = client.get("/api/paper/search?q=launch").json()["results"]
    assert [(hit["kind"], hit["title"]) for hit in hits] == [("whiteboard", "Board 1")]


def test_a_board_comes_back_from_the_trash_as_it_was(state):
    client = make_client(state)
    wb = new_whiteboard(client, paperColor="cream")
    before = save_board(client, wb, render=PNG, render_rect=RECT)
    item = client.delete(f"/api/paper/pages/{wb['path']}/{BOARD}").json()
    assert client.get(f"/api/paper/pages/{wb['path']}/{BOARD}").status_code == 404

    assert client.post(f"/api/paper/trash/{item['id']}/restore").status_code == 200
    after = client.get(f"/api/paper/pages/{wb['path']}/{BOARD}").json()
    moving = {"seq", "modified"}
    assert {k: v for k, v in after.items() if k not in moving} == {
        k: v for k, v in before.items() if k not in moving
    }
    assert after["size"] == {"width": 1366.0, "height": 1024.0}


def test_a_board_comes_back_from_page_history_with_its_own_frame(state):
    client = make_client(state)
    wb = new_whiteboard(client)
    save_board(client, wb, render=PNG, render_rect=RECT)
    asyncio.run(state.vault.commit_all("board: first"))
    later = {"x": 100.0, "y": 200.0, "width": 3000.0, "height": 1500.0}
    save_board(client, wb, base=1, drawing=b"more", render=OTHER_PNG, render_rect=later)
    asyncio.run(state.vault.commit_all("board: second"))

    history = client.get(f"/api/paper/history/{wb['path']}/{BOARD}").json()
    oldest = history["versions"][-1]["sha"]
    restored = client.post(
        f"/api/paper/history/{wb['path']}/{BOARD}/restore", json={"sha": oldest}
    )
    assert restored.status_code == 200, restored.text
    page = restored.json()["page"]
    assert restored.json()["revision"] == 3
    assert page["render_rect"] == RECT
    assert page["size"] == {"width": 1366.0, "height": 1024.0}
    assert page["board_extent"] == EXTENT and page["size_name"] == "board"
    assert client.get(page["render_url"]).content == PNG


# -- index -------------------------------------------------------------------


def test_the_index_knows_whiteboards_and_names_untitled_boards(state):
    client = make_client(state)
    nb = new_notebook(client, "Ideas")
    put_page(client, nb["path"], A, 0, drawing=b"x", text={"lines": [], "transcript": "launch notes"})
    wb = new_whiteboard(client)
    save_board(client, wb, text={"lines": [], "transcript": "launch board"})
    titled = put_page(
        client, wb["path"], B, 0, page=board_part(title="Roadmap"), drawing=b"x",
        text={"lines": [], "transcript": "launch roadmap"},
    )
    assert titled.status_code == 200, titled.text

    async def read():
        notebooks = await state.db.execute_fetchall(
            "SELECT path, kind FROM paper_notebooks ORDER BY path"
        )
        pages = await state.db.execute_fetchall(
            "SELECT page_id, title FROM paper_pages ORDER BY page_id"
        )
        return [tuple(row) for row in notebooks], [tuple(row) for row in pages]

    notebooks, pages = asyncio.run(read())
    assert notebooks == [(nb["path"], "notebook"), (wb["path"], "whiteboard")]
    assert pages == [(A, "Page 1"), (B, "Roadmap"), (BOARD, "Board 1")]
    hits = asyncio.run(state.paper_index.search("launch", limit=10))
    assert sorted((hit["kind"], hit["title"]) for hit in hits) == [
        ("notebook", "Page 1"),
        ("whiteboard", "Board 1"),
        ("whiteboard", "Roadmap"),
    ]
    # A rebuild from disk derives the same.
    asyncio.run(state.paper_index.sync_all(force=True))
    assert asyncio.run(read()) == (notebooks, pages)


def test_an_index_from_before_kind_gains_the_column(tmp_path):
    index = tmp_path / "index"
    index.mkdir()
    with sqlite3.connect(index / "metadata.db") as db:
        db.execute(
            """CREATE TABLE paper_notebooks (
                   path TEXT PRIMARY KEY, id TEXT, title TEXT NOT NULL, folder TEXT,
                   template TEXT, paper_size TEXT, cover TEXT, tags TEXT,
                   favorite INTEGER NOT NULL DEFAULT 0, created TEXT, modified TEXT,
                   page_count INTEGER NOT NULL DEFAULT 0, seq INTEGER NOT NULL DEFAULT 0)"""
        )
        db.execute("INSERT INTO paper_notebooks(path, title) VALUES ('paper/A.paper', 'A')")

    async def reopen():
        db = await open_db(index)
        try:
            return [tuple(row) for row in await db.execute_fetchall(
                "SELECT path, kind FROM paper_notebooks"
            )]
        finally:
            await db.close()

    assert asyncio.run(reopen()) == [("paper/A.paper", "notebook")]


# -- OMNIA -------------------------------------------------------------------


def _library(state):
    paper = state.paper
    paper.create_folder(parent="paper", name="Work", color="red")
    paper.create_folder(parent="paper/Work", name="Accounting", color="orange")
    paper.create_notebook(title="Ledger", folder="paper/Work/Accounting")
    paper.create_notebook(title="Plans", folder="paper/Work", kind="whiteboard")
    paper.create_notebook(title="Loose")


def test_list_notebooks_reports_kinds_folder_names_and_folder_records(state):
    _library(state)
    out = run_tool(state, "list_notebooks", {})
    rows = {nb["title"]: nb for nb in out["notebooks"]}
    assert rows["Plans"]["kind"] == "whiteboard"
    assert rows["Ledger"]["kind"] == "notebook"
    assert rows["Ledger"]["folder_name"] == "Accounting"
    assert rows["Loose"]["folder_name"] is None
    assert out["folders"] == [
        {"path": "paper/Work", "name": "Work", "color": "red", "favorite": False},
        {"path": "paper/Work/Accounting", "name": "Accounting", "color": "orange", "favorite": False},
    ]


@pytest.mark.parametrize("folder", ["accounting", "ACCOUNTING", "paper/work/accounting", "Work/Accounting"])
def test_list_notebooks_finds_a_folder_by_path_or_name_in_any_case(state, folder):
    _library(state)
    out = run_tool(state, "list_notebooks", {"folder": folder})
    assert [nb["title"] for nb in out["notebooks"]] == ["Ledger"]
    assert "note" not in out


def test_list_notebooks_in_an_unknown_folder_says_there_is_no_such_folder(state):
    _library(state)
    out = run_tool(state, "list_notebooks", {"folder": "Receipts"})
    assert out["notebooks"] == []
    assert "no Paper folder 'paper/Receipts'" in out["note"]
    assert "paper/Work/Accounting" in out["note"]


def test_open_takes_a_folder_by_name_and_opens_the_library_there(acting_state):
    _library(acting_state)
    result = asyncio.run(open_in_app(acting_state, "t", {"paper": "accounting"}))
    assert result["_action"]["navigate"] == "/paper?folder=paper%2FWork%2FAccounting"
    by_path = asyncio.run(open_in_app(acting_state, "t", {"paper": "paper/Work"}))
    assert by_path["_action"]["navigate"] == "/paper?folder=paper%2FWork"
    # A notebook named exactly still wins over a folder.
    notebook = asyncio.run(open_in_app(acting_state, "t", {"paper": "Plans"}))
    assert notebook["_action"]["navigate"] == "/paper/Work/Plans.paper"


PDF_PAGE = "b" * 32


def test_a_converted_pdf_page_clears_its_binding_and_an_older_client_keeps_it(state):
    """Convert to Whiteboard turns a PDF page into a board whose picture of
    the page is an image object, and sends ``"pdf": null`` with it: the page
    patch keeps every key it is not sent, so without the explicit clear the
    server's board went on naming the PDF and every device that fetched it
    got both. A page part with no ``pdf`` key -- the iPad build on main --
    still leaves the binding alone."""
    client = make_client(state)
    notebook = new_notebook(client, "Slides")
    path = notebook["path"]
    binding = {"asset": "assets/slides-0123456789abcdef.pdf", "pageIndex": 0}
    sheet = {
        "template": "blank",
        "size": {"width": 595.0, "height": 842.0},
        "size_name": "a4",
        "objects": [],
        "bookmarked": False,
        "paper_color": "white",
        "pdf": binding,
    }
    response = put_page(client, path, PDF_PAGE, 0, page=sheet, drawing=b"ink")
    assert response.status_code == 200, response.text
    revision = response.json()["revision"]

    # An older client's metadata-only save names no binding: it stays.
    retitled = {**sheet, "title": "Week 1"}
    retitled.pop("pdf")
    response = put_page(client, path, PDF_PAGE, revision, page=retitled, drawing=b"ink")
    assert response.status_code == 200, response.text
    revision = response.json()["revision"]
    assert client.get(f"/api/paper/pages/{path}/{PDF_PAGE}").json()["pdf"] == binding

    # The board, as the iPad sends it now.
    board = {
        "template": "blank",
        "size": {"width": 16384.0, "height": 16384.0},
        "size_name": "board",
        "board_extent": {"width": 16384.0, "height": 16384.0},
        "objects": [
            {
                "id": "c" * 32,
                "type": "image",
                "frame": {"x": 7894.5, "y": 7771.0, "width": 595.0, "height": 842.0},
                "asset": "assets/page-1-0123456789abcdef.png",
            }
        ],
        "bookmarked": False,
        "paper_color": "white",
        "title": "Board 1",
        "pdf": None,
    }
    response = put_page(client, path, PDF_PAGE, revision, page=board, drawing=b"moved ink")
    assert response.status_code == 200, response.text

    page = client.get(f"/api/paper/pages/{path}/{PDF_PAGE}").json()
    assert page["size_name"] == "board"
    assert page["objects"][0]["type"] == "image"
    assert "pdf" not in page or page["pdf"] is None
    stored = json.loads((state.paper.dir / path / "pages" / PDF_PAGE / "page.json").read_text())
    assert "pdf" not in stored
