"""The agent's round loop: text between rounds, the round-limit summary, and
the provider history that regenerating a message has to cut back.

Text written before a tool call ran straight into the next round's answer
("Checking the School folder.You have 52 open tasks."), and a turn without a
live listener returned only the last round's text. The round-limit summary sent
no tools on a history full of tool calls, as a standalone user message that
truncate_from then counted as a question the user had asked.
"""
import asyncio
import copy
from types import SimpleNamespace

from app.overseer import agent
from app.overseer.agent import ROUND_LIMIT_REQUEST, Overseer
from app.overseer.history import user_turn_starts
from app.overseer.provider import FakeProvider, ProviderReply, ToolCall, ToolDef
from app.overseer.threads import ThreadStore


def tool_use_reply(text: str = "", call_id: str = "t1") -> ProviderReply:
    raw = [{"type": "text", "text": text}] if text else []
    raw.append({"type": "tool_use", "id": call_id, "name": "read_structure", "input": {}})
    return ProviderReply(
        text=text,
        tool_calls=[ToolCall(id=call_id, name="read_structure", input={})],
        stop_reason="tool_use",
        raw_content=raw,
    )


class SnapshotProvider(FakeProvider):
    """FakeProvider that keeps a copy of each request's history — `seen` holds
    the live list, which the loop goes on appending to."""

    def __init__(self, script):
        super().__init__(script)
        self.snapshots: list[list[dict]] = []

    async def chat(self, system, messages, tools, on_text=None, allow_tools=True, context=None):
        self.snapshots.append(copy.deepcopy(messages))
        return await super().chat(system, messages, tools, on_text, allow_tools, context)


def _chat(overseer, message, streamed: bool):
    deltas: list[str] = []

    async def capture(event: str, value: dict):
        if event == "delta":
            deltas.append(value["text"])

    result = asyncio.run(
        overseer.chat(message, event_callback=capture if streamed else None)
    )
    return result, deltas


# --- text between rounds ----------------------------------------------------


def test_text_before_a_tool_call_is_its_own_paragraph_when_streamed(state):
    provider = FakeProvider(
        [tool_use_reply("Checking."), ProviderReply(text="Done.", stop_reason="end_turn")]
    )
    result, deltas = _chat(Overseer(provider, state), "How many tasks?", streamed=True)

    assert result["reply"] == "Checking.\n\nDone."
    # The live view, the persisted partial and the stored reply agree.
    assert "".join(deltas) == result["reply"]
    assert "\n\n" in deltas, "the break is a delta of its own"


def test_an_unstreamed_turn_returns_every_rounds_text(state):
    """A turn without a listener used to get the last round's text only. The
    closing round still comes back on its own as final_text: the scheduled
    run's NOTHING check and the morning greeting judge that, not the
    narration before a tool call."""
    provider = FakeProvider(
        [tool_use_reply("Checking."), ProviderReply(text="Done.", stop_reason="end_turn")]
    )
    result, _ = _chat(Overseer(provider, state), "How many tasks?", streamed=False)

    assert result["reply"] == "Checking.\n\nDone."
    assert result["final_text"] == "Done."


def test_a_tool_round_without_text_adds_no_break(state):
    provider = FakeProvider(
        [tool_use_reply(""), ProviderReply(text="Done.", stop_reason="end_turn")]
    )
    result, deltas = _chat(Overseer(provider, state), "How many tasks?", streamed=True)

    assert result["reply"] == "Done."
    assert "".join(deltas) == "Done."


def test_no_trailing_break_when_the_last_round_says_nothing(state):
    provider = FakeProvider(
        [tool_use_reply("Checking."), ProviderReply(text="", stop_reason="end_turn")]
    )
    result, deltas = _chat(Overseer(provider, state), "How many tasks?", streamed=True)

    assert result["reply"] == "Checking."
    assert "".join(deltas) == "Checking."


# --- the round-limit summary ------------------------------------------------


