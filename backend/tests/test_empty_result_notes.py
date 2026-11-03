"""The retry-with-a-different-strategy reflex: empty search results explain
themselves (which filter failed, what to try instead), the system prompt
carries the reflex rule, and an all-empty tool round gets one in-band nudge."""
import asyncio
import json
import re
import shutil

import pytest

from app.event_store import EventStore
from app.flashcards import FlashcardStore
from app.index.db import content_hash
from app.index.thread_index import ThreadIndex
from app.overseer.agent import EMPTY_ROUND_NUDGE, Overseer, _empty_ish
from app.overseer.provider import FakeProvider, ProviderReply, ToolCall
from app.overseer.threads import ThreadStore
from app.overseer.tool_expansion import EXPANDED_TOOLS, bulk_tag
from app.overseer.tools import execute_tool


def tool_use_reply(name: str, input: dict, call_id: str = "t1") -> ProviderReply:
    return ProviderReply(
        text="",
        tool_calls=[ToolCall(id=call_id, name=name, input=input)],
        stop_reason="tool_use",
        raw_content=[{"type": "tool_use", "id": call_id, "name": name, "input": input}],
    )


def run_tool(state, name: str, inp: dict) -> dict:
    return json.loads(asyncio.run(execute_tool(state, "s", name, inp)))


def seed_note(state, path: str, content: str) -> None:
    created = state.store.create(path, content)
    asyncio.run(state.indexer.index_note(path, created))


# --- tool-side notes ---------------------------------------------------------


def test_keyword_search_names_a_missing_tag(state):
    seed_note(state, "Notes.md", "gradient descent basics")
    out = run_tool(state, "keyword_search", {"query": "gradient", "tag": "nosuchtag"})
    assert out["results"] == []
    assert "#nosuchtag" in out["note"]
    assert "tag is the problem" in out["note"]


def test_keyword_search_separates_tag_hit_from_query_miss(state):
    seed_note(state, "ML.md", "---\ntags: [ml]\n---\n\nOn optimizers.")
    out = run_tool(state, "keyword_search", {"query": "zzzunfindable", "tag": "ml"})
    assert out["results"] == []
    assert "#ml exist" in out["note"]
    assert "drop the query or the tag" in out["note"]


def test_keyword_search_query_only_suggests_semantic(state):
    out = run_tool(state, "keyword_search", {"query": "zzzunfindable"})
    assert out["results"] == []
    assert "semantic_search" in out["note"]


def test_tag_only_search_notes_the_absent_tag(state):
    out = run_tool(state, "keyword_search", {"tag": "ghost"})
    assert out["results"] == []
    assert "#ghost" in out["note"]


def test_query_by_date_echoes_range_and_other_field(state):
    out = run_tool(
        state, "query_by_date", {"field": "created", "start": "1990-01-01", "end": "1990-01-02"}
    )
    assert out["results"] == []
    assert "1990-01-01" in out["note"]
    assert "modified" in out["note"]


def test_semantic_search_empty_points_at_keyword_search(state):
    class EmptySemantic:
        available = True

        async def search(self, q, k=8, path_prefix=None, exclude_prefix=None):
            return []

    state.semantic = EmptySemantic()
    out = run_tool(state, "semantic_search", {"query": "anything"})
    assert out["results"] == []
    assert "keyword_search" in out["note"]


class _NoPaperIndex:
    """B2's PaperIndex, stubbed: an empty index that still answers the
    counters, which is what turns an empty hit list into a useful note."""

    async def search(self, query, limit=10, notebook=None):
        return []

    async def count_pages(self):
        return 4

    async def count_transcribed(self):
        return 0


def test_list_notebooks_empty_library_blames_the_vault_not_the_query(state):
    out = run_tool(state, "list_notebooks", {})
    assert out["notebooks"] == []
    assert "iPad" in out["note"]


def test_search_handwriting_empty_says_which_half_is_missing(state):
    state.paper_index = _NoPaperIndex()
    out = run_tool(state, "search_handwriting", {"query": "anything"})
    assert out["results"] == []
    assert "4 Paper page(s)" in out["note"]


def test_nonempty_results_carry_no_note(state):
    seed_note(state, "Find.md", "a very findable sentence")
    out = run_tool(state, "keyword_search", {"query": "findable"})
    assert out["results"]
    assert "note" not in out


# --- the convention, registry-wide -------------------------------------------
# HANDOFF §5: "Any new list/search tool must follow this." Every read tool
# named like a listing runs against an empty vault and must say why it came
# back empty — a bare [] reads as "there are none" and invites the same call.

