"""OMNIA Round 3 — navigation. `open` moves the user to a note or view;
`close_tabs` closes editor tabs. Both change no data (undoable=False)."""
import asyncio
import re
from urllib.parse import unquote

import pytest

from app.index.db import content_hash
from app.overseer.agent import SYSTEM_PROMPT
from app.overseer.tool_expansion import (
    archive_note,
    close_tabs,
    create_note,
    create_task_board,
    duplicate_note,
    open_in_app,
    rename_note,
    scaffold_project,
)
from app.vault.notes import NoteError


def note(title: str) -> str:
    return f'---\ntitle: "{title}"\n---\n\n# {title}\n'


def add_note(state, path: str, title: str) -> None:
    created = state.store.create(path, note(title))
    asyncio.run(state.indexer.index_note(path, created))


# --- open ------------------------------------------------------------------


def test_open_view(acting_state):
    result = asyncio.run(open_in_app(acting_state, "t", {"view": "calendar"}))
    assert result["_action"]["navigate"] == "/calendar"


def test_open_note_by_exact_filename(acting_state):
    add_note(acting_state, "groceries.md", "Groceries")
    result = asyncio.run(open_in_app(acting_state, "t", {"note": "groceries"}))
    assert result["_action"]["navigate"] == "/note/groceries.md"


def test_open_note_by_partial_title(acting_state):
    acting_state.store.create_folder("projects")
    add_note(acting_state, "projects/Thesis Plan.md", "Thesis Plan")
    result = asyncio.run(open_in_app(acting_state, "t", {"note": "thesis"}))
    assert result["_action"]["navigate"] == "/note/projects/Thesis Plan.md"


@pytest.mark.parametrize(
    ("view", "route"),
    [
        ("notes", "/notes"),
        ("paper", "/paper"),
        ("history", "/history/omnia"),
        ("media", "/media"),
        ("recipes", "/recipes"),
    ],
)
def test_open_reaches_every_top_level_view(acting_state, view, route):
    result = asyncio.run(open_in_app(acting_state, "t", {"view": view}))
    assert result["_action"]["navigate"] == route


@pytest.mark.parametrize("view", ["tags", "study"])
def test_views_without_an_index_route_are_not_offered(acting_state, view):
    # /tags and /study only exist with a tag or deck after them; a bare route
    # would land on the app's 404.
    with pytest.raises(NoteError) as err:
        asyncio.run(open_in_app(acting_state, "t", {"view": view}))
    assert err.value.status == 400


def test_open_history_of_a_note(acting_state):
    acting_state.store.create_folder("projects")
    add_note(acting_state, "projects/Thesis Plan.md", "Thesis Plan")
    result = asyncio.run(open_in_app(acting_state, "t", {"view": "history", "note": "thesis"}))
    assert result["_action"]["navigate"] == "/history?path=projects%2FThesis%20Plan.md"


def test_open_folder_board_canonicalizes_the_folder(acting_state):
    acting_state.store.create_folder("School")
    asyncio.run(create_task_board(acting_state, "t", {"folder": "School"}))
    result = asyncio.run(open_in_app(acting_state, "t", {"folder": "school"}))
    assert result["_action"]["navigate"] == "/tasks/School"
    # A folder beats a generic view passed alongside it.
    both = asyncio.run(open_in_app(acting_state, "t", {"view": "tasks", "folder": "School/"}))
    assert both["_action"]["navigate"] == "/tasks/School"


def test_open_folder_without_a_board_says_so(acting_state):
    acting_state.store.create_folder("Errands")
    with pytest.raises(NoteError) as err:
        asyncio.run(open_in_app(acting_state, "t", {"folder": "errands"}))
    assert err.value.status == 404
    assert "no task board" in str(err.value.detail)
    with pytest.raises(NoteError):
        asyncio.run(open_in_app(acting_state, "t", {"folder": "nowhere"}))


def test_open_query_lands_on_search_results(acting_state):
    result = asyncio.run(open_in_app(acting_state, "t", {"query": "thermo dynamics"}))
    assert result["_action"]["navigate"] == "/search?q=thermo%20dynamics"


def test_open_tag_uses_its_stored_spelling(acting_state):
    created = acting_state.store.create("lab.md", "# Lab\n\nNotes #Physics\n")
    asyncio.run(acting_state.indexer.index_note("lab.md", created))
    result = asyncio.run(open_in_app(acting_state, "t", {"tag": "#physics"}))
    assert result["_action"]["navigate"] == "/tags/Physics"
    with pytest.raises(NoteError):
        asyncio.run(open_in_app(acting_state, "t", {"tag": "chemistry"}))


