"""The reasoning knob: one user-facing level, mapped to whatever each
provider's API actually accepts. The mappings are pinned exactly — a wrong
parameter here is a 400 from the provider at chat time."""
from app.overseer.provider import (
    anthropic_reasoning_kwargs,
    google_thinking_config,
    openai_reasoning_kwargs,
)
from app.overseer.runtime_config import reasoning_level, role_reasoning


def test_reasoning_level_parses_and_rejects_junk():
    assert reasoning_level({}) == "default"
    assert reasoning_level({"reasoning": "high"}) == "high"
    assert reasoning_level({"reasoning": "turbo"}) == "default"
    assert reasoning_level({"reasoning": None}) == "default"


def test_role_reasoning_overrides_and_falls_back():
    config = {"reasoning": "high", "reasoning_levels": {"background": "low"}}
    assert role_reasoning(config, "background") == "low"
    # An absent or invalid override inherits the chat knob, not "default".
    assert role_reasoning(config, "media") == "high"
    config["reasoning_levels"]["media"] = "turbo"
    assert role_reasoning(config, "media") == "high"
    assert role_reasoning({}, "autocorrect") == "default"


def test_default_sends_nothing_anywhere():
    assert anthropic_reasoning_kwargs("claude-opus-4-8", "default", 32000) == {}
    assert openai_reasoning_kwargs("default") == {}
    assert google_thinking_config("default") == {}


def test_anthropic_effort_models_get_adaptive_thinking_plus_effort():
    for model in ("claude-opus-4-8", "claude-sonnet-5", "claude-fable-5"):
        kwargs = anthropic_reasoning_kwargs(model, "high", 32000)
        assert kwargs == {
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": "high"},
        }
    assert anthropic_reasoning_kwargs("claude-opus-4-8", "max", 32000)[
        "output_config"
    ] == {"effort": "max"}


def test_haiku_uses_a_thinking_budget_never_effort():
    # Haiku 4.5 rejects output_config.effort — it must get budget_tokens,
    # and the budget must stay under max_tokens.
    kwargs = anthropic_reasoning_kwargs("claude-haiku-4-5", "medium", 16000)
    assert "output_config" not in kwargs
    assert kwargs["thinking"] == {"type": "enabled", "budget_tokens": 8192}

    capped = anthropic_reasoning_kwargs("claude-haiku-4-5", "max", 16000)
    assert capped["thinking"]["budget_tokens"] == 16000 - 1024

    # A pathologically small output cap leaves no room to think at all.
    assert anthropic_reasoning_kwargs("claude-haiku-4-5", "low", 2000) == {}


def test_openai_ladder_tops_out_at_high():
    assert openai_reasoning_kwargs("low") == {"reasoning_effort": "low"}
    assert openai_reasoning_kwargs("max") == {"reasoning_effort": "high"}


def test_google_budgets_respect_the_flash_ceiling():
    assert google_thinking_config("low") == {
        "thinking_config": {"thinking_budget": 2048}
    }
    assert google_thinking_config("max")["thinking_config"]["thinking_budget"] == 24576


def _openai_provider_with_stub(create):
    """An OpenAIProvider whose chat.completions.create is `create`."""
    from types import SimpleNamespace

    from app.overseer.provider import OpenAIProvider

    provider = OpenAIProvider("test-key", "gpt-5.6-luna", reasoning="medium")
    provider.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    return provider


def _bad_request(message: str, param: str | None):
    import httpx
    from openai import BadRequestError

    body = {"error": {"message": message, "type": "invalid_request_error", "param": param}}
    response = httpx.Response(
        400, request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"),
        json=body,
    )
    return BadRequestError(message, response=response, body=body["error"])


def _completion_reply(text="ok"):
    from types import SimpleNamespace

    message = SimpleNamespace(content=text, tool_calls=None)
    choice = SimpleNamespace(finish_reason="stop", message=message)
    return SimpleNamespace(choices=[choice], usage=None)


def test_openai_retries_with_none_when_the_model_rejects_reasoning_effort():
    """gpt-5.6-luna 400s on any reasoning effort with function tools, even the
    implicit default — the retry must send an explicit "none" (merely omitting
    the param leaves the default effort in force and 400s again), and the
    provider must remember for subsequent calls."""
    import asyncio

    calls: list[dict] = []

    async def create(**kwargs):
        calls.append(kwargs)
        if kwargs.get("reasoning_effort") != "none":
            raise _bad_request(
                "Function tools with reasoning_effort are not supported for "
                "gpt-5.6-luna in /v1/chat/completions. To use function tools, "
                "use /v1/responses or set reasoning_effort to 'none'.",
                "reasoning_effort",
            )
        return _completion_reply()

    provider = _openai_provider_with_stub(create)
    reply = asyncio.run(provider.chat("sys", [{"role": "user", "content": "hi"}], []))
    assert reply.text == "ok"
    assert calls[0]["reasoning_effort"] == "medium"
    assert calls[1]["reasoning_effort"] == "none"

    # Later calls skip the doomed attempt entirely.
    asyncio.run(provider.chat("sys", [{"role": "user", "content": "again"}], []))
    assert calls[2]["reasoning_effort"] == "none"
    assert len(calls) == 3


def test_openai_default_reasoning_also_self_heals():
    """With the knob on Default no param is sent at all, but the model's
    implicit effort still trips the 400 — the retry must add "none"."""
    import asyncio

    calls: list[dict] = []

    async def create(**kwargs):
        calls.append(kwargs)
        if kwargs.get("reasoning_effort") != "none":
            raise _bad_request(
                "Function tools with reasoning_effort are not supported for "
                "gpt-5.6-luna in /v1/chat/completions.",
                "reasoning_effort",
            )
        return _completion_reply()

    provider = _openai_provider_with_stub(create)
    provider.reasoning = "default"
    reply = asyncio.run(provider.chat("sys", [{"role": "user", "content": "hi"}], []))
    assert reply.text == "ok"
    assert "reasoning_effort" not in calls[0]
    assert calls[1]["reasoning_effort"] == "none"


def test_openai_unrelated_400s_still_propagate():
    import asyncio

    import pytest
    from openai import BadRequestError

    async def create(**kwargs):
        raise _bad_request("Invalid value for max_completion_tokens.", "max_completion_tokens")

    provider = _openai_provider_with_stub(create)
    with pytest.raises(BadRequestError):
        asyncio.run(provider.chat("sys", [{"role": "user", "content": "hi"}], []))