def test_the_round_limit_request_rides_inside_the_last_tool_result(state, monkeypatch):
    monkeypatch.setattr(agent, "MAX_ROUNDS", 2)
    provider = SnapshotProvider(
        [
            tool_use_reply(call_id="t1"),
            tool_use_reply(call_id="t2"),
            ProviderReply(text="Read two notes; three remain.", stop_reason="end_turn"),
        ]
    )
    result, _ = _chat(Overseer(provider, state), "Read everything", streamed=False)

    request = provider.snapshots[-1]
    last = request[-1]
    assert last["role"] == "user"
    assert last["content"][0]["type"] == "tool_result"
    assert last["content"][-1] == {"type": "text", "text": ROUND_LIMIT_REQUEST}
    # No standalone user message: the question is still the only real turn.
    assert len(user_turn_starts(request)) == 1
    # Tools stay declared (the history is full of tool calls), calls forbidden.
    assert provider.allow_tools_seen == [True, True, False]
    assert provider.tool_counts[-1] > 0
    assert result["reply"] == "Read two notes; three remain."
    assert result["final_text"] == "Read two notes; three remain."


def test_the_round_limit_turn_leaves_one_user_turn_in_history(state, monkeypatch):
    monkeypatch.setattr(agent, "MAX_ROUNDS", 2)
    provider = FakeProvider(
        [
            tool_use_reply(call_id="t1"),
            tool_use_reply(call_id="t2"),
            ProviderReply(text="Read two notes.", stop_reason="end_turn"),
        ]
    )
    overseer = Overseer(provider, state)
    result, _ = _chat(overseer, "Read everything", streamed=False)

    history = overseer.sessions[result["session_id"]]
    assert len(user_turn_starts(history)) == 1
    assert history[-1] == {"role": "assistant", "content": "Read two notes."}


def test_a_streamed_round_limit_reply_is_what_the_user_watched(state, monkeypatch):
    """The reply used to be the summary alone while the client had appended
    it to the earlier rounds' text, so the message changed on reload."""
    monkeypatch.setattr(agent, "MAX_ROUNDS", 2)
    provider = FakeProvider(
        [
            tool_use_reply("Starting.", call_id="t1"),
            tool_use_reply(call_id="t2"),
            ProviderReply(text="Read two notes.", stop_reason="end_turn"),
        ]
    )
    result, deltas = _chat(Overseer(provider, state), "Read everything", streamed=True)

    assert result["reply"] == "Starting.\n\nRead two notes."
    assert "".join(deltas) == result["reply"]


def test_a_failed_summary_falls_back_to_its_own_paragraph(state, monkeypatch):
    monkeypatch.setattr(agent, "MAX_ROUNDS", 1)

    class Failing(FakeProvider):
        async def chat(
            self, system, messages, tools, on_text=None, allow_tools=True, context=None
        ):
            if not allow_tools:
                raise RuntimeError("provider down")
            return await super().chat(system, messages, tools, on_text, allow_tools, context)

    provider = Failing([tool_use_reply("Starting.", call_id="t1")])
    result, deltas = _chat(Overseer(provider, state), "Read everything", streamed=True)

    assert result["reply"] == (
        "Starting.\n\n(Stopped after too many tool rounds — ask me to continue.)"
    )
    assert "".join(deltas) == result["reply"]


# --- truncate_from pairs by text, not by count ------------------------------


def _text_turn(role: str, text: str) -> dict:
    return {"role": role, "content": [{"type": "text", "text": text}]}


def _texts(history: list[dict]) -> list[str]:
    out = []
    for message in history:
        content = message["content"]
        if isinstance(content, str):
            out.append(content)
        else:
            out.append(
                "".join(b.get("text", "") for b in content if b.get("type") == "text")
                or content[0]["type"]
            )
    return out


def _store(tmp_path, public: list[tuple[str, str]], history: list[dict]):
    store = ThreadStore(tmp_path)
    thread = store.create(title="t", thread_id="abcd1234")
    ids = [
        store.append_public(thread["id"], {"role": role, "text": text})["id"]
        for role, text in public
    ]
    store.set_provider_history(thread["id"], history)
    return store, thread["id"], ids


def _round_limit_exchange(question: str, answer: str) -> list[dict]:
    return [
        _text_turn("user", question),
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t1", "name": "read_structure", "input": {}}],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "{}"},
                {"type": "text", "text": ROUND_LIMIT_REQUEST},
            ],
        },
        _text_turn("assistant", answer),
    ]


def test_a_round_limit_exchange_survives_regenerating_the_next_one(tmp_path):
    store, thread_id, ids = _store(
        tmp_path,
        [("user", "U1"), ("assistant", "A1"), ("user", "U2"), ("assistant", "A2")],
        [
            *_round_limit_exchange("U1", "A1"),
            _text_turn("user", "U2"),
            _text_turn("assistant", "A2"),
        ],
    )

    store.truncate_from(thread_id, ids[2])

    history = store.provider_history(thread_id)
    assert history[0]["content"][0]["text"] == "U1"
    assert history[-1]["content"][0]["text"] == "A1"
    assert len(history) == 4