# paperurl.ts notebookHref: /paper/<route path, each segment encodeURIComponent'd>.
NOTEBOOK_ROUTE = re.compile(r"^/paper/(?:[A-Za-z0-9\-_.!~*'()]|%[0-9A-F]{2})+(?:/(?:[A-Za-z0-9\-_.!~*'()]|%[0-9A-F]{2})+)*$")


def test_open_paper_notebook_by_title(acting_state):
    made = acting_state.paper.create_notebook(title="Thermo Lectures", folder="School")
    result = asyncio.run(open_in_app(acting_state, "t", {"paper": "thermo"}))
    route = result["_action"]["navigate"]
    assert NOTEBOOK_ROUTE.match(route), route
    # Decoding the route param and restoring the root gives the vault path.
    assert "paper/" + unquote(route.removeprefix("/paper/")) == made["path"]
    by_path = asyncio.run(open_in_app(acting_state, "t", {"paper": made["path"]}))
    assert by_path["_action"]["navigate"] == route


def test_open_paper_page_uses_the_durable_link(acting_state):
    made = acting_state.paper.create_notebook(title="Ideas")
    page = acting_state.paper.create_page(made["path"])
    page_dir = f"{made['path']}/pages/{page['id']}"
    result = asyncio.run(open_in_app(acting_state, "t", {"paper": page_dir}))
    assert result["_action"]["navigate"] == f"/paper/id/{made['id']}/{page['id']}"
    assert result["page"] == page["id"]


def test_open_unknown_paper_notebook_errors(acting_state):
    with pytest.raises(NoteError):
        asyncio.run(open_in_app(acting_state, "t", {"paper": "nothing here"}))


def test_open_unknown_view_errors(acting_state):
    with pytest.raises(NoteError):
        asyncio.run(open_in_app(acting_state, "t", {"view": "nope"}))


def test_open_requires_a_target(acting_state):
    with pytest.raises(NoteError):
        asyncio.run(open_in_app(acting_state, "t", {}))


def test_open_unknown_note_errors(acting_state):
    with pytest.raises(NoteError):
        asyncio.run(open_in_app(acting_state, "t", {"note": "does-not-exist"}))


# --- navigate is always a route -------------------------------------------
# ChatPanel goto()s `navigate` verbatim; a bare vault path resolved against the
# page the user was on (from /note/a/b.md, "c.md" became /note/a/c.md).


def test_note_tools_navigate_to_note_routes(acting_state):
    body = '---\ntitle: "Old"\n---\n\n# Old\n'
    acting_state.store.create_folder("projects")
    asyncio.run(create_note(acting_state, "t", {"path": "projects/Old.md", "content": body}))
    dup = asyncio.run(duplicate_note(acting_state, "t", {"path": "projects/Old.md"}))
    assert dup["_action"]["navigate"] == f"/note/{dup['path']}"

    revision = content_hash(acting_state.store.read("projects/Old.md"))
    renamed = asyncio.run(
        rename_note(acting_state, "t", {"path": "projects/Old.md", "new_title": "New", "base_revision": revision})
    )
    assert renamed["_action"]["navigate"] == "/note/projects/New.md"

    archived = asyncio.run(archive_note(acting_state, "t", {"path": "projects/New.md"}))
    assert archived["_action"]["navigate"] == "/note/archive/New.md"


def test_board_tools_navigate_to_the_board_view(acting_state):
    acting_state.store.create_folder("School")
    made = asyncio.run(create_task_board(acting_state, "t", {"folder": "school"}))
    assert made["_action"]["navigate"] == "/tasks/School"

    project = asyncio.run(scaffold_project(acting_state, "t", {"name": "Thesis"}))
    assert project["_action"]["navigate"] == "/tasks/Thesis"


# --- the app guide in the system prompt -------------------------------------


def test_app_guide_describes_the_current_ui():
    # The Read/Formatted/Raw cycle and its toggle were removed on Aug 25.
    assert "\u00b6" not in SYSTEM_PROMPT
    assert "Raw shows" not in SYSTEM_PROMPT
    assert "Tags (the default)" in SYSTEM_PROMPT
    assert "Pin, History, Rename, Delete" in SYSTEM_PROMPT
    assert "Cmd/Ctrl+J" in SYSTEM_PROMPT


# --- close_tabs ------------------------------------------------------------


def test_close_tabs_others(acting_state):
    result = asyncio.run(close_tabs(acting_state, "t", {"others": True}))
    assert result["_action"]["close_tabs"] == {"match": None, "others": True}


def test_close_tabs_by_match(acting_state):
    result = asyncio.run(close_tabs(acting_state, "t", {"match": "scratch"}))
    assert result["_action"]["close_tabs"] == {"match": "scratch", "others": False}


def test_close_tabs_requires_a_criterion(acting_state):
    with pytest.raises(NoteError):
        asyncio.run(close_tabs(acting_state, "t", {}))
