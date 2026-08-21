"""Runtime AI configuration, editable from the Settings UI.

Stored at data/ai.json — deliberately OUTSIDE the vault git repo so API keys
are never committed or synced. UI-set values override environment values.

Shape:
    {
      "provider": "anthropic" | "openai",
      "keys":   {"anthropic": "sk-ant-…", "openai": "sk-…"},
      "models": {"anthropic": "claude-opus-4-8", "openai": "gpt-5.2"}
    }
Both providers' keys/models are kept, so switching back and forth loses nothing.
"""
import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

FILE_NAME = "ai.json"

PROVIDERS = [
    {
        "id": "anthropic",
        "label": "Anthropic (Claude)",
        "key_prefix": "sk-ant-",
        "key_placeholder": "sk-ant-…",
        "default_model": "claude-opus-4-8",
        "models": [
            {
                "id": "claude-opus-4-8",
                "label": "Claude Opus 4.8",
                "blurb": "Most capable Opus-tier model — recommended.",
            },
            {
                "id": "claude-sonnet-5",
                "label": "Claude Sonnet 5",
                "blurb": "Near-Opus quality at lower cost, faster.",
            },
            {
                "id": "claude-haiku-4-5",
                "label": "Claude Haiku 4.5",
                "blurb": "Fastest and cheapest, for simple tasks.",
            },
            {
                "id": "claude-fable-5",
                "label": "Claude Fable 5",
                "blurb": "Anthropic's most capable model (premium pricing).",
            },
        ],
    },
    {
        "id": "openai",
        "label": "OpenAI (ChatGPT)",
        "key_prefix": "sk-",
        "key_placeholder": "sk-…",
        "default_model": "gpt-5.6-sol",
        "models": [
            {
                "id": "gpt-5.6-sol",
                "label": "GPT-5.6 Sol",
                "blurb": "OpenAI's frontier model (July 2026) — recommended.",
            },
            {
                "id": "gpt-5.6-terra",
                "label": "GPT-5.6 Terra",
                "blurb": "Balanced intelligence and cost.",
            },
            {
                "id": "gpt-5.6-luna",
                "label": "GPT-5.6 Luna",
                "blurb": "Efficient and cheap, for high-volume use.",
            },
            {
                "id": "gpt-5.4",
                "label": "GPT-5.4",
                "blurb": "Previous-generation flagship.",
            },
        ],
    },
    {
        "id": "google",
        "label": "Google (Gemini)",
        # AI Studio now issues AQ. authorization keys by default. Keep
        # accepting legacy AIza standard keys during Google's migration.
        "key_prefixes": ["AQ.", "AIza"],
        "key_placeholder": "AQ.… or AIza…",
        "default_model": "gemini-3.6-flash",
        "models": [
            {
                "id": "gemini-3.6-flash",
                "label": "Gemini 3.6 Flash",
                "blurb": "Newest (July 2026) — speed with intelligence; recommended.",
            },
            {
                "id": "gemini-3.5-flash",
                "label": "Gemini 3.5 Flash",
                "blurb": "Most intelligent for sustained agentic/coding work.",
            },
            {
                "id": "gemini-3.5-flash-lite",
                "label": "Gemini 3.5 Flash-Lite",
                "blurb": "Fastest and most cost-effective 3.5.",
            },
            {
                "id": "gemini-2.5-pro",
                "label": "Gemini 2.5 Pro",
                "blurb": "Previous-generation deep-reasoning model.",
            },
        ],
    },
]


def provider_info(provider_id: str) -> dict | None:
    return next((p for p in PROVIDERS if p["id"] == provider_id), None)


def model_ids(provider_id: str) -> set[str]:
    p = provider_info(provider_id)
    return {m["id"] for m in p["models"]} if p else set()


def api_key_matches(provider_id: str, key: str) -> bool:
    """Lightweight typo/provider check, supporting providers with multiple key formats."""
    info = provider_info(provider_id)
    if not info:
        return False
    prefixes = info.get("key_prefixes") or [info.get("key_prefix", "")]
    return any(prefix and key.startswith(prefix) for prefix in prefixes)


def _path(data_dir: Path) -> Path:
    return data_dir / FILE_NAME


