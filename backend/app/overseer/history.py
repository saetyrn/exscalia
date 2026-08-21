"""Keeping a conversation's provider history usable.

Two failure modes made threads permanently unusable:

1. **Unmatched tool calls.** The agent appends the assistant's `tool_use`
   blocks, runs the tools, then appends the matching `tool_result`s. If a Stop
   click or a tool exception lands in between, the history is persisted with a
   `tool_use` that has no result. Anthropic and OpenAI reject that on *every*
   later request, so the thread fails forever with no way back.

2. **Unbounded growth.** Nothing trimmed history, so it grew until the provider
   returned a context-length error — and the oversized history was written back
   to disk, guaranteeing the same failure on every retry.

Both are repaired in place: history is normalized on load and before persisting.
"""

from __future__ import annotations

import json
from typing import Any


def _blocks(message: dict) -> list[dict]:
    content = message.get("content")
    return content if isinstance(content, list) else []


def _ids(message: dict, block_type: str, key: str) -> set[str]:
    return {
        str(b.get(key))
        for b in _blocks(message)
        if isinstance(b, dict) and b.get("type") == block_type and b.get(key)
    }


def user_turn_starts(messages: list[dict]) -> list[int]:
    """Indices of real user turns.

    Tool results are also sent with role "user", so a plain role check would
    treat them as conversation boundaries and cut a tool call away from its
    result.
    """
    return [
        index
        for index, message in enumerate(messages)
        if message.get("role") == "user" and not _ids(message, "tool_result", "tool_use_id")
    ]


def repair_history(messages: list[dict]) -> tuple[list[dict], list[str]]:
    """Return history that a provider will accept, plus what was changed.

    Drops assistant tool calls whose results never arrived (and any orphaned
    results), then removes assistant turns left with nothing to say.
    """
    notes: list[str] = []
    out = [dict(m) for m in messages]

    # A tool_use is satisfied only by a tool_result in a LATER message. Every
    # call sits before at least message 1, so the union over the whole tail is
    # the same set for all of them — the outer loop this replaces recomputed
    # that identical union once per message, at 134ms a call on a long thread.
    satisfied: set[str] = set()
    for message in out[1:]:
        satisfied |= _ids(message, "tool_result", "tool_use_id")

    dropped_calls = 0
    dropped_results = 0
    seen_calls: set[str] = set()
    for message in out:
        blocks = _blocks(message)
        if not blocks:
            continue
        kept: list[dict] = []
        for block in blocks:
            btype = isinstance(block, dict) and block.get("type")
            if btype == "tool_use":
                if str(block.get("id")) in satisfied:
                    seen_calls.add(str(block.get("id")))
                    kept.append(block)
                else:
                    dropped_calls += 1
                continue
            if btype == "tool_result":
                # A result whose call we removed (or never saw) is also invalid.
                if str(block.get("tool_use_id")) in seen_calls:
                    kept.append(block)
                else:
                    dropped_results += 1
                continue
            kept.append(block)
        message["content"] = kept

    if dropped_calls:
        notes.append(f"dropped {dropped_calls} interrupted tool call(s)")
    if dropped_results:
        notes.append(f"dropped {dropped_results} orphaned tool result(s)")

    # Empty turns are rejected by some providers; a stub keeps the transcript
    # readable without pretending the interrupted work happened.
    cleaned: list[dict] = []
    for message in out:
        content = message.get("content")
        if isinstance(content, list) and not content:
            if message.get("role") == "assistant":
                cleaned.append({"role": "assistant", "content": "(interrupted)"})
            continue
        cleaned.append(message)
    return cleaned, notes


def history_chars(messages: list[dict]) -> int:
    try:
        return len(json.dumps(messages, ensure_ascii=False))
    except (TypeError, ValueError):
        return sum(len(str(m)) for m in messages)


def trim_history(messages: list[dict], limit: int) -> tuple[list[dict], list[str]]:
    """Drop whole exchanges from the front until the history fits.

    Trimming happens at user-turn boundaries so tool calls never lose their
    results, and the most recent exchange is always kept even if it alone
    exceeds the limit — better one oversized request than a thread that can
    never be used again.
    """
    if history_chars(messages) <= limit:
        return messages, []

    starts = user_turn_starts(messages)
    for start in starts[1:]:  # never cut everything
        candidate = messages[start:]
        if history_chars(candidate) <= limit:
            repaired, _ = repair_history(candidate)
            return repaired, [f"trimmed {start} earlier message(s) to fit the context window"]

    keep = messages[starts[-1] :] if starts else messages[-2:]
    repaired, _ = repair_history(keep)
    return repaired, ["trimmed to the most recent exchange"]


def normalize(messages: list[dict], limit: int) -> tuple[list[dict], list[str]]:
    """Repair, then trim. Safe to call on load and before persisting."""
    repaired, notes = repair_history(messages)
    trimmed, trim_notes = trim_history(repaired, limit)
    return trimmed, notes + trim_notes
