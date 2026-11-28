"""The tool array OMNIA is offered each turn.

It used to be scored against the user's message and trimmed to the best 120 of
162, which had two costs: the array changed shape between two ordinary messages
(36 tools of drift), so the prompt cache the system prompt is built around was
thrown away every turn; and tools that prompt advertises BY NAME — capture,
read_attachment, propose_edits — dropped out whenever the wording missed them,
so the model was told to use tools it had not been given. Now the base set is
fixed and the niche tools sit behind use_tools(domain).
"""
import asyncio
import json

from app.overseer.agent import SYSTEM_PROMPT, Overseer
from app.overseer.persona import PERSONA
from app.overseer.provider import FakeProvider, ProviderReply, ToolCall
from app.overseer.tools import (
    BASE_TOOLS,
    CORE_TOOL_NAMES,
    ESCAPE_TOOL,
    MAX_TOOLS,
    SUGGESTION_TOOL_DEFS,
    TOOL_DEFS,
    TOOL_DOMAINS,
    UNATTENDED_EXCLUDED,
    UNATTENDED_TOOL_NAMES,
    base_tool_names,
    execute_tool,
    hidden_tool_names,
    prompt_referenced_tools,
    reveal_tools,
    select_tools,
)


def _names(defs):
    return {t.name for t in defs}


class RecordingProvider(FakeProvider):
    """FakeProvider that also keeps the tool array of every round."""

    def __init__(self, script):
        super().__init__(script)
        self.tool_sets: list[list[str]] = []

    async def chat(self, system, messages, tools, on_text=None, context=None):
        self.tool_sets.append([t.name for t in tools])
        return await super().chat(system, messages, tools, on_text, context=context)


def _reply(text="ok"):
    return ProviderReply(text=text, stop_reason="end_turn")


def tool_use_reply(name: str, inp: dict, call_id: str = "t1") -> ProviderReply:
    return ProviderReply(
        text="",
        tool_calls=[ToolCall(id=call_id, name=name, input=inp)],
        stop_reason="tool_use",
        raw_content=[{"type": "tool_use", "id": call_id, "name": name, "input": inp}],
    )


def test_full_registry_exceeds_the_api_cap():
    # The reason this module exists: the registry is over 128.
    assert len(TOOL_DEFS) > 128


def test_core_names_all_exist():
    # Guard against drift: a renamed/removed core tool should fail loudly here,
    # not silently drop out of every request.
    missing = CORE_TOOL_NAMES - _names(TOOL_DEFS)
    assert not missing, f"core names no longer in the registry: {sorted(missing)}"
    assert len(CORE_TOOL_NAMES) < BASE_TOOLS  # core must leave room to fit


def test_the_base_set_is_the_same_for_any_message(state):
    """The point of the rewrite. Two ordinary messages used to differ by 36
    tools, which invalidates the cached prefix the schemas sit in."""
    provider = RecordingProvider([_reply(), _reply()])
    overseer = Overseer(provider, state)

    asyncio.run(overseer.chat("make flashcards from this deck"))
    asyncio.run(overseer.chat("can you delete all tasks in school"))

    first, second = provider.tool_sets
    assert first == second
    assert first == [t.name for t in select_tools(TOOL_DEFS)]


def test_the_offered_set_fits_the_cap_at_rest_and_after_reveals():
    offered = select_tools(TOOL_DEFS)
    assert len(offered) <= BASE_TOOLS + 1  # the base set plus use_tools
    assert CORE_TOOL_NAMES <= _names(offered)
    assert ESCAPE_TOOL in _names(offered)

    # Every domain revealed at once still clears the API's 128 ceiling.
    revealed = offered
    for domain in TOOL_DOMAINS:
        revealed = reveal_tools(revealed, sorted(TOOL_DOMAINS[domain][1]))
        assert len(revealed) <= MAX_TOOLS
        # The domain just asked for is always fully present…
        assert TOOL_DOMAINS[domain][1] <= _names(revealed)
    # …and the fundamentals are never what got dropped to make room.
    assert CORE_TOOL_NAMES <= _names(revealed)


def test_every_tool_is_either_offered_or_in_exactly_one_domain():
    """A tool in neither is unreachable; a tool in two is reachable by a domain
    that does not describe it."""
    base, hidden = base_tool_names(), hidden_tool_names()
    registry = _names(TOOL_DEFS)

    assert not base & hidden
    assert base | hidden == registry, (
        f"unreachable: {sorted(registry - base - hidden)}"
    )
    counts: dict[str, int] = {}
    for _description, names in TOOL_DOMAINS.values():
        for name in names:
            counts[name] = counts.get(name, 0) + 1
    assert not [name for name, n in counts.items() if n > 1]
    assert not hidden - registry, f"domain names not in the registry: {hidden - registry}"


def test_every_tool_the_prompt_names_is_actually_offered():
    """The advertised-but-absent bug: the prompt says "call read_attachment on
    it" while the array holds no such tool."""
    advertised = prompt_referenced_tools(SYSTEM_PROMPT, PERSONA)
    assert advertised, "the scan itself broke — it should find dozens"
    assert advertised <= base_tool_names()
    # A spot-check of the ones the old scoring dropped most often.
    assert {
        "capture",
        "set_media_art",
        "search_media_images",
        "read_attachment",
        "propose_edits",
        "show_graphic",
    } <= advertised


