"""Prompt caching: the split system prompt and the rolling history breakpoint.

Measured on production, 5.86M chat input tokens in a month bought 31.8k cache
reads — 0.5%. The cause was structural: every volatile value (the clock, the
note count, the memory note, whatever the user had selected) was formatted
INTO the one ~10k-token system block that carried the cache breakpoint, so
each turn invalidated the whole prefix.

Splitting it into a stable and a volatile system block fixed the system prompt
but not the conversation: caching is a prefix match over tools -> system ->
messages, so a volatile system block still sat ahead of the history, and every
turn re-wrote the whole thread at the write surcharge without ever reading it.
Now the system blocks are the stable prompt and the memory note (each with a
breakpoint), and the per-turn context follows the conversation. These pin the
shapes that make that hold.
"""
import asyncio
import copy
import json
import re
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.overseer import agent, history
from app.overseer.agent import Overseer
from app.overseer.provider import (
    FakeProvider,
    ProviderReply,
    ToolDef,
    takes_system_messages,
    with_history_breakpoint,
)
from app.overseer.threads import ThreadStore

EPHEMERAL = {"type": "ephemeral"}


def _reply(text="ok"):
    return ProviderReply(text=text, stop_reason="end_turn")


def _run(overseer, message, **kwargs):
    return asyncio.run(overseer.chat(message, **kwargs))


class _Clock(datetime):
    """agent.datetime, one minute later on every reading."""

    minutes = 0

    @classmethod
    def now(cls, tz=None):
        cls.minutes += 1
        return datetime(2026, 9, 23, 9, 0, tzinfo=tz) + timedelta(minutes=cls.minutes)


@pytest.fixture
def ticking_clock(monkeypatch):
    _Clock.minutes = 0
    monkeypatch.setattr(agent, "datetime", _Clock)


# --- the split system prompt ------------------------------------------------


def test_the_stable_block_is_identical_across_turns_whatever_the_context(state):
    provider = FakeProvider([_reply(), _reply()])
    overseer = Overseer(provider, state)

    _run(overseer, "hi")
    # Move everything that can move: the note count, the memory note, and what
    # the user is looking at.
    state.store.create("omnia/memory.md", "---\ntitle: M\n---\n\nWrites in Go.")
    asyncio.run(state.indexer.index_note("omnia/memory.md", state.store.read("omnia/memory.md")))
    _run(
        overseer,
        "hi again",
        context_note="Ideas.md",
        ui_context={"selection": "one line", "view": "Calendar"},
    )

    first, second = provider.system_parts
    assert len(first) == 2 and len(second) == 2
    # The cached prefix must be byte-identical, or the breakpoint buys nothing.
    assert first[0] == second[0]
    # The memory block moves when the memory does — and only then.
    assert first[1] != second[1]
    assert "Writes in Go." in second[1]


def test_two_turns_a_minute_apart_send_identical_system_blocks(state, ticking_clock):
    """The clock, the open note and the selection ride after the conversation.
    Up in the system blocks they re-wrote the whole cached thread every turn."""
    provider = FakeProvider([_reply(), _reply()])
    overseer = Overseer(provider, state)
    moment = {"context_note": "Ideas.md", "ui_context": {"selection": "quick brown fox"}}

    result = _run(overseer, "hi", **moment)
    _run(overseer, "and again", session_id=result["session_id"], **moment)

    first, second = provider.system_parts
    assert first == second  # stable AND memory blocks, byte for byte
    assert "local time 09:01" in provider.contexts[0]
    assert "local time 09:02" in provider.contexts[1]
    system = "\n\n".join(first)
    for moving in ("Current date and time", "local time", "Ideas.md", "quick brown fox"):
        assert moving not in system


