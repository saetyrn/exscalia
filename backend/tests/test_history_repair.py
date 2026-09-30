"""Regression tests for the failure that permanently bricked conversations.

Hitting Stop (or any tool exception) between the assistant's tool call and its
result used to persist an unmatched `tool_use`. Anthropic and OpenAI reject that
on every subsequent request, so the thread could never be used again.
"""

from app.overseer.history import history_chars, normalize, repair_history, trim_history


def _call(call_id: str) -> dict:
    return {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "looking"},
            {"type": "tool_use", "id": call_id, "name": "read_notes", "input": {}},
        ],
    }


def _result(call_id: str, text: str = "{}") -> dict:
    return {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": call_id, "content": text}],
    }


def test_interrupted_tool_call_is_dropped():
    history = [
        {"role": "user", "content": "summarize my notes"},
        _call("toolu_1"),  # cancelled before the result came back
    ]

    repaired, notes = repair_history(history)

    assert not any(
        b.get("type") == "tool_use" for m in repaired for b in m.get("content", [])
        if isinstance(b, dict)
    )
    assert "dropped 1 interrupted tool call(s)" in notes
    # The assistant's text survives; only the dangling call goes.
    assert repaired[-1]["content"] == [{"type": "text", "text": "looking"}]


def test_completed_tool_calls_are_left_alone():
    history = [
        {"role": "user", "content": "hi"},
        _call("toolu_1"),
        _result("toolu_1"),
        {"role": "assistant", "content": "done"},
    ]

    repaired, notes = repair_history(history)

    assert repaired == history
    assert notes == []


def test_orphaned_results_are_dropped_too():
    history = [
        {"role": "user", "content": "hi"},
        _result("toolu_missing"),  # no matching call anywhere
    ]

    repaired, notes = repair_history(history)

    assert repaired == [{"role": "user", "content": "hi"}]
    assert "dropped 1 orphaned tool result(s)" in notes


def test_assistant_turn_emptied_by_repair_becomes_a_stub():
    history = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "toolu_1", "name": "x", "input": {}}],
        },
    ]

    repaired, _ = repair_history(history)

    # Never leave an assistant turn with empty content — providers reject it.
    assert repaired[-1] == {"role": "assistant", "content": "(interrupted)"}


def test_a_later_result_only_counts_if_it_comes_after_the_call():
    # A result appearing BEFORE its call must not satisfy it.
    history = [_result("toolu_1"), _call("toolu_1")]

    repaired, notes = repair_history(history)

    assert notes  # both sides removed
    assert all(
        b.get("type") not in {"tool_use", "tool_result"}
        for m in repaired
        for b in m.get("content", [])
        if isinstance(b, dict)
    )


def test_trimming_drops_whole_exchanges_and_keeps_pairs_intact():
    history = []
    for i in range(12):
        history.append({"role": "user", "content": f"question {i} " + "x" * 4000})
        history.append(_call(f"toolu_{i}"))
        history.append(_result(f"toolu_{i}", "y" * 4000))
        history.append({"role": "assistant", "content": f"answer {i}"})

    trimmed, notes = trim_history(history, 20_000)

    assert history_chars(trimmed) <= 20_000
    assert notes and "trimmed" in notes[0]
    # Whatever survived must still be internally consistent.
    repaired, repair_notes = repair_history(trimmed)
    assert repair_notes == []
    assert repaired == trimmed
    # The newest exchanges are kept and the oldest are gone.
    flat = str(trimmed)
    assert "question 11" in flat
    assert "question 0 " not in flat
    # It keeps as much recent history as fits, cutting at an exchange boundary.
    assert trimmed[0]["role"] == "user"


def test_trimming_never_empties_the_history():
    huge = [
        {"role": "user", "content": "x" * 50_000},
        {"role": "assistant", "content": "y" * 50_000},
    ]

    trimmed, notes = trim_history(huge, 1_000)

    assert trimmed, "an oversized single exchange is better than an unusable thread"
    assert notes


def test_normalize_repairs_then_trims():
    history = [
        {"role": "user", "content": "old " + "x" * 30_000},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "new question"},
        _call("toolu_9"),  # interrupted
    ]

    normalized, notes = normalize(history, 20_000)

    assert history_chars(normalized) <= 20_000
    assert any("interrupted tool call" in n for n in notes)


def test_retry_policy_distinguishes_transient_from_fatal():
    """Retrying a bad API key would just burn time and rate limit; retrying a
    429 or a 503 is the whole point."""
    from app.overseer.provider import is_retryable

    transient = [
        Exception("429 RESOURCE_EXHAUSTED: rate limit exceeded"),
        Exception("503 Service Unavailable"),
        Exception("deadline exceeded"),
        Exception("500 internal error"),
    ]
    fatal = [
        Exception("401 invalid x-api-key"),
        Exception("400 INVALID_ARGUMENT: bad request"),
        Exception("404 model not found"),
    ]
    assert all(is_retryable(e) for e in transient)
    assert not any(is_retryable(e) for e in fatal)


def test_with_retries_backs_off_then_succeeds():
    import asyncio

    from app.overseer.provider import with_retries

    attempts = {"n": 0}

    async def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise Exception("429 rate limit")
        return "ok"

    result = asyncio.run(with_retries(flaky, base_delay=0.001))

    assert result == "ok"
    assert attempts["n"] == 3


def test_with_retries_gives_up_immediately_on_a_fatal_error():
    import asyncio

    import pytest

    from app.overseer.provider import with_retries

    attempts = {"n": 0}

    async def bad_key():
        attempts["n"] += 1
        raise Exception("401 invalid x-api-key")

    with pytest.raises(Exception, match="invalid x-api-key"):
        asyncio.run(with_retries(bad_key, base_delay=0.001))
    assert attempts["n"] == 1  # no pointless retries
