"""The retry-with-a-different-strategy reflex: empty search results explain
themselves (which filter failed, what to try instead), the system prompt
carries the reflex rule, and an all-empty tool round gets one in-band nudge."""
import asyncio
import json

from app.overseer.agent import EMPTY_ROUND_NUDGE, Overseer, _empty_ish
from app.overseer.provider import FakeProvider, ProviderReply, ToolCall
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

        async def search(self, q, k=8):
            return []

    state.semantic = EmptySemantic()
    out = run_tool(state, "semantic_search", {"query": "anything"})
    assert out["results"] == []
    assert "keyword_search" in out["note"]


def test_nonempty_results_carry_no_note(state):
    seed_note(state, "Find.md", "a very findable sentence")
    out = run_tool(state, "keyword_search", {"query": "findable"})
    assert out["results"]
    assert "note" not in out


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
