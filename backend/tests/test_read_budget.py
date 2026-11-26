"""The readers that can return a lot stay inside one tool result.

agent._clip cuts every tool result at MAX_TOOL_RESULT_CHARS. read_structure's
default page and read_notes' budget both used to run past it: the page header
still said `returned: 300`, so paging by it skipped the notes the clip cut, and
a note longer than the clip lost the `revision` serialized after its content,
which left every revision-gated edit tool out of reach for that note.
"""

import asyncio
import json

import pytest

from app.index.db import content_hash
from app.overseer import agent, tools
from app.overseer.tool_expansion import get_outline, patch_note
from app.overseer.tools import execute_tool, read_notes, read_structure


def _note(body: str, title: str) -> str:
    return f"---\ntitle: {title}\n---\n\n{body}\n"


def _seed_courses(state, count: int) -> list[str]:
    """Nested course paths and two tags each: the ~230-char entries that
    overflowed a default page the most."""
    paths = []
    for i in range(count):
        path = (
            f"courses/2026-fall/computational-neuroscience/week-{i % 13:02d}/"
            f"lecture-notes-on-population-coding-and-dynamics-{i:03d}.md"
        )
        title = f"Lecture notes on population coding and dynamics, part {i:03d}"
        state.store.create(path, _note(f"Body {i}. #coursework #neuro-{i % 7}", title))
        paths.append(path)
    asyncio.run(state.indexer.sync_all(force=True))
    return paths


def _dumped(result: dict) -> str:
    return json.dumps(result, ensure_ascii=False)


