"""Provider telemetry: token usage means the same on every provider, and a
Gemini reply cut off at the output ceiling is reported as cut off.

`input` is uncached prompt tokens at full price everywhere. Anthropic reports
it that way; OpenAI and Gemini fold cache hits into the prompt count, and
Gemini reports thinking (billed as output) apart from the answer."""
import asyncio
from types import SimpleNamespace

import pytest
from google.genai.types import BlockedReason, FinishReason

from app.overseer.provider import GeminiProvider, _enum_name, _usage


# --- _usage, one fixture per raw shape ---------------------------------------


def test_anthropic_usage_is_already_normalized():
    raw = SimpleNamespace(
        input_tokens=5,
        output_tokens=50,
        cache_creation_input_tokens=3000,
        cache_read_input_tokens=9000,
    )
    assert _usage(raw) == {
        "input": 5,
        "output": 50,
        "cache_read": 9000,
        "cache_write": 3000,
    }


def test_openai_cached_tokens_come_out_of_input():
    raw = SimpleNamespace(
        prompt_tokens=1000,
        completion_tokens=20,
        prompt_tokens_details=SimpleNamespace(cached_tokens=800),
    )
    assert _usage(raw) == {"input": 200, "output": 20, "cache_read": 800}


def test_openai_usage_as_a_dict():
    raw = {
        "prompt_tokens": 1000,
        "completion_tokens": 20,
        "prompt_tokens_details": {"cached_tokens": 800},
    }
    assert _usage(raw) == {"input": 200, "output": 20, "cache_read": 800}


def test_openai_usage_without_cache_details():
    raw = SimpleNamespace(prompt_tokens=1000, completion_tokens=20, prompt_tokens_details=None)
    assert _usage(raw) == {"input": 1000, "output": 20}
    assert _usage({"prompt_tokens": 1000, "completion_tokens": 20}) == {
        "input": 1000,
        "output": 20,
    }


def test_gemini_cache_hits_come_out_and_thoughts_count_as_output():
    raw = SimpleNamespace(
        prompt_token_count=1000,
        cached_content_token_count=400,
        candidates_token_count=30,
        thoughts_token_count=70,
    )
    assert _usage(raw) == {"input": 600, "output": 100, "cache_read": 400}


def test_gemini_usage_without_cache_or_thinking():
    raw = SimpleNamespace(
        prompt_token_count=1000,
        cached_content_token_count=None,
        candidates_token_count=30,
        thoughts_token_count=None,
    )
    assert _usage(raw) == {"input": 1000, "output": 30}


def test_real_sdk_usage_objects():
    from anthropic.types import Usage
    from google.genai.types import GenerateContentResponseUsageMetadata
    from openai.types import CompletionUsage
    from openai.types.completion_usage import PromptTokensDetails

    assert _usage(
        Usage(
            input_tokens=5,
            output_tokens=50,
            cache_creation_input_tokens=3000,
            cache_read_input_tokens=9000,
        )
    ) == {"input": 5, "output": 50, "cache_read": 9000, "cache_write": 3000}
    assert _usage(
        CompletionUsage(
            prompt_tokens=1000,
            completion_tokens=20,
            total_tokens=1020,
            prompt_tokens_details=PromptTokensDetails(cached_tokens=800),
        )
    ) == {"input": 200, "output": 20, "cache_read": 800}
    assert _usage(
        GenerateContentResponseUsageMetadata(
            prompt_token_count=1000,
            cached_content_token_count=400,
            candidates_token_count=30,
            thoughts_token_count=70,
        )
    ) == {"input": 600, "output": 100, "cache_read": 400}


# --- Gemini finish reasons ---------------------------------------------------


def test_finish_reason_enum_is_read_by_name():
    # str() on google-genai's enum gives "FinishReason.MAX_TOKENS", which
    # hit_output_limit never matched.
    assert str(FinishReason.MAX_TOKENS) != "MAX_TOKENS"
    assert _enum_name(FinishReason.MAX_TOKENS) == "MAX_TOKENS"
    assert _enum_name("MAX_TOKENS") == "MAX_TOKENS"
    assert _enum_name(None) == ""


def _part(text=None, call=None):
    return SimpleNamespace(text=text, function_call=call, thought_signature=None)


def _chunk(parts, finish=None, usage=None):
    content = SimpleNamespace(parts=parts) if parts is not None else None
    candidate = SimpleNamespace(finish_reason=finish, content=content)
    return SimpleNamespace(candidates=[candidate], usage_metadata=usage)


