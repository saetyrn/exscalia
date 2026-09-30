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
    assert openai_reasoning_kwargs("gpt-6.1-sol", "default") == {}
    assert google_thinking_config("default") == {}


def test_anthropic_effort_models_get_adaptive_thinking_plus_effort():
    for model in (
        "claude-opus-5-5",
        "claude-sonnet-5-5",
        "claude-fable-5-1",
        "claude-opus-4-8",
        "claude-sonnet-5",
        "claude-fable-5",
    ):
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


def test_openai_effort_rides_the_responses_reasoning_field():
    assert openai_reasoning_kwargs("gpt-6.1-sol", "low") == {"reasoning": {"effort": "low"}}
    # GPT-6 takes "max"; the GPT-5 ladder tops out at "high".
    assert openai_reasoning_kwargs("gpt-6-astra", "max") == {"reasoning": {"effort": "max"}}
    assert openai_reasoning_kwargs("gpt-5.6-sol", "max") == {"reasoning": {"effort": "high"}}


def test_google_budgets_respect_the_flash_ceiling():
    assert google_thinking_config("low") == {
        "thinking_config": {"thinking_budget": 2048}
    }
    assert google_thinking_config("max")["thinking_config"]["thinking_budget"] == 24576


def _openai_provider_with_stub(create, model="gpt-6-astra"):
    """An OpenAIProvider whose responses.create is `create`."""
    from types import SimpleNamespace

    from app.overseer.provider import OpenAIProvider

    provider = OpenAIProvider("test-key", model, reasoning="medium")
    provider.client = SimpleNamespace(responses=SimpleNamespace(create=create))
    return provider


def _bad_request(message: str, param: str | None):
    import httpx
    from openai import BadRequestError

    body = {"error": {"message": message, "type": "invalid_request_error", "param": param}}
    response = httpx.Response(
        400, request=httpx.Request("POST", "https://api.openai.com/v1/responses"),
        json=body,
    )
    return BadRequestError(message, response=response, body=body["error"])


def _response(text="ok"):
    from types import SimpleNamespace

    part = SimpleNamespace(type="output_text", text=text)
    item = SimpleNamespace(type="message", content=[part])
    return SimpleNamespace(output=[item], incomplete_details=None, usage=None)


def test_openai_retries_at_the_default_effort_when_the_model_rejects_one():
    """Effort ladders differ per model (Astra has no "none", GPT-5 no "max").
    A rejected level retries once without the reasoning field, and the
    provider remembers for later calls."""
    import asyncio

    calls: list[dict] = []

    async def create(**kwargs):
        calls.append(kwargs)
        if "reasoning" in kwargs:
            raise _bad_request(
                "Unsupported value: 'medium' is not supported with this model.",
                "reasoning.effort",
            )
        return _response()

    provider = _openai_provider_with_stub(create)
    reply = asyncio.run(provider.chat("sys", [{"role": "user", "content": "hi"}], []))
    assert reply.text == "ok"
    assert calls[0]["reasoning"] == {"effort": "medium"}
    assert "reasoning" not in calls[1]

    # Later calls skip the doomed attempt entirely.
    asyncio.run(provider.chat("sys", [{"role": "user", "content": "again"}], []))
    assert "reasoning" not in calls[2]
    assert len(calls) == 3


def test_openai_default_reasoning_sends_no_field_and_does_not_retry():
    import asyncio

    import pytest
    from openai import BadRequestError

    calls: list[dict] = []

    async def create(**kwargs):
        calls.append(kwargs)
        raise _bad_request("reasoning is broken today", "reasoning.effort")

    provider = _openai_provider_with_stub(create)
    provider.reasoning = "default"
    with pytest.raises(BadRequestError):
        asyncio.run(provider.chat("sys", [{"role": "user", "content": "hi"}], []))
    assert len(calls) == 1 and "reasoning" not in calls[0]


def test_openai_unrelated_400s_still_propagate():
    import asyncio

    import pytest
    from openai import BadRequestError

    async def create(**kwargs):
        raise _bad_request("Invalid value for max_output_tokens.", "max_output_tokens")

    provider = _openai_provider_with_stub(create)
    with pytest.raises(BadRequestError):
        asyncio.run(provider.chat("sys", [{"role": "user", "content": "hi"}], []))