def test_the_system_blocks_hold_instructions_and_memory_the_context_the_moment(state):
    provider = FakeProvider([_reply()])
    overseer = Overseer(provider, state)
    _run(
        overseer,
        "hi",
        context_note="Ideas.md",
        ui_context={"selection": "a sentence", "view": "Calendar"},
        proactive=True,
    )
    stable, memory = provider.system_parts[-1]
    context = provider.contexts[-1]

    assert "dry and understated" in stable  # persona
    assert "## Your abilities" in stable
    assert "## Playbooks" in stable
    assert "<memory>" in memory and "<habits>" in memory
    # Nothing that moves may sit above the conversation.
    for block in (stable, memory):
        assert "## Right now" not in block
        assert "Ideas.md" not in block
        assert "a sentence" not in block
        assert "PROACTIVE" not in block

    assert context.startswith("## Right now")
    assert "Ideas.md" in context
    assert "a sentence" in context
    assert "Calendar" in context
    assert "PROACTIVE" in context
    assert "<memory>" not in context


def test_the_context_carries_the_clock_not_just_the_date(state):
    """"Remind me in an hour" is unanswerable from a date alone."""
    provider = FakeProvider([_reply()])
    overseer = Overseer(provider, state)
    state.timezone = "Europe/Berlin"
    _run(overseer, "hi")

    assert re.search(
        r"Current date and time: \d{4}-\d{2}-\d{2} \(\w+\), local time "
        r"\d{2}:\d{2} \(Europe/Berlin\)",
        provider.contexts[-1],
    )
    # And the clock is nowhere in the cached blocks (the stable text does carry
    # a literal example date, so look for the live line, not any date at all).
    for block in provider.system_parts[-1]:
        assert "Current date and time" not in block
        assert "local time" not in block


def test_an_unknown_timezone_still_produces_a_clock(state):
    provider = FakeProvider([_reply()])
    overseer = Overseer(provider, state)
    state.timezone = "Mars/Olympus_Mons"
    _run(overseer, "hi")
    assert "local time" in provider.contexts[-1]


def test_the_persona_reminder_lands_in_the_context(state):
    """It flips on partway through a long thread. In a system block that would
    invalidate the cached prefix for the rest of the conversation."""
    provider = FakeProvider([_reply(), _reply()])
    overseer = Overseer(provider, state)

    result = _run(overseer, "hi")
    short_parts = provider.system_parts[-1]
    assert "voice rules above still apply" not in provider.contexts[-1]

    sid = result["session_id"]
    overseer.sessions[sid].extend(
        {"role": "user", "content": f"m{i}"} for i in range(20)
    )
    _run(overseer, "still there?", session_id=sid)

    assert "voice rules above still apply" in provider.contexts[-1]
    assert provider.system_parts[-1] == short_parts


def test_the_context_is_the_same_on_every_round_of_a_turn(state):
    from app.overseer.provider import ToolCall

    provider = FakeProvider(
        [
            ProviderReply(
                text="",
                tool_calls=[ToolCall(id="t1", name="read_structure", input={})],
                stop_reason="tool_use",
                raw_content=[
                    {"type": "tool_use", "id": "t1", "name": "read_structure", "input": {}}
                ],
            ),
            _reply(),
        ]
    )
    _run(Overseer(provider, state), "what's here?", context_note="Ideas.md")

    assert len(provider.contexts) == 2
    assert provider.contexts[0] == provider.contexts[1]
    assert provider.system_parts[0] == provider.system_parts[1]


# --- providers without mid-conversation system messages ---------------------


def test_without_system_messages_the_context_is_written_into_the_user_turn(state):
    """Persisted, so the next turn's history extends this one's byte for byte
    instead of losing a block from the middle of the cached prefix."""
    provider = FakeProvider([_reply(), _reply()], system_messages=False)
    overseer = Overseer(provider, state)

    result = _run(overseer, "hi", context_note="Ideas.md")
    sid = result["session_id"]
    after_first = copy.deepcopy(overseer.sessions[sid])
    _run(overseer, "and again", session_id=sid, context_note="Ideas.md")

    assert provider.contexts == [None, None]
    user = after_first[0]["content"]
    assert user[0] == {"type": "text", "text": "hi"}
    assert user[-1]["text"].startswith("\n\n## Right now")
    assert "Ideas.md" in user[-1]["text"]
    assert overseer.sessions[sid][: len(after_first)] == after_first
    for block in provider.system_parts[-1]:
        assert "Ideas.md" not in block and "local time" not in block