def test_a_legacy_standalone_round_limit_message_is_not_a_question(tmp_path):
    """Histories written before the fix hold the request as its own user
    message; counting it made the cut land on it, keeping the question and
    every tool round of the answer the user discarded."""
    history = _round_limit_exchange("U1", "A1")
    history[2]["content"] = history[2]["content"][:1]
    history.insert(3, {"role": "user", "content": ROUND_LIMIT_REQUEST})
    store, thread_id, ids = _store(tmp_path, [("user", "U1"), ("assistant", "A1")], history)

    store.truncate_from(thread_id, ids[0])

    assert store.provider_history(thread_id) == []


def test_a_scheduled_run_after_the_message_is_cut_with_it(tmp_path):
    store, thread_id, ids = _store(
        tmp_path,
        [("user", "U1"), ("assistant", "A1"), ("assistant", "A_sched")],
        [
            _text_turn("user", "U1"),
            _text_turn("assistant", "A1"),
            {"role": "user", "content": "(Scheduled run — summarize U1 and the rest.)"},
            _text_turn("assistant", "A_sched"),
        ],
    )

    store.truncate_from(thread_id, ids[0])

    assert store.provider_history(thread_id) == []


def test_a_turn_carrying_a_system_note_still_pairs_with_its_message(tmp_path):
    store, thread_id, ids = _store(
        tmp_path,
        [("user", "U1"), ("assistant", "A1"), ("user", "U2 text"), ("assistant", "A2")],
        [
            _text_turn("user", "U1"),
            _text_turn("assistant", "A1"),
            {
                "role": "user",
                "content": "U2 text\n\n[system] Since your last reply: the user approved it.",
            },
            _text_turn("assistant", "A2"),
            # A scheduled run after it, so a count from the end would miss.
            {"role": "user", "content": "(Scheduled run — check the inbox.)"},
            _text_turn("assistant", "Inbox is quiet."),
        ],
    )

    store.truncate_from(thread_id, ids[2])

    assert _texts(store.provider_history(thread_id)) == ["U1", "A1"]


def test_a_repeated_message_pairs_with_its_own_turn(tmp_path):
    """'continue' twice: matching the first removed one against the latest
    turn would keep the first exchange the user just discarded."""
    store, thread_id, ids = _store(
        tmp_path,
        [
            ("user", "Tidy the tags"),
            ("assistant", "A0"),
            ("user", "continue"),
            ("assistant", "A1"),
            ("user", "continue"),
            ("assistant", "A2"),
        ],
        [
            _text_turn("user", "Tidy the tags"),
            _text_turn("assistant", "A0"),
            _text_turn("user", "continue"),
            _text_turn("assistant", "A1"),
            _text_turn("user", "continue"),
            _text_turn("assistant", "A2"),
        ],
    )

    store.truncate_from(thread_id, ids[2])

    assert _texts(store.provider_history(thread_id)) == ["Tidy the tags", "A0"]


def test_a_short_message_does_not_pair_with_a_prompt_that_contains_it(tmp_path):
    store, thread_id, ids = _store(
        tmp_path,
        [("user", "ok"), ("assistant", "A1"), ("assistant", "Morning.")],
        [
            _text_turn("user", "ok"),
            _text_turn("assistant", "A1"),
            {"role": "user", "content": "(Internal morning check-in — look at the notes.)"},
            _text_turn("assistant", "Morning."),
        ],
    )

    store.truncate_from(thread_id, ids[0])

    assert store.provider_history(thread_id) == []


def test_an_attachment_only_message_pairs_with_a_turn_without_text(tmp_path):
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "x"}}
    store, thread_id, ids = _store(
        tmp_path,
        [
            ("user", "U1"),
            ("assistant", "A1"),
            ("user", ""),
            ("assistant", "A2"),
            ("assistant", "S"),
        ],
        [
            _text_turn("user", "U1"),
            _text_turn("assistant", "A1"),
            {"role": "user", "content": [image]},
            _text_turn("assistant", "A2"),
            {"role": "user", "content": "(Scheduled run — check the inbox.)"},
            _text_turn("assistant", "S"),
        ],
    )

    store.truncate_from(thread_id, ids[2])

    assert _texts(store.provider_history(thread_id)) == ["U1", "A1"]


