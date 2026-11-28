"""The Gemini provider must translate the neutral history to Gemini contents:
user/model roles, function_call parts, and function_response parts matched to
the originating call by tool name (Gemini has no stable call ids)."""
from app.overseer.provider import to_gemini_contents


def test_plain_text_turns():
    msgs = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]
    assert to_gemini_contents(msgs) == [
        {"role": "user", "parts": [{"text": "hello"}]},
        {"role": "model", "parts": [{"text": "hi there"}]},
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
                    "id": "gcall_1",
                    "name": "keyword_search",
                    "input": {"query": "travel"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "gcall_1", "content": '{"results": []}'}
            ],
        },
    ]
    out = to_gemini_contents(msgs)
    assert out[1]["role"] == "model"
    assert out[1]["parts"][0] == {"text": "Let me look."}
    assert out[1]["parts"][1] == {
        "function_call": {"name": "keyword_search", "args": {"query": "travel"}}
    }
    # The response is matched back to the call by NAME via the recorded id.
    assert out[2] == {
        "role": "user",
        "parts": [
            {
                "function_response": {
                    "name": "keyword_search",
                    "response": {"result": '{"results": []}'},
                }
            }
        ],
    }


def test_parallel_calls_keep_name_mapping():
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
                {"type": "tool_result", "tool_use_id": "b", "content": "B"},
                {"type": "tool_result", "tool_use_id": "a", "content": "A"},
            ],
        },
    ]
    out = to_gemini_contents(msgs)
    names = [p["function_response"]["name"] for p in out[1]["parts"]]
    assert names == ["read_structure", "read_notes"]  # order preserved, names correct


def test_thought_signature_round_trips():
    # Gemini thinking models attach a bytes thought_signature to the function
    # calls they emit; replaying a call without it is a hard 400. It is stored
    # base64 in the neutral history and must decode back to bytes for the SDK.
    import base64

    sig = base64.b64encode(b"opaque-signature").decode("ascii")
    msgs = [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "gcall_0",
                    "name": "get_digest_data",
                    "input": {"days": 1},
                    "thought_signature": sig,
                }
            ],
        }
    ]
    part = to_gemini_contents(msgs)[0]["parts"][0]
    assert part["function_call"] == {"name": "get_digest_data", "args": {"days": 1}}
    assert part["thought_signature"] == b"opaque-signature"


def test_missing_thought_signature_is_omitted():
    # Calls from other providers (or older histories) have no signature — the
    # part must simply omit the key rather than emit a null.
    msgs = [
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "x", "name": "read_notes", "input": {}}],
        }
    ]
    part = to_gemini_contents(msgs)[0]["parts"][0]
    assert "thought_signature" not in part
