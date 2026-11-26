"""The OpenAI provider speaks the Responses API. It must translate the neutral
(Anthropic-shaped) history to Responses input items without losing tool calls
or results, and read replies back into the neutral shape."""
import asyncio
from types import SimpleNamespace

from app.overseer.provider import OpenAIProvider, ToolDef, _usage, to_openai_input


def test_plain_text_turns():
    msgs = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]
    assert to_openai_input(msgs) == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]


def test_tool_call_round_trip():
    msgs = [
        {"role": "user", "content": "search my notes"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Let me look."},
                {
                    "type": "tool_use",
                    "id": "call_1",
                    "name": "keyword_search",
                    "input": {"query": "travel"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "call_1", "content": '{"results": []}'}
            ],
        },
    ]
    assert to_openai_input(msgs) == [
        {"role": "user", "content": "search my notes"},
        {"role": "assistant", "content": "Let me look."},
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "keyword_search",
            "arguments": '{"query": "travel"}',
        },
        {"type": "function_call_output", "call_id": "call_1", "output": '{"results": []}'},
    ]


def test_parallel_tool_calls_and_results_stay_paired():
    msgs = [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "a", "name": "read_notes", "input": {}},
                {"type": "tool_use", "id": "b", "name": "read_structure", "input": {}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "a", "content": "A"},
                {"type": "tool_result", "tool_use_id": "b", "content": "B"},
            ],
        },
    ]
    out = to_openai_input(msgs)
    # No empty assistant message when the round was calls only.
    assert [item.get("type") for item in out] == [
        "function_call",
        "function_call",
        "function_call_output",
        "function_call_output",
    ]
    assert [item["call_id"] for item in out] == ["a", "b", "a", "b"]


def test_thinking_and_other_provider_blocks_are_not_sent():
    msgs = [
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "", "signature": "sig"},
                {"type": "text", "text": "Done."},
            ],
        }
    ]
    assert to_openai_input(msgs) == [{"role": "assistant", "content": "Done."}]


def _event(kind, **fields):
    return SimpleNamespace(type=kind, **fields)


class _Stream:
    def __init__(self, events):
        self.events = events

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for event in self.events:
            yield event


def _final(output, reason=None, usage=None):
    return SimpleNamespace(
        output=output,
        incomplete_details=SimpleNamespace(reason=reason) if reason else None,
        usage=usage,
    )


def _provider(reply):
    captured: list[dict] = []

    async def create(**kwargs):
        captured.append(kwargs)
        return reply(kwargs)

    provider = OpenAIProvider("test-key", "gpt-6.1-sol")
    provider.client = SimpleNamespace(responses=SimpleNamespace(create=create))
    return provider, captured


TOOL = ToolDef(
    name="read_notes",
    description="Read notes.",
    input_schema={"type": "object", "properties": {"paths": {"type": "array"}}},
)


def test_streaming_forwards_text_and_reads_calls_from_the_final_response():
    call = SimpleNamespace(
        type="function_call", call_id="fc_1", name="read_notes", arguments='{"paths": ["a.md"]}'
    )
    message = SimpleNamespace(
        type="message", content=[SimpleNamespace(type="output_text", text="Reading.")]
    )
    events = [
        _event("response.created", response=None),
        _event("response.output_text.delta", delta="Read"),
        _event("response.output_text.delta", delta="ing."),
        _event("response.completed", response=_final([message, call])),
    ]
    provider, captured = _provider(lambda kwargs: _Stream(events))
    deltas: list[str] = []

    async def on_text(delta):
        deltas.append(delta)

    reply = asyncio.run(
        provider.chat("sys", [{"role": "user", "content": "hi"}], [TOOL], on_text=on_text)
    )

    assert deltas == ["Read", "ing."]
    assert reply.text == "Reading."
    assert [(c.id, c.name, c.input) for c in reply.tool_calls] == [
        ("fc_1", "read_notes", {"paths": ["a.md"]})
    ]
    assert reply.stop_reason == "tool_use"
    assert reply.raw_content == [
        {"type": "text", "text": "Reading."},
        {"type": "tool_use", "id": "fc_1", "name": "read_notes", "input": {"paths": ["a.md"]}},
    ]
    sent = captured[0]
    assert sent["stream"] is True
    assert sent["instructions"] == "sys"
    assert sent["store"] is False
    # Responses defaults to strict schemas, which these tools do not satisfy.
    assert sent["tools"] == [
        {
            "type": "function",
            "name": "read_notes",
            "description": "Read notes.",
            "parameters": TOOL.input_schema,
            "strict": False,
        }
    ]


def test_an_incomplete_response_reports_the_output_limit():
    message = SimpleNamespace(
        type="message", content=[SimpleNamespace(type="output_text", text="Half")]
    )
    provider, _ = _provider(lambda kwargs: _final([message], reason="max_output_tokens"))
    reply = asyncio.run(provider.chat("sys", [{"role": "user", "content": "hi"}], []))
    assert reply.hit_output_limit


def test_a_refusal_drops_its_tool_calls():
    message = SimpleNamespace(
        type="message", content=[SimpleNamespace(type="refusal", refusal="No.")]
    )
    call = SimpleNamespace(type="function_call", call_id="x", name="read_notes", arguments="{}")
    provider, _ = _provider(lambda kwargs: _final([message, call]))
    reply = asyncio.run(provider.chat("sys", [{"role": "user", "content": "hi"}], [TOOL]))
    assert reply.refused
    assert reply.tool_calls == [] and reply.raw_content == []


def test_a_failed_stream_raises():
    import pytest

    failed = SimpleNamespace(error=SimpleNamespace(message="server_error"))
    events = [_event("response.failed", response=failed)]
    provider, _ = _provider(lambda kwargs: _Stream(events))

    async def on_text(delta):
        pass

    with pytest.raises(RuntimeError, match="server_error"):
        asyncio.run(
            provider.chat("sys", [{"role": "user", "content": "hi"}], [], on_text=on_text)
        )


def test_responses_usage_takes_the_cache_traffic_out_of_input():
    usage = SimpleNamespace(
        input_tokens=1000,
        input_tokens_details=SimpleNamespace(cached_tokens=600, cache_write_tokens=100),
        output_tokens=50,
        output_tokens_details=SimpleNamespace(reasoning_tokens=20),
    )
    assert _usage(usage) == {"input": 300, "output": 50, "cache_read": 600, "cache_write": 100}


def test_anthropic_usage_is_unchanged():
    usage = SimpleNamespace(
        input_tokens=40,
        output_tokens=5,
        cache_read_input_tokens=900,
        cache_creation_input_tokens=10,
    )
    assert _usage(usage) == {"input": 40, "output": 5, "cache_read": 900, "cache_write": 10}