def test_an_unrecognisable_history_still_falls_back_to_counting(tmp_path):
    store, thread_id, ids = _store(
        tmp_path,
        [("user", "one"), ("assistant", "A1"), ("user", "two"), ("assistant", "A2")],
        [
            _text_turn("user", "something else"),
            _text_turn("assistant", "A1"),
            _text_turn("user", "another"),
            _text_turn("assistant", "A2"),
        ],
    )

    store.truncate_from(thread_id, ids[2])

    assert _texts(store.provider_history(thread_id)) == ["something else", "A1"]


# --- provider request shapes ------------------------------------------------


def _tool(name="read_notes"):
    return ToolDef(name=name, description="d", input_schema={"type": "object"})


class _TextBlock(SimpleNamespace):
    def model_dump(self, exclude_none=False):
        return {"type": self.type, "text": self.text}


def _anthropic(captured: list[dict]):
    from app.overseer.provider import AnthropicProvider

    provider = AnthropicProvider("test-key", "claude-sonnet-5")

    async def create(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(
            content=[_TextBlock(type="text", text="ok")],
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=10, output_tokens=2),
        )

    provider.client = SimpleNamespace(messages=SimpleNamespace(create=create))
    return provider


def _openai(captured: list[dict]):
    from app.overseer.provider import OpenAIProvider

    provider = OpenAIProvider("test-key", "gpt-6.1-sol")

    async def create(**kwargs):
        captured.append(kwargs)
        part = SimpleNamespace(type="output_text", text="ok")
        return SimpleNamespace(
            output=[SimpleNamespace(type="message", content=[part])],
            incomplete_details=None,
            usage=None,
        )

    provider.client = SimpleNamespace(responses=SimpleNamespace(create=create))
    return provider


def _gemini(captured: list[dict]):
    from app.overseer.provider import GeminiProvider

    provider = GeminiProvider("test-key", "gemini-3-flash")

    async def generate_content(**kwargs):
        captured.append(kwargs)
        part = SimpleNamespace(text="ok", function_call=None, thought_signature=None)
        candidate = SimpleNamespace(content=SimpleNamespace(parts=[part]), finish_reason="STOP")
        return SimpleNamespace(candidates=[candidate], usage_metadata=None)

    provider.client = SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    )
    return provider


HI = [{"role": "user", "content": "hi"}]


def test_anthropic_forbids_calls_without_dropping_the_tools():
    captured: list[dict] = []
    provider = _anthropic(captured)

    asyncio.run(provider.chat("s", HI, [_tool("a"), _tool("b")], allow_tools=False))
    asyncio.run(provider.chat("s", HI, [_tool("a"), _tool("b")]))

    forbidden, allowed = captured
    assert forbidden["tool_choice"] == {"type": "none"}
    # Same array as an ordinary round, so the cached tools prefix still hits.
    assert forbidden["tools"] == allowed["tools"]
    assert "tool_choice" not in allowed


def test_openai_forbids_calls_and_omits_an_empty_tool_list():
    captured: list[dict] = []
    provider = _openai(captured)

    asyncio.run(provider.chat("s", HI, [_tool()], allow_tools=False))
    asyncio.run(provider.chat("s", HI, []))
    asyncio.run(provider.chat("s", HI, [], allow_tools=False))

    forbidden, empty, empty_forbidden = captured
    assert forbidden["tool_choice"] == "none"
    assert [t["name"] for t in forbidden["tools"]] == ["read_notes"]
    assert "tools" not in empty and "tool_choice" not in empty
    assert "tools" not in empty_forbidden and "tool_choice" not in empty_forbidden


def test_gemini_forbids_calls_and_omits_an_empty_tool_list():
    captured: list[dict] = []
    provider = _gemini(captured)

    asyncio.run(provider.chat("s", HI, [_tool()], allow_tools=False))
    asyncio.run(provider.chat("s", HI, []))

    forbidden, empty = (c["config"] for c in captured)
    assert forbidden["tool_config"] == {"function_calling_config": {"mode": "NONE"}}
    assert forbidden["tools"][0]["function_declarations"][0]["name"] == "read_notes"
    assert "tools" not in empty and "tool_config" not in empty
