"""OMNIA Round 3 — selection-native editing. The frontend persists the editor
selection's offsets onto the user message; these tools act on that exact range
(with an old_string fallback when it's stale)."""
import asyncio
from types import SimpleNamespace

from app.overseer.tool_expansion import edit_selection, extract_to_note, format_selection


def setup_note(state, path: str, content: str) -> str:
    state.store.create(path, content)
    stored = state.store.read(path)
    asyncio.run(state.indexer.index_note(path, stored))
    return stored


def select(state, path: str, stored: str, frm: int, to: int) -> None:
    """Stub the thread so _selection() finds a persisted range for this note."""
    message = {
        "role": "user",
        "selection_range": {"from": frm, "to": to, "head": to, "doc_len": len(stored), "path": path},
    }
    state.threads = SimpleNamespace(get=lambda _tid: {"messages": [message]})


NOTE = '---\ntitle: "N"\n---\n\n# N\n\n'


# --- edit_selection --------------------------------------------------------


def test_edit_selection_replaces_the_live_range(acting_state):
    stored = setup_note(acting_state, "n.md", NOTE + "the old text here\n")
    frm = stored.index("old text")
    select(acting_state, "n.md", stored, frm, frm + len("old text"))

    asyncio.run(edit_selection(acting_state, "t", {"new_string": "new words"}))

    body = acting_state.store.read("n.md")
    assert "new words" in body and "old text" not in body


def test_edit_selection_inserts_at_cursor(acting_state):
    stored = setup_note(acting_state, "n.md", NOTE + "AB\n")
    pos = stored.index("AB") + 1  # empty range between A and B
    select(acting_state, "n.md", stored, pos, pos)

    asyncio.run(edit_selection(acting_state, "t", {"new_string": "X"}))

    assert "AXB" in acting_state.store.read("n.md")


def test_edit_selection_falls_back_to_old_string_when_stale(acting_state):
    setup_note(acting_state, "n.md", NOTE + "findme\n")
    # A stale range (wrong doc_len) forces the old_string fallback.
    message = {
        "role": "user",
        "selection_range": {"from": 0, "to": 3, "head": 3, "doc_len": 999999, "path": "n.md"},
    }
    acting_state.threads = SimpleNamespace(get=lambda _tid: {"messages": [message]})

    asyncio.run(
        edit_selection(acting_state, "t", {"new_string": "gotcha", "old_string": "findme", "path": "n.md"})
    )

    assert "gotcha" in acting_state.store.read("n.md")


# --- format_selection ------------------------------------------------------


def test_format_selection_heading(acting_state):
    stored = setup_note(acting_state, "n.md", NOTE + "My Section\n")
    frm = stored.index("My Section")
    select(acting_state, "n.md", stored, frm, frm + len("My Section"))

    asyncio.run(format_selection(acting_state, "t", {"style": "heading", "level": 2}))

    assert "## My Section" in acting_state.store.read("n.md")


def test_format_selection_bold(acting_state):
    stored = setup_note(acting_state, "n.md", NOTE + "important\n")
    frm = stored.index("important")
    select(acting_state, "n.md", stored, frm, frm + len("important"))

    asyncio.run(format_selection(acting_state, "t", {"style": "bold"}))

    assert "**important**" in acting_state.store.read("n.md")


def test_format_selection_task_list(acting_state):
    stored = setup_note(acting_state, "n.md", NOTE + "buy milk\nwalk dog\n")
    frm = stored.index("buy milk")
    to = stored.index("walk dog") + len("walk dog")
    select(acting_state, "n.md", stored, frm, to)

    asyncio.run(format_selection(acting_state, "t", {"style": "task"}))

    body = acting_state.store.read("n.md")
    assert "- [ ] buy milk" in body and "- [ ] walk dog" in body


# --- extract_to_note -------------------------------------------------------


def test_extract_to_note(acting_state):
    stored = setup_note(acting_state, "n.md", NOTE + "Extract this paragraph into its own note.\n")
    sel = "Extract this paragraph into its own note."
    frm = stored.index(sel)
    select(acting_state, "n.md", stored, frm, frm + len(sel))

    result = asyncio.run(extract_to_note(acting_state, "t", {"title": "Extracted"}))

    assert result["path"] == "Extracted.md"
    assert "Extract this paragraph" in acting_state.store.read("Extracted.md")
    assert "[[Extracted]]" in acting_state.store.read("n.md")