def test_the_context_block_is_not_the_users_words(tmp_path):
    """Regenerating an attachments-only message pairs it by having no text; a
    context block counted as text sent the cut to the count-back fallback,
    which a scheduled run in between put one exchange too late."""
    context = {"type": "text", "text": "\n\n## Right now\n\nCurrent date and time: x."}
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA"}}
    provider_history = [
        {"role": "user", "content": [image, context]},
        {"role": "assistant", "content": [{"type": "text", "text": "A1"}]},
        {"role": "user", "content": [{"type": "text", "text": "Daily check"}, context]},
        {"role": "assistant", "content": [{"type": "text", "text": "S"}]},
        {"role": "user", "content": [{"type": "text", "text": "U2"}, context]},
        {"role": "assistant", "content": [{"type": "text", "text": "A2"}]},
    ]
    threads = ThreadStore(tmp_path)
    thread = threads.create(title="t", thread_id="abcd1234")
    first = threads.append_public(thread["id"], {"role": "user", "text": ""})
    for role, text in (("assistant", "A1"), ("user", "U2"), ("assistant", "A2")):
        threads.append_public(thread["id"], {"role": role, "text": text})
    threads.set_provider_history(thread["id"], provider_history)

    threads.truncate_from(thread["id"], first["id"])

    assert threads.provider_history(thread["id"]) == []
    # Nor is it anything a summary should repeat back.
    assert "Right now" not in history.transcript(provider_history)


# --- the Anthropic request shape -------------------------------------------


class _TextBlock(SimpleNamespace):
    def model_dump(self, exclude_none=False):
        return {"type": self.type, "text": self.text}


def _anthropic_provider(captured: list[dict], model: str = "claude-sonnet-5"):
    """An AnthropicProvider whose messages.create records its kwargs."""
    from app.overseer.provider import AnthropicProvider

    provider = AnthropicProvider("test-key", model)

    async def create(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(
            content=[_TextBlock(type="text", text="ok")],
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=10, output_tokens=2),
        )

    provider.client = SimpleNamespace(messages=SimpleNamespace(create=create))
    return provider


def _tool(name="read_notes"):
    return ToolDef(name=name, description="d", input_schema={"type": "object"})


def test_anthropic_emits_one_system_block_per_part_each_cached():
    captured: list[dict] = []
    provider = _anthropic_provider(captured)

    asyncio.run(
        provider.chat(
            ["STABLE", "MEMORY"],
            [{"role": "user", "content": "hi"}],
            [_tool()],
        )
    )

    blocks = captured[0]["system"]
    assert [b["text"] for b in blocks] == ["STABLE", "MEMORY"]
    # A remember() re-writes the memory block, never the stable one.
    assert [b["cache_control"] for b in blocks] == [EPHEMERAL, EPHEMERAL]


def test_anthropic_tools_carry_no_breakpoint_of_their_own():
    """Tools render ahead of the system prompt, so its breakpoint covers them;
    one of their own only spent one of the four the API allows."""
    captured: list[dict] = []
    provider = _anthropic_provider(captured)

    asyncio.run(
        provider.chat(
            ["STABLE", "MEMORY"],
            [{"role": "user", "content": "hi"}],
            [_tool("a"), _tool("b")],
        )
    )

    assert all("cache_control" not in tool for tool in captured[0]["tools"])


def test_anthropic_still_accepts_a_plain_string_system():
    captured: list[dict] = []
    provider = _anthropic_provider(captured)

    asyncio.run(provider.chat("just one", [{"role": "user", "content": "hi"}], []))

    assert captured[0]["system"] == [
        {"type": "text", "text": "just one", "cache_control": EPHEMERAL}
    ]


