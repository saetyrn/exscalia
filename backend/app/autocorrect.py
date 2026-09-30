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
import re

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

# Fixes the user undid in the editor come back as `ignore`. The client caps its
# list too; these bound what an arbitrary caller can push into the prompt.
MAX_IGNORED = 50
MAX_IGNORED_LEN = 200

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
case and punctuation, and always as whole words — never part of a longer
word. Only if that exact text appears more than once, add a 0-based
`occurrence` saying which one you mean. When unsure whether something is a
mistake, leave it alone.

Answer with ONLY this JSON:
{"fixes": [{"category": "misspelling", "before": "recieve", "after": "receive"}]}
An empty list means the note is clean.
"""

# The prompt asks the model to skip these; the server enforces it, because an
# autocorrect fix is applied with no one reviewing it. Over-matching only
# costs a missed typo, so the patterns are deliberately loose.
_FRONTMATTER = re.compile(r"\A-{3,}[ \t]*\n(?:.*?\n)?-{3,}[ \t]*(?:\n|\Z)", re.S)
# A backtick fence's info string cannot hold a backtick, so ```x``` on one
# line is inline code, not an opener that would swallow the rest of the note.
_FENCE_OPEN = re.compile(r"[ \t]*(`{3,}(?!.*`)|~{3,})")
_PROTECTED = re.compile(
    r"(`+)[^\n]*?\1"  # inline code
    r"|https?://\S+|www\.\S+"  # URLs
    r"|\[\[[^\n]*?\]\]"  # [[wikilinks]] and ![[embeds]]
    r"|(?<!\w)#[\w/-]+"  # #tags; `# Heading` has a space, so it is not one
    r"|<!--.*?-->",  # comments, including the hidden task metadata
    re.S,
)


def protected_ranges(content: str) -> list[tuple[int, int]]:
    """Code-point ranges of `content` autocorrect must never edit: YAML
    frontmatter, fenced and inline code, URLs, wikilinks, #tags and HTML
    comments. An unclosed fence runs to the end of the note."""
    ranges: list[tuple[int, int]] = []
    front = _FRONTMATTER.match(content)
    if front:
        ranges.append((0, front.end()))
    fence: tuple[int, str] | None = None
    pos = 0
    for line in content.splitlines(keepends=True):
        if fence is None:
            opened = _FENCE_OPEN.match(line)
            if opened:
                fence = (pos, opened.group(1))
        elif line.strip().startswith(fence[1]) and not line.strip().strip(fence[1][0]):
            ranges.append((fence[0], pos + len(line)))
            fence = None
        pos += len(line)
    if fence is not None:
        ranges.append((fence[0], len(content)))
    ranges.extend(m.span() for m in _PROTECTED.finditer(content))
    return ranges


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


async def scan(state, content: str, ignore=()) -> list[dict]:
    """Scan `content` once and return anchored fixes, or [] for any reason —
    no model, no budget, oversized text, or a failure anywhere below.

    `ignore` holds `before` texts the user undid: the model is told to leave
    them, and any fix that quotes one anyway is dropped before anchoring."""
    try:
        ignored = {
            s
            for s in list(ignore)[:MAX_IGNORED]
            if isinstance(s, str) and s and len(s) <= MAX_IGNORED_LEN
        }
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
        blocks = [{"type": "text", "text": content}]
        if ignored:
            # Chat Completions joins text blocks with nothing between them, so
            # the block carries its own separator from the note.
            blocks.append(
                {
                    "type": "text",
                    "text": "\n\nNever change these; the user rejected them: "
                    + json.dumps(sorted(ignored), ensure_ascii=False),
                }
            )
        reply = await provider.chat(
            SYSTEM_PROMPT,
            [{"role": "user", "content": blocks}],
            [],
            None,
        )
        if ledger is not None:
            ledger.record("autocorrect", getattr(reply, "usage", None))
        fixes = (_parse_reply(reply.text) or {}).get("fixes")
        if not isinstance(fixes, list):
            return []
        # The instruction above is a request; this is the guarantee.
        raw = [
            fix
            for fix in fixes
            if isinstance(fix, dict) and str(fix.get("before")) not in ignored
        ][:MAX_FIXES]
        accepted, _rejections = anchor(
            content,
            raw,
            categories=CATEGORIES,
            whole_words=True,
            protected=protected_ranges(content),
        )
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