LISTING = re.compile(r"(list_|find_|search_|related_|get_forward_)")
# Minimal valid input for the listings that require some.
EMPTY_INPUTS = {
    "list_events": {"start": "2030-01-01", "end": "2030-01-31"},
    "search_conversations": {"query": "anything"},
    "search_handwriting": {"query": "anything"},
    "related_notes": {"path": "Lonely.md"},
    "get_forward_links": {"path": "Lonely.md"},
    "find_notes": {"filter": "stale", "before": "1990-01-01"},
}
NOT_SWEPT = {
    "search_media_images": "asks image-search services over the network",
    "find_free_time": "an empty calendar is all free time, never an empty list",
}
LISTINGS = sorted(
    tool.name
    for tool, _ in EXPANDED_TOOLS
    if tool.mode == "read" and LISTING.match(tool.name) and tool.name not in NOT_SWEPT
)


@pytest.fixture
def empty_state(acting_state):
    """A vault with nothing in it but one unlinked note (the tools that take a
    path need a real one), and every store those tools read."""
    state = acting_state
    for info in state.store.list_notes():
        state.store.delete(info.path)
        asyncio.run(state.indexer.remove_note(info.path))
    shutil.rmtree(state.settings.vault_dir / "templates", ignore_errors=True)
    data = state.settings.data_dir
    state.events = EventStore(state.settings.vault_dir)
    state.flashcards = FlashcardStore(state.settings.vault_dir)
    state.threads = ThreadStore(
        state.settings.vault_dir, data, index=ThreadIndex(data / "threads" / "fts.db")
    )
    seed_note(state, "Lonely.md", "Nothing links here, and it links nowhere.")
    return state


def test_the_sweep_names_real_tools():
    names = {tool.name for tool, _ in EXPANDED_TOOLS}
    assert set(EMPTY_INPUTS) <= set(LISTINGS)
    assert set(NOT_SWEPT) <= names


@pytest.mark.parametrize("name", LISTINGS)
def test_every_listing_explains_an_empty_result(empty_state, name):
    out = run_tool(empty_state, name, EMPTY_INPUTS.get(name, {}))
    assert "error" not in out, out
    assert _empty_ish(out), f"{name} found something in an empty vault: {out}"
    assert str(out.get("note") or "").strip(), f"{name} came back empty with no note"


def test_list_media_status_miss_names_what_is_tracked(acting_state):
    run_tool(
        acting_state,
        "bulk_log_media",
        {"items": [{"title": "Dune", "media_type": "book"}, {"title": "Alien", "media_type": "movie"}]},
    )
    out = run_tool(acting_state, "list_media", {"status": "active"})
    assert out["items"] == []
    assert "status=active" in out["note"]
    assert "not the vault" in out["note"]
    assert "planned 2" in out["note"]
    assert "book 1" in out["note"] and "movie 1" in out["note"]


def test_list_events_separates_an_empty_range_from_an_empty_calendar(acting_state):
    acting_state.events = EventStore(acting_state.settings.vault_dir)
    out = run_tool(acting_state, "list_events", {"start": "2030-01-01", "end": "2030-01-31"})
    assert "no calendar events at all" in out["note"]
    run_tool(acting_state, "create_event", {"title": "Exam", "start": "2030-06-01T09:00"})
    out = run_tool(acting_state, "list_events", {"start": "2030-01-01", "end": "2030-01-31"})
    assert out["events"] == []
    assert "not the vault: 1 calendar events exist" in out["note"]


def test_board_digest_of_an_unknown_board_names_the_real_ones(acting_state):
    """_resolve_board_arg passes a miss through, which used to yield an
    all-zero digest: a healthy empty board where the name was simply wrong."""
    acting_state.store.create_folder("School")
    run_tool(acting_state, "create_task_board", {"folder": "School"})
    out = run_tool(acting_state, "board_digest", {"path": "Scool"})
    assert "Scool" in out["error"]
    assert "School" in out["error"]
    assert "total" not in out


def test_get_forward_links_of_a_missing_note_is_an_error(state):
    out = run_tool(state, "get_forward_links", {"path": "Nowhere.md"})
    assert "error" in out
    assert "forward_links" not in out


def test_related_notes_with_an_index_that_finds_only_itself(state):
    seed_note(state, "Alone.md", "a thought with no neighbours")

    class SelfOnly:
        available = True

        async def search(self, q, k=8, path_prefix=None, exclude_prefix=None):
            return [{"path": "Alone.md", "heading": None, "score": 1.0}]

    state.semantic = SelfOnly()
    out = run_tool(state, "related_notes", {"path": "Alone.md"})
    assert out["related"] == []
    assert "keyword_search" in out["note"]


def test_bulk_log_media_all_already_tracked_names_the_skipped(acting_state):
    items = [{"title": "Dune", "media_type": "book"}, {"title": "Alien", "media_type": "movie"}]
    run_tool(acting_state, "bulk_log_media", {"items": items})
    out = run_tool(acting_state, "bulk_log_media", {"items": items})
    assert out["logged"] == 0
    assert out["skipped"] == ["Dune", "Alien"]
    assert "already tracked" in out["note"]


