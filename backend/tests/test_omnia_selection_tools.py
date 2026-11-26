"""OMNIA Round 3 — selection-native editing. The frontend persists the editor
selection's offsets onto the user message; these tools act on that exact range
(with an old_string fallback when it's stale)."""
import asyncio
from types import SimpleNamespace

import pytest

from app.overseer.tool_expansion import edit_selection, extract_to_note, format_selection
from app.task_service import create_task
from app.vault.notes import NoteError


def setup_note(state, path: str, content: str) -> str:
    state.store.create(path, content)
    stored = state.store.read(path)
    asyncio.run(state.indexer.index_note(path, stored))
    return stored


def utf16(text: str) -> int:
    """CodeMirror's length for `text`: UTF-16 code units."""
    return len(text.encode("utf-16-le")) // 2


def select(state, path: str, stored: str, frm: int, to: int) -> None:
    """Stub the thread so _selection() finds a persisted range for this note,
    given str indexes; the editor sends UTF-16 offsets, so convert them."""
    message = {
        "role": "user",
        "selection_range": {
            "from": utf16(stored[:frm]),
            "to": utf16(stored[:to]),
            "head": utf16(stored[:to]),
            "doc_len": utf16(stored),
            "path": path,
        },
    }
    state.threads = SimpleNamespace(get=lambda _tid: {"messages": [message]})


NOTE = '---\ntitle: "N"\n---\n\n# N\n\n'
# The due marker the app writes (U+1F4C5) is two UTF-16 units, one str char.
DUE_TASK = "- [ ] pay rent \U0001F4C5 2026-01-01\n"


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


def test_edit_selection_counts_utf16_offsets_past_a_due_marker(acting_state):
    stored = setup_note(acting_state, "n.md", NOTE + DUE_TASK + "the old text here\n")
    frm = stored.index("old text")
    select(acting_state, "n.md", stored, frm, frm + len("old text"))

    asyncio.run(edit_selection(acting_state, "t", {"new_string": "new words"}))

    body = acting_state.store.read("n.md")
    assert "the new words here" in body and "old text" not in body
    assert "pay rent \U0001F4C5 2026-01-01" in body


def test_edit_selection_inserts_at_cursor_past_a_due_marker(acting_state):
    # A cursor insert has no old_string to fall back on: the live range is
    # the only way it can work.
    stored = setup_note(acting_state, "n.md", NOTE + DUE_TASK + "AB\n")
    pos = stored.index("AB") + 1
    select(acting_state, "n.md", stored, pos, pos)

    asyncio.run(edit_selection(acting_state, "t", {"new_string": "X"}))

    assert "AXB" in acting_state.store.read("n.md")


def test_edit_selection_distrusts_an_offset_inside_a_surrogate_pair(acting_state):
    stored = setup_note(acting_state, "n.md", NOTE + DUE_TASK)
    mid = utf16(stored[: stored.index("\U0001F4C5")]) + 1
    message = {
        "role": "user",
        "selection_range": {"from": mid, "to": mid, "head": mid, "doc_len": utf16(stored), "path": "n.md"},
    }
    acting_state.threads = SimpleNamespace(get=lambda _tid: {"messages": [message]})

    with pytest.raises(NoteError) as err:
        asyncio.run(edit_selection(acting_state, "t", {"new_string": "X"}))

    assert err.value.status == 409
    assert acting_state.store.read("n.md") == stored


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


def test_format_selection_heading_replaces_an_existing_heading(acting_state):
    stored = setup_note(acting_state, "n.md", NOTE + "# Old heading\n### Deep\n#tagged line\n")
    frm = stored.index("Old heading")
    to = stored.index("tagged line")
    select(acting_state, "n.md", stored, frm, to)

    asyncio.run(format_selection(acting_state, "t", {"style": "heading", "level": 2}))

    body = acting_state.store.read("n.md")
    assert "\n## Old heading\n## Deep\n## #tagged line\n" in body


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


def test_format_selection_task_keeps_leading_letters(acting_state):
    lines = "- xylophone practice\n- [x] x-ray results\n* [ ] -5 degrees\n"
    stored = setup_note(acting_state, "n.md", NOTE + lines)
    frm = stored.index("- xylophone")
    select(acting_state, "n.md", stored, frm, frm + len(lines) - 1)

    asyncio.run(format_selection(acting_state, "t", {"style": "task"}))

    body = acting_state.store.read("n.md")
    for text in ("xylophone practice", "x-ray results", "-5 degrees"):
        assert f"- [ ] {text}" in body


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


def test_extract_to_note_links_the_note_it_made(acting_state):
    setup_note(acting_state, "Ideas.md", NOTE + "already here\n")
    stored = setup_note(acting_state, "n.md", NOTE + "A fresh idea.\n")
    frm = stored.index("A fresh idea.")
    select(acting_state, "n.md", stored, frm, frm + len("A fresh idea."))

    result = asyncio.run(extract_to_note(acting_state, "t", {"title": "Ideas!"}))

    assert result["path"] == "Ideas 2.md"
    assert "A fresh idea." in acting_state.store.read("Ideas 2.md")
    assert "[[Ideas 2]]" in acting_state.store.read("n.md")
    assert "already here" in acting_state.store.read("Ideas.md")


def test_extract_to_note_keeps_an_extracted_task_indexed(acting_state):
    # The new note used to be indexed while the source still held the task
    # id, so the index skipped it and the source's rewrite tombstoned it.
    setup_note(acting_state, "n.md", NOTE + "Intro.\n")
    task = asyncio.run(create_task(acting_state, text="Pack the bags", path="n.md"))
    stored = acting_state.store.read("n.md")
    frm = stored.index("<!--")
    to = stored.index("Pack the bags") + len("Pack the bags")
    select(acting_state, "n.md", stored, frm, to)

    asyncio.run(extract_to_note(acting_state, "t", {"title": "Packing"}))

    # The seeded Welcome note carries a real task; only the tests' own rows matter here.
    rows = asyncio.run(
        acting_state.db.execute_fetchall("SELECT id, path FROM tasks WHERE id NOT LIKE 'legacy:%' AND path != 'Welcome.md'")
    )
    assert [(row["id"], row["path"]) for row in rows] == [(task["id"], "Packing.md")]
    assert acting_state.task_tombstones.all() == {}


def test_extract_to_note_refuses_a_title_it_cannot_name_before_cutting(acting_state):
    stored = setup_note(acting_state, "n.md", NOTE + "Keep this text.\n")
    sel = "Keep this text."
    frm = stored.index(sel)
    select(acting_state, "n.md", stored, frm, frm + len(sel))

    with pytest.raises(NoteError):
        asyncio.run(extract_to_note(acting_state, "t", {"title": "Overlong " * 30}))

    assert acting_state.store.read("n.md") == stored