def load_ai_config(data_dir: Path) -> dict:
    p = _path(data_dir)
    if not p.is_file():
        return {}
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        log.exception("failed reading %s — ignoring", p)
        return {}
    # Migrate the pre-multi-provider shape {api_key, model} (Anthropic-only).
    if "api_key" in cfg or ("model" in cfg and "models" not in cfg):
        migrated: dict = {"provider": "anthropic", "keys": {}, "models": {}}
        if cfg.get("api_key"):
            migrated["keys"]["anthropic"] = cfg["api_key"]
        if cfg.get("model"):
            migrated["models"]["anthropic"] = cfg["model"]
        return migrated
    return cfg


def save_ai_config(data_dir: Path, config: dict) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    p = _path(data_dir)
    p.write_text(json.dumps(config, indent=2), encoding="utf-8")
    os.chmod(p, 0o600)  # the file holds API keys


def env_key(settings, provider_id: str) -> str | None:
    if provider_id == "anthropic":
        return settings.anthropic_api_key
    if provider_id == "openai":
        return settings.openai_api_key
    if provider_id == "google":
        return settings.google_api_key
    return None


def env_model(settings, provider_id: str) -> str | None:
    return {
        "anthropic": settings.anthropic_model,
        "openai": settings.openai_model,
        "google": settings.google_model,
    }.get(provider_id)


def effective_ai(settings, config: dict) -> tuple[str, str | None, str]:
    """(provider_id, api_key, model) — UI config wins over environment."""
    provider_id = config.get("provider") or settings.ai_provider
    if not provider_info(provider_id):
        provider_id = "anthropic"
    key = (config.get("keys") or {}).get(provider_id) or env_key(settings, provider_id)
    model = (
        (config.get("models") or {}).get(provider_id)
        or env_model(settings, provider_id)
        or provider_info(provider_id)["default_model"]
    )
    return provider_id, key, model


def effective_background_model(settings, config: dict) -> str:
    """The model unattended work runs on. Falls back to the chat model, so
    nothing changes until the user picks a cheaper one."""
    provider_id, _key, model = effective_ai(settings, config)
    background = (config.get("background_models") or {}).get(provider_id)
    return background if background in model_ids(provider_id) else model


def effective_media_model(settings, config: dict) -> str:
    """The model the Media job runs on — backdrop choice and page theming.
    Vision work with taste, so the user may want a stronger (or cheaper)
    model than chat; falls back to the chat model until they pick one."""
    provider_id, _key, model = effective_ai(settings, config)
    media = (config.get("media_models") or {}).get(provider_id)
    return media if media in model_ids(provider_id) else model


def effective_autocorrect_model(settings, config: dict) -> str:
    """The model autocorrect scans run on. Falls back to the background
    model, then the chat model — a scan fired per editing pause should ride
    whatever the user already blessed for unattended work."""
    provider_id, _key, model = effective_ai(settings, config)
    autocorrect = (config.get("autocorrect_models") or {}).get(provider_id)
    if autocorrect in model_ids(provider_id):
        return autocorrect
    background = (config.get("background_models") or {}).get(provider_id)
    return background if background in model_ids(provider_id) else model


REASONING_LEVELS = ("default", "low", "medium", "high", "max")


def reasoning_level(config: dict) -> str:
    """How hard the model thinks. "default" leaves each provider's own
    behavior untouched — exactly what shipped before this knob existed."""
    level = config.get("reasoning")
    return level if level in REASONING_LEVELS else "default"


def role_reasoning(config: dict, role: str) -> str:
    """Per-role reasoning override for the special-purpose roles (background,
    media, autocorrect). Not per-provider — the global knob isn't either; the
    provider-specific mapping happens in provider.py. An absent or invalid
    override inherits the chat knob."""
    level = (config.get("reasoning_levels") or {}).get(role)
    return level if level in REASONING_LEVELS else reasoning_level(config)


def monthly_token_budget(config: dict) -> int:
    """Soft ceiling on a month's input+output tokens. 0 means no budget."""
    try:
        return max(0, int(config.get("monthly_token_budget") or 0))
    except (TypeError, ValueError):
        return 0
