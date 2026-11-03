"""One place to run a cheap, unattended model call.

Three jobs now need the same four steps — check the soft budget, build a
provider on the configured background model, make one call, meter it — and they
were drifting apart as each grew its own copy. Everything here fails soft: an
unattended job that cannot run must leave the app exactly as it found it.
"""

from __future__ import annotations

import logging

from .provider import build_provider
from .runtime_config import (
    effective_ai,
    effective_background_model,
    load_ai_config,
    monthly_token_budget,
    role_reasoning,
)

log = logging.getLogger(__name__)


def allowed(state, config: dict) -> bool:
    """Whether unattended work may spend right now.

    The budget pauses background work first and never chat, so this is the only
    gate any of these jobs consult.
    """
    ledger = getattr(state, "usage", None)
    if ledger is None:
        return True
    try:
        return ledger.allow_background(monthly_token_budget(config))
    except Exception:  # pragma: no cover - a config hiccup must not wedge a job
        return True


def provider_for(state, config: dict, fallback):
    """The background-model provider, or `fallback` (the chat one) when the
    configured background model is the chat model or cannot be built."""
    background_model = effective_background_model(state.settings, config)
    if fallback is not None and background_model == fallback.model:
        return fallback
    provider_id, key, _model = effective_ai(state.settings, config)
    try:
        cheap = build_provider(
            state.settings,
            provider_id=provider_id,
            api_key=key,
            model=background_model,
            reasoning=role_reasoning(config, "background"),
        )
    except Exception:
        cheap = None
    if cheap is None:
        log.warning(
            "background model %s unavailable; using the chat model",
            background_model,
        )
        return fallback
    return cheap


async def ask(state, provider, system: str, message: str, *, caller: str) -> str | None:
    """One metered call. Returns the reply text, or None if anything failed
    or the reply was cut off."""
    if provider is None:
        return None
    try:
        reply = await provider.chat(
            system, [{"role": "user", "content": message}], [], None
        )
    except Exception:
        log.warning("background call for %s failed", caller, exc_info=True)
        return None
    ledger = getattr(state, "usage", None)
    if ledger is not None:
        ledger.record(caller, getattr(reply, "usage", None))
    # Metered first, because the tokens were spent. A reply stopped by the
    # output ceiling is a fragment that reads like a whole — a summary missing
    # its end, a memory note missing its last sections — so it is a failure.
    if getattr(reply, "hit_output_limit", False):
        log.warning("background call for %s was cut off by the output limit", caller)
        return None
    if getattr(reply, "refused", False):
        log.warning("background call for %s was declined by the provider", caller)
        return None
    return reply.text or ""


def config_for(state) -> dict:
    return load_ai_config(state.settings.data_dir)