def test_anthropic_marks_the_last_message_and_leaves_the_caller_alone():
    """The breakpoint has to ride on a copy: `messages` is the live session
    history, persisted to disk and replayed to OpenAI and Gemini, neither of
    which understands cache_control."""
    import copy

    captured: list[dict] = []
    provider = _anthropic_provider(captured)
    messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": [{"type": "text", "text": "answer"}]},
    ]
    before = copy.deepcopy(messages)

    asyncio.run(provider.chat(["S", "V"], messages, []))

    sent = captured[0]["messages"]
    assert sent[-1]["content"][-1]["cache_control"] == EPHEMERAL
    assert messages == before


def _breakpoints(kwargs: dict) -> list[str]:
    """Where a request's cache_control markers sit."""
    found = [f"tools[{i}]" for i, t in enumerate(kwargs["tools"]) if "cache_control" in t]
    found += [f"system[{i}]" for i, b in enumerate(kwargs["system"]) if "cache_control" in b]
    for i, message in enumerate(kwargs["messages"]):
        content = message["content"]
        for j, block in enumerate(content if isinstance(content, list) else []):
            if "cache_control" in block:
                found.append(f"messages[{i}][{j}]")
    return found


def test_an_opus_turn_sends_its_context_as_a_trailing_system_message(state):
    captured: list[dict] = []
    provider = _anthropic_provider(captured, "claude-opus-4-8")
    overseer = Overseer(provider, state)

    result = _run(
        overseer, "hi", context_note="Ideas.md", ui_context={"selection": "quick brown fox"}
    )

    sent = captured[-1]
    last = sent["messages"][-1]
    assert last["role"] == "system"
    assert last["content"].startswith("## Right now")
    assert "local time" in last["content"] and "quick brown fox" in last["content"]
    # Stable, memory, and the tail of the conversation: three of four.
    tail = len(sent["messages"]) - 2
    assert sent["messages"][tail]["role"] == "user"
    end = len(sent["messages"][tail]["content"]) - 1
    assert _breakpoints(sent) == ["system[0]", "system[1]", f"messages[{tail}][{end}]"]
    for block in sent["system"]:
        assert "local time" not in block["text"] and "quick brown fox" not in block["text"]
    # Request-only: the stored history never holds it.
    stored = overseer.sessions[result["session_id"]]
    assert all(m["role"] != "system" for m in stored)
    assert "cache_control" not in json.dumps(stored)


def test_a_sonnet_turn_folds_its_context_into_the_user_message(state):
    """Sonnet 5 400s on a system role inside messages[]."""
    captured: list[dict] = []
    provider = _anthropic_provider(captured, "claude-sonnet-5")

    _run(Overseer(provider, state), "hi", context_note="Ideas.md")

    sent = captured[-1]
    assert all(m["role"] != "system" for m in sent["messages"])
    user = sent["messages"][-1]
    assert user["role"] == "user"
    assert user["content"][0]["text"] == "hi"
    assert user["content"][-1]["text"].startswith("\n\n## Right now")
    assert "local time" in user["content"][-1]["text"]
    assert _breakpoints(sent) == ["system[0]", "system[1]", "messages[0][1]"]


def _prefix(messages: list[dict]) -> str:
    """Messages as the cache sees them: the moving breakpoint is not content,
    and a string is shorthand for one text block."""
    out = []
    for message in messages:
        content = message["content"]
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        out.append(
            {
                "role": message["role"],
                "content": [
                    {k: v for k, v in block.items() if k != "cache_control"}
                    for block in content
                ],
            }
        )
    return json.dumps(out, sort_keys=True)