def _gemini(*, stream=None, response=None) -> GeminiProvider:
    provider = GeminiProvider("x", "gemini-3.6-flash")

    async def generate_content_stream(**kwargs):
        async def chunks():
            for chunk in stream:
                yield chunk

        return chunks()

    async def generate_content(**kwargs):
        return response

    provider.client = SimpleNamespace(
        aio=SimpleNamespace(
            models=SimpleNamespace(
                generate_content_stream=generate_content_stream,
                generate_content=generate_content,
            )
        )
    )
    return provider


HI = [{"role": "user", "content": "hi"}]


async def _ignore(_text: str) -> None:
    return None


def _stream_chat(provider: GeminiProvider):
    return asyncio.run(provider.chat("s", HI, [], on_text=_ignore))


def test_streamed_reply_cut_off_at_the_ceiling_is_flagged():
    provider = _gemini(stream=[_chunk([_part("partial")], FinishReason.MAX_TOKENS)])
    reply = _stream_chat(provider)
    assert reply.text == "partial"
    assert reply.hit_output_limit is True


def test_streamed_finish_reason_on_a_final_empty_chunk_is_kept():
    usage = SimpleNamespace(
        prompt_token_count=1000,
        cached_content_token_count=400,
        candidates_token_count=30,
        thoughts_token_count=70,
    )
    provider = _gemini(
        stream=[
            _chunk([_part("partial")]),
            # The last chunk can carry the reason and the usage but no content.
            _chunk(None, FinishReason.MAX_TOKENS, usage),
        ]
    )
    reply = _stream_chat(provider)
    assert reply.text == "partial"
    assert reply.hit_output_limit is True
    assert reply.usage == {"input": 600, "output": 100, "cache_read": 400}


def test_streamed_normal_finish_is_not_flagged():
    provider = _gemini(stream=[_chunk([_part("done")], FinishReason.STOP)])
    reply = _stream_chat(provider)
    assert reply.stop_reason == "STOP"
    assert reply.hit_output_limit is False


def test_streamed_function_calls_are_a_tool_round():
    call = SimpleNamespace(name="read_notes", args={"path": "a.md"})
    provider = _gemini(stream=[_chunk([_part(call=call)], FinishReason.STOP)])
    reply = _stream_chat(provider)
    assert reply.stop_reason == "tool_use"
    assert [c.name for c in reply.tool_calls] == ["read_notes"]


def _response(candidates, block_reason=None):
    feedback = SimpleNamespace(block_reason=block_reason) if block_reason else None
    return SimpleNamespace(
        candidates=candidates, prompt_feedback=feedback, usage_metadata=None
    )


def test_blocked_prompt_raises_with_the_reason():
    provider = _gemini(response=_response([], BlockedReason.SAFETY))
    with pytest.raises(RuntimeError, match="SAFETY"):
        asyncio.run(provider.chat("s", HI, []))


def test_empty_candidates_without_feedback_still_raises_cleanly():
    provider = _gemini(response=_response(None))
    with pytest.raises(RuntimeError, match="no candidates"):
        asyncio.run(provider.chat("s", HI, []))


def test_non_streamed_reply_cut_off_at_the_ceiling_is_flagged():
    candidate = SimpleNamespace(
        finish_reason=FinishReason.MAX_TOKENS,
        content=SimpleNamespace(parts=[_part("partial")]),
    )
    reply = asyncio.run(_gemini(response=_response([candidate])).chat("s", HI, []))
    assert reply.text == "partial"
    assert reply.hit_output_limit is True


def test_non_streamed_ceiling_hit_with_no_content_is_flagged():
    # Thinking can spend the whole budget, leaving a candidate with no content.
    candidate = SimpleNamespace(finish_reason=FinishReason.MAX_TOKENS, content=None)
    reply = asyncio.run(_gemini(response=_response([candidate])).chat("s", HI, []))
    assert reply.text == ""
    assert reply.hit_output_limit is True


def test_non_streamed_function_calls_are_a_tool_round():
    call = SimpleNamespace(name="read_notes", args={"path": "a.md"})
    candidate = SimpleNamespace(
        finish_reason=FinishReason.STOP,
        content=SimpleNamespace(parts=[_part(call=call)]),
    )
    reply = asyncio.run(_gemini(response=_response([candidate])).chat("s", HI, []))
    assert reply.stop_reason == "tool_use"
    assert reply.hit_output_limit is False
