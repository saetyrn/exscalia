"""OMNIA Round 3 — navigation. `open` moves the user to a note or view;
`close_tabs` closes editor tabs. Both change no data (undoable=False)."""
import asyncio

import pytest

from app.overseer.tool_expansion import close_tabs, open_in_app
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


def test_open_unknown_view_errors(acting_state):
    with pytest.raises(NoteError):
        asyncio.run(open_in_app(acting_state, "t", {"view": "nope"}))


def test_open_requires_a_target(acting_state):
    with pytest.raises(NoteError):
        asyncio.run(open_in_app(acting_state, "t", {}))


def test_open_unknown_note_errors(acting_state):
    with pytest.raises(NoteError):
        asyncio.run(open_in_app(acting_state, "t", {"note": "does-not-exist"}))


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
