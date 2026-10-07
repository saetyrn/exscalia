"""Claude Opus 5.5, Sonnet 5.5 and Fable 5.1 run safety classifiers that can
decline a benign request. Those models opt into the API's server-side
fallback, and a reply that still comes back declined must end the turn
without running anything it started."""
import asyncio
from types import SimpleNamespace

from app.overseer.agent import Overseer
from app.overseer.provider import (
    AnthropicProvider,
    FakeProvider,
    ProviderReply,
    ToolCall,
    after_fallback,
    takes_system_messages,
)
from app.overseer.runtime_config import PROVIDERS, provider_info


class _Block(SimpleNamespace):
    def model_dump(self, exclude_none=False):
        return dict(vars(self))


def _provider(model, content, stop_reason="end_turn"):
    captured: dict[str, list[dict]] = {"plain": [], "beta": []}

    def api(kind):
        async def create(**kwargs):
            captured[kind].append(kwargs)
            return SimpleNamespace(
                content=content,
                stop_reason=stop_reason,
                usage=SimpleNamespace(input_tokens=1, output_tokens=1),
            )

        return SimpleNamespace(create=create)

    provider = AnthropicProvider("test-key", model)
    provider.client = SimpleNamespace(messages=api("plain"), beta=SimpleNamespace(messages=api("beta")))
    return provider, captured


HI = [{"role": "user", "content": "hi"}]


def test_the_new_models_opt_into_server_side_fallback():
    for model in ("claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1"):
        provider, captured = _provider(model, [_Block(type="text", text="ok")])
        asyncio.run(provider.chat("s", HI, []))
        [sent] = captured["beta"]
        assert captured["plain"] == []
        assert sent["betas"] == ["server-side-fallback-2026-07-01"]
        assert sent["extra_body"] == {"fallbacks": "default"}


def test_older_models_keep_the_plain_messages_api():
    for model in ("claude-opus-4-8", "claude-sonnet-5", "claude-haiku-4-5", "claude-fable-5"):
        provider, captured = _provider(model, [_Block(type="text", text="ok")])
        asyncio.run(provider.chat("s", HI, []))
        assert captured["beta"] == []
        assert "betas" not in captured["plain"][0]


def test_a_mid_output_fallback_drops_the_declined_attempts_calls():
    blocks = [
        _Block(type="thinking", thinking="", signature="a"),
        _Block(type="text", text="Looking. "),
        _Block(type="tool_use", id="t0", name="read_notes", input={}),
        _Block(type="fallback", **{"from": {"model": "claude-opus-5-5"}}, to={"model": "claude-opus-5"}),
        _Block(type="text", text="Here it is."),
        _Block(type="tool_use", id="t1", name="read_structure", input={}),
    ]
    kept = after_fallback(blocks)
    assert [(b.type, getattr(b, "id", None)) for b in kept] == [
        ("text", None),
        ("text", None),
        ("tool_use", "t1"),
    ]
    assert after_fallback(blocks[:3]) == blocks[:3]  # no fallback, untouched

    provider, _ = _provider("claude-opus-5-5", blocks, stop_reason="tool_use")
    reply = asyncio.run(provider.chat("s", HI, []))
    assert [c.id for c in reply.tool_calls] == ["t1"]
    assert "fallback" not in [b["type"] for b in reply.raw_content]


def test_a_refusal_after_every_fallback_runs_nothing():
    blocks = [
        _Block(type="text", text="Sure, deleting"),
        _Block(type="tool_use", id="t0", name="delete_note", input={}),
    ]
    provider, _ = _provider("claude-fable-5-1", blocks, stop_reason="refusal")
    reply = asyncio.run(provider.chat("s", HI, []))
    assert reply.refused
    assert reply.tool_calls == []
    assert [b["type"] for b in reply.raw_content] == ["text"]


def test_the_agent_ends_a_declined_turn_with_a_notice(state):
    provider = FakeProvider(
        [
            ProviderReply(
                text="Partial",
                tool_calls=[ToolCall(id="x", name="read_structure", input={})],
                stop_reason="refusal",
            )
        ]
    )
    result = asyncio.run(Overseer(provider, state).chat("hello"))
    assert result["reply"] == "(The model declined this request.)"
    assert len(provider.seen) == 1  # no tool ran, no second round


def test_sonnet_5_5_gets_the_persisted_context_block():
    # It accepts system messages, but a per-request one is a history edit
    # under preserved thinking.
    assert not takes_system_messages("claude-sonnet-5-5")
    assert not takes_system_messages("claude-opus-5-5")
    assert takes_system_messages("claude-opus-5")


def test_the_picker_lists_the_new_models_first():
    anthropic = provider_info("anthropic")
    assert anthropic["default_model"] == "claude-opus-5-5"
    assert [m["id"] for m in anthropic["models"]][:4] == [
        "claude-opus-5-5",
        "claude-sonnet-5-5",
        "claude-haiku-4-5",
        "claude-fable-5-1",
    ]
    openai = provider_info("openai")
    assert openai["default_model"] == "gpt-6.1-sol"
    assert [m["id"] for m in openai["models"]][:3] == ["gpt-6.1-sol", "gpt-6-astra", "gpt-6-luna"]
    # Every provider's default is one of its listed models.
    for p in PROVIDERS:
        assert p["default_model"] in {m["id"] for m in p["models"]}