def _long_body(size: int) -> str:
    # Newlines and quotes serialize to two characters each, so the raw length
    # understates what the model is sent.
    line = 'Paragraph with "quoted" words, a tab\tand an accent: café.\n'
    return (line * (size // len(line) + 1))[:size]


def test_budget_stays_below_the_agent_clip():
    # tools.py cannot import agent (agent imports tools), so the relation that
    # makes the budget meaningful is pinned here.
    assert tools.MAX_READ_CHARS < tools.TOOL_RESULT_BUDGET < agent.MAX_TOOL_RESULT_CHARS


def test_default_read_structure_page_fits_one_tool_result(state):
    _seed_courses(state, 400)

    page = asyncio.run(read_structure(state, "s", {}))

    assert len(_dumped(page)) <= agent.MAX_TOOL_RESULT_CHARS
    assert page["returned"] == len(page["notes"])
    assert page["more_available"] is True
    assert page["next_offset"] == page["returned"]
    # What the agent loop hands the model is the whole page, not a clipped one.
    output = asyncio.run(execute_tool(state, "s", "read_structure", {}))
    assert agent._clip(output) == output


def test_read_structure_trims_an_oversized_limit(state):
    _seed_courses(state, 400)

    page = asyncio.run(read_structure(state, "s", {"limit": 1000}))

    assert len(_dumped(page)) <= agent.MAX_TOOL_RESULT_CHARS
    assert page["returned"] == len(page["notes"]) < 400
    assert page["next_offset"] == page["returned"]


def test_paging_by_next_offset_visits_every_note_once(state):
    _seed_courses(state, 400)
    total = asyncio.run(read_structure(state, "s", {}))["note_count"]

    seen: list[str] = []
    offset = 0
    for _ in range(50):
        page = asyncio.run(read_structure(state, "s", {"offset": offset, "limit": 1000}))
        assert len(_dumped(page)) <= agent.MAX_TOOL_RESULT_CHARS
        seen += [n["path"] for n in page["notes"]]
        if not page["more_available"]:
            assert page["next_offset"] is None
            break
        offset = page["next_offset"]

    assert len(seen) == len(set(seen)) == total


def test_read_notes_pages_a_long_note_with_its_revision_first(state):
    content = _note(_long_body(40_000), "Long")
    state.store.create("long.md", content)
    revision = content_hash(state.store.read("long.md"))

    first = asyncio.run(read_notes(state, "s", {"paths": ["long.md"]}))["notes"][0]

    assert list(first)[:2] == ["path", "revision"]
    assert first["revision"] == revision
    assert first["truncated"] is True
    assert first["total_chars"] == len(state.store.read("long.md"))
    assert first["next_offset"] == len(first["content"])

    parts = [first["content"]]
    offset = first["next_offset"]
    while offset is not None:
        output = asyncio.run(
            execute_tool(state, "s", "read_notes", {"paths": ["long.md"], "offset": offset})
        )
        assert agent._clip(output) == output
        entry = json.loads(output)["notes"][0]
        assert entry["revision"] == revision
        assert entry["offset"] == offset
        parts.append(entry["content"])
        offset = entry["next_offset"]

    assert "".join(parts) == state.store.read("long.md")
    assert entry["truncated"] is False


def test_single_note_past_the_old_budget_is_sliced_not_refused(state):
    state.store.create("huge.md", _note(_long_body(80_000), "Huge"))

    output = asyncio.run(execute_tool(state, "s", "read_notes", {"paths": ["huge.md"]}))

    assert agent._clip(output) == output
    entry = json.loads(output)["notes"][0]
    assert "error" not in entry
    assert entry["truncated"] is True
    assert entry["revision"] == content_hash(state.store.read("huge.md"))


def test_max_chars_caps_a_slice(state):
    state.store.create("long.md", _note(_long_body(10_000), "Long"))
    content = state.store.read("long.md")

    entry = asyncio.run(
        read_notes(state, "s", {"paths": ["long.md"], "offset": 100, "max_chars": 500})
    )["notes"][0]

    assert entry["content"] == content[100:600]
    assert entry["next_offset"] == 600


def test_short_note_reads_whole_without_slice_fields(state):
    state.store.create("short.md", _note("Just a line.", "Short"))

    entry = asyncio.run(read_notes(state, "s", {"paths": ["short.md"]}))["notes"][0]

    assert entry == {
        "path": "short.md",
        "revision": content_hash(state.store.read("short.md")),
        "content": state.store.read("short.md"),
    }


def test_offset_past_the_end_is_an_error(state):
    state.store.create("short.md", _note("Just a line.", "Short"))

    entry = asyncio.run(
        read_notes(state, "s", {"paths": ["short.md"], "offset": 10_000})
    )["notes"][0]

    assert "past the end" in entry["error"]


def test_several_notes_share_the_budget(state):
    state.store.create("a.md", _note(_long_body(15_000), "A"))
    state.store.create("b.md", _note(_long_body(15_000), "B"))
    state.store.create("c.md", _note("Small.", "C"))
    state.store.create("big.md", _note(_long_body(40_000), "Big"))

    output = asyncio.run(
        execute_tool(state, "s", "read_notes", {"paths": ["a.md", "b.md", "big.md", "c.md"]})
    )

    assert agent._clip(output) == output
    a, b, big, c = json.loads(output)["notes"]
    assert a["revision"] == content_hash(state.store.read("a.md"))
    assert "budget" in b["error"]
    assert "on its own" in big["error"]
    # A later note that still fits is read rather than dropped with the rest.
    assert c["content"] == state.store.read("c.md")


def test_slicing_arguments_need_a_single_path(state):
    state.store.create("a.md", _note("A.", "A"))
    state.store.create("b.md", _note("B.", "B"))

    result = asyncio.run(read_notes(state, "s", {"paths": ["a.md", "b.md"], "offset": 5}))

    assert "single path" in result["error"]


def test_a_sliced_read_unlocks_revision_gated_edits(acting_state):
    body = _long_body(40_000) + "\nThe closing line.\n"
    acting_state.store.create("long.md", _note(body, "Long"))

    entry = asyncio.run(read_notes(acting_state, "s", {"paths": ["long.md"]}))["notes"][0]
    assert entry["truncated"] is True
    asyncio.run(
        patch_note(
            acting_state,
            "t",
            {
                "path": "long.md",
                "base_revision": entry["revision"],
                "edits": [{"old_string": "The closing line.", "new_string": "Closed."}],
            },
        )
    )

    assert "Closed." in acting_state.store.read("long.md")


def test_get_outline_returns_the_revision(state):
    state.store.create("outlined.md", _note("## One\n\ntext\n\n## Two\n\nmore", "Outlined"))

    outline = asyncio.run(get_outline(state, "s", {"path": "outlined.md"}))

    assert outline["revision"] == content_hash(state.store.read("outlined.md"))
    assert [h["text"] for h in outline["headings"]] == ["One", "Two"]


@pytest.mark.parametrize("char", ["\n", '"', "\\", "\u0001"])
def test_escape_heavy_note_still_fits(state, char):
    # Every raw character serializes to 2 (or 6) characters.
    state.store.create("escapes.md", _note(char * 30_000, "Escapes"))

    output = asyncio.run(execute_tool(state, "s", "read_notes", {"paths": ["escapes.md"]}))

    assert agent._clip(output) == output
    entry = json.loads(output)["notes"][0]
    assert entry["truncated"] is True
    assert len(entry["content"]) > 0