@pytest.mark.parametrize("model", ["claude-opus-4-8", "claude-sonnet-5"])
def test_consecutive_turns_share_a_byte_identical_prefix(state, ticking_clock, model):
    """The whole point: turn two reads turn one from cache, up to where turn
    one ended, a minute later, with the same note open."""
    captured: list[dict] = []
    overseer = Overseer(_anthropic_provider(captured, model), state)
    moment = {"context_note": "Ideas.md", "ui_context": {"selection": "quick brown fox"}}

    result = _run(overseer, "hi", **moment)
    _run(overseer, "and again", session_id=result["session_id"], **moment)

    first, second = captured
    assert json.dumps(first["tools"]) == json.dumps(second["tools"])
    assert first["system"] == second["system"]
    conversation = [m for m in first["messages"] if m["role"] != "system"]
    assert _prefix(second["messages"][: len(conversation)]) == _prefix(conversation)
    # And what follows it is turn one's reply, then turn two.
    assert second["messages"][len(conversation)]["role"] == "assistant"


def test_only_models_that_take_system_messages_get_them():
    for model in ("claude-opus-4-8", "claude-opus-5", "claude-fable-5"):
        assert takes_system_messages(model), model
    # Sonnet and Haiku reject the role; Fable 5.1 and Opus 5.5 would count the
    # vanished per-request message as a history edit.
    for model in ("claude-sonnet-5", "claude-haiku-4-5", "claude-fable-5-1", "claude-opus-5-5"):
        assert not takes_system_messages(model), model


# --- the history breakpoint helper -----------------------------------------


def test_history_breakpoint_wraps_string_content_on_the_copy_only():
    messages = [{"role": "user", "content": "plain text"}]
    out = with_history_breakpoint(messages)

    assert out[-1]["content"] == [
        {"type": "text", "text": "plain text", "cache_control": EPHEMERAL}
    ]
    assert messages == [{"role": "user", "content": "plain text"}]


def test_history_breakpoint_does_not_mutate_shared_blocks():
    block = {"type": "text", "text": "answer"}
    messages = [{"role": "assistant", "content": [block]}]

    out = with_history_breakpoint(messages)

    assert out[-1]["content"][0]["cache_control"] == EPHEMERAL
    assert block == {"type": "text", "text": "answer"}
    assert messages[-1]["content"][0] is block


def test_history_breakpoint_walks_back_past_a_thinking_block():
    """Thinking blocks reject cache_control, so the mark goes on the last block
    that accepts one."""
    messages = [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "answer"},
                {"type": "thinking", "thinking": "…", "signature": "sig"},
            ],
        }
    ]
    out = with_history_breakpoint(messages)

    assert out[-1]["content"][0]["cache_control"] == EPHEMERAL
    assert "cache_control" not in out[-1]["content"][1]


def test_history_breakpoint_gives_up_gracefully():
    # Nothing cacheable at all, an empty string, and an empty history.
    only_thinking = [{"role": "assistant", "content": [{"type": "thinking", "thinking": "…"}]}]
    assert with_history_breakpoint(only_thinking) == only_thinking
    blank = [{"role": "user", "content": "   "}]
    assert with_history_breakpoint(blank) == blank
    assert with_history_breakpoint([]) == []


def test_earlier_messages_are_shared_not_copied():
    """Only the tail needs cloning; copying a 400k-char history every round
    would cost more than the caching saves."""
    messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": [{"type": "text", "text": "second"}]},
    ]
    out = with_history_breakpoint(messages)
    assert out[0] is messages[0]
    assert out[-1] is not messages[-1]


# --- per-message usage survives into the thread JSON ------------------------


def test_usage_is_present_on_every_message_and_survives_an_update(tmp_path):
    threads = ThreadStore(tmp_path / "vault", tmp_path / "data")
    thread = threads.create(title="T")
    user = threads.append_public(thread["id"], {"role": "user", "text": "hi"})
    assistant = threads.append_public(thread["id"], {"role": "assistant", "text": ""})

    assert user["usage"] == {}
    threads.update_message(
        thread["id"],
        assistant["id"],
        text="done",
        status="complete",
        usage={"input": 12000, "output": 200, "cache_read": 11500},
    )

    stored = threads.get(thread["id"])["messages"][-1]
    assert stored["usage"] == {"input": 12000, "output": 200, "cache_read": 11500}
