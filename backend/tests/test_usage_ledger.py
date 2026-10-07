"""The token-usage ledger, the soft monthly budget, and background-model
routing. The budget's one guarantee: unattended work pauses first, interactive
chat never blocks."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from app.overseer import housekeeping as hk
from app.overseer.provider import ProviderReply
from app.overseer.runtime_config import (
    effective_background_model,
    monthly_token_budget,
)
from app.overseer.usage import UsageLedger

from test_omnia_housekeeping import hk_state, with_model  # noqa: F401 (fixture)


SETTINGS = SimpleNamespace(
    ai_provider="anthropic",
    anthropic_api_key=None,
    openai_api_key=None,
    google_api_key=None,
    anthropic_model="claude-opus-4-8",
    openai_model="gpt-5.6-sol",
    google_model="gemini-3.6-flash",
)


def test_ledger_records_and_sums_a_month(tmp_path):
    ledger = UsageLedger(tmp_path)
    ledger.record("chat", {"input": 1000, "output": 200, "cache_read": 5000})
    ledger.record("chat", {"input": 500, "output": 100, "cache_write": 700})
    ledger.record("housekeeping", {"input": 300, "output": 50})
    ledger.record("chat", None)  # a no-usage call records nothing

    summary = ledger.month_summary()
    # The budget counts what costs real money: cache writes count toward the
    # total, cache reads stay out.
    assert summary["total"] == 1000 + 200 + 500 + 100 + 700 + 300 + 50
    assert summary["by_caller"]["chat"]["calls"] == 2
    assert summary["by_caller"]["chat"]["cache_read"] == 5000
    assert summary["by_caller"]["chat"]["cache_write"] == 700
    assert summary["by_caller"]["housekeeping"]["input"] == 300


def test_budget_counts_an_anthropic_cached_turn(tmp_path):
    # A cached Anthropic turn: breakpoints on the tools, system and history
    # tail leave input_tokens a handful and put the prompt in cache_write and
    # cache_read. Counting input + output alone saw 55 tokens here.
    from app.overseer.provider import _usage

    ledger = UsageLedger(tmp_path)
    ledger.record(
        "chat",
        _usage(
            SimpleNamespace(
                input_tokens=5,
                output_tokens=50,
                cache_creation_input_tokens=3000,
                cache_read_input_tokens=9000,
            )
        ),
    )
    assert ledger.month_summary()["total"] == 5 + 50 + 3000
    assert ledger.allow_background(1000) is False  # input + output said 55
    assert ledger.allow_background(3056) is True


def test_budget_gates_background_but_zero_means_unlimited(tmp_path):
    ledger = UsageLedger(tmp_path)
    ledger.record("chat", {"input": 900, "output": 100})
    assert ledger.allow_background(0) is True  # no budget set
    assert ledger.allow_background(1001) is True
    assert ledger.allow_background(1000) is False  # at the ceiling
    assert ledger.allow_background(50) is False


def test_ledger_survives_garbage_on_disk(tmp_path):
    ledger = UsageLedger(tmp_path)
    ledger.path.parent.mkdir(parents=True, exist_ok=True)
    ledger.path.write_text("not json", encoding="utf-8")
    ledger.record("chat", {"input": 10, "output": 5})
    assert ledger.month_summary()["total"] == 15


def test_background_model_falls_back_to_chat_model():
    config = {"provider": "anthropic", "models": {"anthropic": "claude-opus-4-8"}}
    assert effective_background_model(SETTINGS, config) == "claude-opus-4-8"

    config["background_models"] = {"anthropic": "claude-haiku-4-5"}
    assert effective_background_model(SETTINGS, config) == "claude-haiku-4-5"

    # An override that isn't a real model for the provider is ignored.
    config["background_models"] = {"anthropic": "claude-nonsense"}
    assert effective_background_model(SETTINGS, config) == "claude-opus-4-8"


def test_monthly_token_budget_parsing():
    assert monthly_token_budget({}) == 0
    assert monthly_token_budget({"monthly_token_budget": 5_000_000}) == 5_000_000
    assert monthly_token_budget({"monthly_token_budget": -3}) == 0
    assert monthly_token_budget({"monthly_token_budget": "junk"}) == 0


def test_housekeeping_pauses_over_budget_without_claiming_the_day(hk_state):  # noqa: F811 (fixture)
    with_model(hk_state, ProviderReply(text="{}", stop_reason="end_turn"))
    hk_state.usage = UsageLedger(hk_state.settings.vault_dir)
    hk_state.usage.record("chat", {"input": 900, "output": 200})
    (hk_state.settings.data_dir / "ai.json").write_text(
        '{"provider": "anthropic", "monthly_token_budget": 1000}', encoding="utf-8"
    )

    late = datetime(2026, 7, 28, 6, 0, tzinfo=timezone.utc)  # past 00:20 local
    result = asyncio.run(hk.run_housekeeping(hk_state, now=late))
    assert result == {"ran": False, "reason": "budget"}
    # The day was not claimed, so raising the budget lets the next tick run.
    assert hk.read_marker(hk_state) == {}

    (hk_state.settings.data_dir / "ai.json").write_text(
        '{"provider": "anthropic", "monthly_token_budget": 50000}', encoding="utf-8"
    )
    result = asyncio.run(hk.run_housekeeping(hk_state, now=late))
    assert result["reason"] == "seeded"  # proceeded to the normal first-run path
