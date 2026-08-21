"""One-shot autocorrect scan over the text being typed.

The inverse of proofreading: no staged review, no sidecar, no persistence at
all. The editor sends the content it holds, one model call finds outright
mistakes — misspellings, context-wrong words, doubled words, a/an slips —
and the anchored fixes go straight back. Anything the model gets wrong is
dropped by the same anchoring that guards proofreading, and any failure at
all returns an empty list: a scan that runs while the user types can never
be allowed to break typing.
"""

from __future__ import annotations

import json
import logging

from .overseer.provider import build_provider
from .overseer.runtime_config import (
    effective_ai,
    effective_autocorrect_model,
    load_ai_config,
    monthly_token_budget,
    role_reasoning,
)
from .proofread import anchor

log = logging.getLogger(__name__)

CATEGORIES = ("misspelling", "wrong_word", "doubled_word", "article")

# A scan should surface the handful of real slips in what was just typed, not
# audit the whole note.
MAX_FIXES = 20

# Past this the note is an archive, not something being typed into; the token
# bill would dwarf the value of catching one typo.
MAX_CONTENT = 40_000

SYSTEM_PROMPT = """\
You are a typo scanner for a personal notes app. Find ONLY these four kinds
of mistake in the note you are given:

- misspelling: a genuinely misspelled word ("recieve", "definately")
- wrong_word: a real word that is wrong in its context ("their" for
  "they're", "its" for "it's", "affect" for "effect")
- doubled_word: the same word accidentally typed twice ("the the")
- article: a/an disagreement with the word that follows ("a apple")

Do NOT propose grammar, style, tone, or punctuation rewrites — this is not
an editing pass. Skip anything inside code fences or inline code, URLs,
[[wikilinks]], YAML frontmatter, #tags, proper nouns, and deliberate
stylization. Quote `before` exactly as it appears in the note, including
case and punctuation; if that exact text appears more than once, add a
0-based `occurrence` saying which one you mean. When unsure whether
something is a mistake, leave it alone.

Answer with ONLY this JSON:
{"fixes": [{"category": "misspelling", "before": "recieve", "after": "receive", "occurrence": 0}]}
An empty list means the note is clean.
"""


def _parse_reply(text: str) -> dict | None:
    """The strict-JSON ask, defensively parsed anyway."""
    if not text:
        return None
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _build_autocorrect_provider(state):
    overseer = getattr(state, "overseer", None)
    config = getattr(state, "ai_config", None) or load_ai_config(state.settings.data_dir)
    model = effective_autocorrect_model(state.settings, config)
    if overseer is not None and overseer.provider.model == model:
        return overseer.provider
    provider_id, key, _model = effective_ai(state.settings, config)
    try:
        built = build_provider(
            state.settings,
            provider_id=provider_id,
            api_key=key,
            model=model,
            reasoning=role_reasoning(config, "autocorrect"),
        )
    except Exception:
        log.warning("Autocorrect model %s unavailable", model, exc_info=True)
        built = None
    if built is not None:
        return built
    return overseer.provider if overseer is not None else None


async def scan(state, content: str) -> list[dict]:
    """Scan `content` once and return anchored fixes, or [] for any reason —
    no model, no budget, oversized text, or a failure anywhere below."""
    try:
        provider = _build_autocorrect_provider(state)
        if provider is None:
            return []
        config = getattr(state, "ai_config", None) or load_ai_config(
            state.settings.data_dir
        )
        ledger = getattr(state, "usage", None)
        if ledger is not None and not ledger.allow_background(
            monthly_token_budget(config)
        ):
            return []
        if len(content) > MAX_CONTENT:
            return []
        reply = await provider.chat(
            SYSTEM_PROMPT,
            [{"role": "user", "content": [{"type": "text", "text": content}]}],
            [],
            None,
        )
        if ledger is not None:
            ledger.record("autocorrect", getattr(reply, "usage", None))
        fixes = (_parse_reply(reply.text) or {}).get("fixes")
        if not isinstance(fixes, list):
            return []
        raw = [fix for fix in fixes if isinstance(fix, dict)][:MAX_FIXES]
        accepted, _rejections = anchor(content, raw, categories=CATEGORIES)
        return [
            {
                "id": s["id"],
                "category": s["category"],
                "before": s["before"],
                "after": s["after"],
                "offset": s["offset"],
                "end": s["end"],
            }
            for s in accepted
        ]
    except Exception:
        log.warning("autocorrect scan failed", exc_info=True)
        return []