def test_the_prompt_lists_every_domain():
    """The operating rule names the domains; a new one that never reaches the
    prompt can only be found by guessing."""
    for domain in TOOL_DOMAINS:
        assert domain in SYSTEM_PROMPT


def test_small_sets_pass_through_untouched():
    # The proactive/suggestion set is under the budget, so it is never trimmed —
    # and it deliberately holds no use_tools, so a background pass cannot pull
    # action-tier tools into itself.
    assert select_tools(SUGGESTION_TOOL_DEFS) is SUGGESTION_TOOL_DEFS
    assert len(SUGGESTION_TOOL_DEFS) <= BASE_TOOLS
    assert ESCAPE_TOOL not in _names(SUGGESTION_TOOL_DEFS)


def test_unattended_set_is_explicit_and_read_only():
    """Nobody watches a scheduled run or the morning pass, so what they may
    call is a reviewed list. It used to be derived — every read or proposal
    tool — and a tool registered without mode= is read, so the next one with a
    side effect would have joined the 04:00 set with no test failing."""
    registry = {t.name: t for t in TOOL_DEFS}

    unknown = (UNATTENDED_TOOL_NAMES | UNATTENDED_EXCLUDED) - set(registry)
    assert not unknown, f"no longer in the registry: {sorted(unknown)}"
    assert not UNATTENDED_TOOL_NAMES & UNATTENDED_EXCLUDED

    writes = sorted(
        name
        for name in UNATTENDED_TOOL_NAMES
        if registry[name].mode not in {"read", "proposal"}
        or registry[name].tier != "suggestion"
    )
    assert not writes, f"unattended tools that are not read/proposal: {writes}"

    decided = UNATTENDED_TOOL_NAMES | UNATTENDED_EXCLUDED
    undecided = sorted(
        name
        for name, tool in registry.items()
        if tool.mode in {"read", "proposal"}
        and tool.tier == "suggestion"
        and name not in decided
    )
    assert not undecided, (
        f"{undecided}: put each in UNATTENDED_TOOL_NAMES (scheduled runs and "
        "the morning pass may call it with nobody watching) or in "
        "UNATTENDED_EXCLUDED, in overseer/tools.py"
    )

    offered = _names(SUGGESTION_TOOL_DEFS)
    assert offered == UNATTENDED_TOOL_NAMES
    assert "note_observation" in offered
    assert "get_security_overview" not in offered
    assert "update_habit" not in offered


def test_use_tools_reveals_a_domain_for_the_rest_of_the_turn(state):
    provider = RecordingProvider(
        [
            tool_use_reply("use_tools", {"domain": "flashcards"}),
            _reply("Nothing due."),
        ]
    )
    overseer = Overseer(provider, state)
    result = asyncio.run(overseer.chat("how is my studying going?"))

    first_round, second_round = provider.tool_sets
    assert "study_stats" not in first_round
    assert TOOL_DOMAINS["flashcards"][1] <= set(second_round)
    # Nothing was lost to make room, and the ceiling holds.
    assert set(first_round) <= set(second_round)
    assert len(second_round) <= MAX_TOOLS
    assert result["trace"][0]["ok"] is True

    # The reveal dies with the turn: the next message starts from the base set.
    provider.script.append(_reply())
    asyncio.run(overseer.chat("anything else?", session_id=result["session_id"]))
    assert provider.tool_sets[-1] == first_round


def test_the_reveal_marker_never_reaches_the_model(state):
    provider = RecordingProvider(
        [tool_use_reply("use_tools", {"domain": "activity"}), _reply()]
    )
    asyncio.run(Overseer(provider, state).chat("log some hours"))

    results = [
        block
        for message in provider.seen[-1]
        if isinstance(message["content"], list)
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]
    payload = json.loads(results[-1]["content"])
    assert "_reveal_tools" not in payload
    assert payload["domain"] == "activity"
    assert {tool["name"] for tool in payload["tools"]} == set(TOOL_DOMAINS["activity"][1])


def test_an_unknown_domain_says_which_ones_exist(state):
    out = json.loads(asyncio.run(execute_tool(state, "s", "use_tools", {"domain": "nope"})))
    assert "Unknown domain" in out["error"]
    assert "flashcards" in out["error"]


def test_calling_a_hidden_tool_points_at_its_domain(state):
    """Unknown to this turn is not unknown to the app — the model has to be
    told the ability exists behind a use_tools call, not that it does not."""
    offered = frozenset(t.name for t in select_tools(TOOL_DEFS))

    hidden = json.loads(
        asyncio.run(
            execute_tool(state, "s", "get_appearance", {}, available=offered)
        )
    )
    assert 'use_tools("appearance")' in hidden["error"]
    assert "get_appearance" in hidden["error"]

    # A name that is not a tool at all keeps the plain message.
    nonsense = json.loads(
        asyncio.run(execute_tool(state, "s", "make_coffee", {}, available=offered))
    )
    assert nonsense["error"] == "Unknown tool: make_coffee"

    # And with no turn to gate against (approving a confirmation card), a
    # hidden tool still runs.
    ran = json.loads(asyncio.run(execute_tool(state, "s", "get_appearance", {})))
    assert "error" not in ran or "not available this turn" not in ran["error"]