def test_bulk_tag_zero_says_whether_the_filter_or_the_tag_was_the_cause(acting_state):
    # Called directly: through execute_tool a bulk tag stops at its
    # confirmation card, and the zero case is what comes after.
    seed_note(acting_state, "Work/A.md", "---\ntags: [done]\n---\n\nbody")
    already = asyncio.run(bulk_tag(acting_state, "s", {"tag": "done", "folder": "Work"}))
    assert already["changed"] == 0
    assert "already have #done" in already["note"]
    missed = asyncio.run(bulk_tag(acting_state, "s", {"tag": "done", "folder": "Wrok"}))
    assert missed["changed"] == 0
    assert "No notes matched" in missed["note"]


def test_convert_bullets_with_nothing_to_convert_says_so(acting_state):
    seed_note(acting_state, "Plan.md", "- [ ] already a task\n")
    revision = content_hash(acting_state.store.read("Plan.md"))
    out = run_tool(
        acting_state, "convert_bullets_to_tasks", {"path": "Plan.md", "base_revision": revision}
    )
    assert out["converted"] == 0
    assert "Plan.md" in out["note"]


# --- the empty-ish detector --------------------------------------------------


def test_empty_ish_is_conservative():
    assert _empty_ish({"tasks": []})
    assert _empty_ish({"results": [], "note": "hint"})
    assert not _empty_ish({"tasks": [1]})
    assert not _empty_ish({"tasks": [], "matches": [1]})
    assert not _empty_ish({"renamed": 0})  # zero-count ack, no list
    assert not _empty_ish({"displayed": True})
    assert not _empty_ish({"results": [], "error": "boom"})
    assert not _empty_ish("plain string")


# --- loop-level nudge --------------------------------------------------------


def nudge_blocks(provider) -> list[dict]:
    """Every EMPTY_ROUND_NUDGE text block in the final message history (the
    last snapshot the provider saw), paired with whether it rode inside a
    tool-result message."""
    found = []
    for message in provider.seen[-1]:
        if message["role"] != "user" or not isinstance(message["content"], list):
            continue
        has_tool_result = any(
            b.get("type") == "tool_result" for b in message["content"]
        )
        for block in message["content"]:
            if block.get("type") == "text" and block["text"] == EMPTY_ROUND_NUDGE:
                found.append({"inside_tool_results": has_tool_result})
    return found


def test_system_prompt_carries_the_reflex_rule(state):
    provider = FakeProvider([ProviderReply(text="ok", stop_reason="end_turn")])
    asyncio.run(Overseer(provider, state).chat("hello"))
    assert "fact about the query" in provider.systems[0]
    assert "NEVER rerun the same query shape" in provider.systems[0]


def test_all_empty_round_gets_one_nudge_inside_the_tool_result_message(state):
    provider = FakeProvider(
        [
            tool_use_reply("list_tasks", {"path": "Nowhere"}),
            ProviderReply(text="done", stop_reason="end_turn"),
        ]
    )
    asyncio.run(Overseer(provider, state).chat("delete all tasks in nowhere"))
    nudges = nudge_blocks(provider)
    assert len(nudges) == 1
    assert nudges[0]["inside_tool_results"]  # never a standalone user turn


def test_mixed_round_gets_no_nudge(state):
    seed_note(state, "Find.md", "a very findable sentence")
    calls = [
        ToolCall(id="t1", name="list_tasks", input={"path": "Nowhere"}),
        ToolCall(id="t2", name="keyword_search", input={"query": "findable"}),
    ]
    provider = FakeProvider(
        [
            ProviderReply(
                text="",
                tool_calls=calls,
                stop_reason="tool_use",
                raw_content=[
                    {"type": "tool_use", "id": c.id, "name": c.name, "input": c.input}
                    for c in calls
                ],
            ),
            ProviderReply(text="found it", stop_reason="end_turn"),
        ]
    )
    asyncio.run(Overseer(provider, state).chat("any tasks?"))
    assert nudge_blocks(provider) == []


def test_nudge_fires_at_most_once_per_turn(state):
    provider = FakeProvider(
        [
            tool_use_reply("list_tasks", {"path": "Nowhere"}, call_id="t1"),
            tool_use_reply("keyword_search", {"query": "zzzunfindable"}, call_id="t2"),
            ProviderReply(text="nothing anywhere", stop_reason="end_turn"),
        ]
    )
    asyncio.run(Overseer(provider, state).chat("find my zzz note"))
    assert len(nudge_blocks(provider)) == 1


def test_proactive_pass_never_gets_the_nudge(state):
    provider = FakeProvider(
        [
            tool_use_reply("list_tasks", {"path": "Nowhere"}),
            ProviderReply(text="quiet", stop_reason="end_turn"),
        ]
    )
    asyncio.run(Overseer(provider, state).chat("morning pass", proactive=True))
    assert nudge_blocks(provider) == []
