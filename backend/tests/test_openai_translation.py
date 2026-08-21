"""The OpenAI provider must translate the neutral (Anthropic-shaped) history
to OpenAI chat.completions format without losing tool calls or results."""
from app.overseer.provider import to_openai_messages


def test_plain_text_turns():
    msgs = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]
    assert to_openai_messages(msgs) == [
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
    out = to_openai_messages(msgs)
    assert out[0] == {"role": "user", "content": "search my notes"}
    assert out[1]["role"] == "assistant"
    assert out[1]["content"] == "Let me look."
    assert out[1]["tool_calls"] == [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "keyword_search", "arguments": '{"query": "travel"}'},
        }
    ]
    assert out[2] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": '{"results": []}',
    }


def test_parallel_tool_results_become_separate_tool_messages():
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
    out = to_openai_messages(msgs)
    assert out[0]["content"] is None  # no text alongside the calls
    assert len(out[0]["tool_calls"]) == 2
    assert [m["tool_call_id"] for m in out[1:]] == ["a", "b"]
    assert all(m["role"] == "tool" for m in out[1:])
