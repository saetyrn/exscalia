"""Thin LLM-provider abstraction. One interface, one env var per provider.

Message dicts use the Anthropic wire shape (role + content blocks) as the
neutral format; additional providers adapt to it in their own class. That
keeps the agent loop and stored chat history provider-agnostic — you can
switch providers mid-conversation.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import random
import uuid
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


def _encode_signature(sig: Any) -> str | None:
    """Gemini thinking models attach a bytes thought_signature to the parts
    they emit; it must be echoed back verbatim on the next turn. Store it as
    base64 so it survives our JSON-persisted neutral history."""
    if not sig:
        return None
    if isinstance(sig, bytes):
        return base64.b64encode(sig).decode("ascii")
    return str(sig)


def _decode_signature(value: Any) -> bytes | None:
    if not value:
        return None
    if isinstance(value, bytes):
        return value
    try:
        return base64.b64decode(value)
    except (ValueError, TypeError):
        return None

def _append_text(raw: list[dict[str, Any]], text: str, sig: str | None) -> None:
    """Add streamed text, merging into the trailing text block where possible.

    Streaming delivers text a chunk at a time, and each chunk used to become
    its own block in the persisted history — hundreds of them per reply, each
    repeating the same base64 thought_signature. Signatures still have to
    round-trip verbatim (Gemini 400s on a replayed call without one), so blocks
    only merge when that cannot lose or confuse a signature.
    """
    tail = raw[-1] if raw else None
    if tail is not None and tail.get("type") == "text":
        existing = tail.get("thought_signature")
        if not sig or not existing:
            tail["text"] += text
            if sig and not existing:
                tail["thought_signature"] = sig
            return
        if existing == sig:
            tail["text"] += text
            return
    block: dict[str, Any] = {"type": "text", "text": text}
    if sig:
        block["thought_signature"] = sig
    raw.append(block)


log = logging.getLogger(__name__)

TextCallback = Callable[[str], Awaitable[None]]


@dataclass
class ToolDef:
    name: str
    description: str
    input_schema: dict[str, Any]
    # Runtime permission mode:
    # read: no vault mutation; proposal: review-tray only; direct: explicit
    # user requests only; confirmed: requires a separate user approval.
    mode: str = "read"
    # "suggestion": may run freely, including in proactive/background passes
    #   (output is read-only or accumulates in the review tray).
    # "action": only on explicit user request — never proactively (deletes,
    #   moves, dedupe/merge, bulk rewrites).
    tier: str = "suggestion"


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class ProviderReply:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str | None = None
    # Raw assistant content blocks, appended verbatim to history.
    raw_content: list[dict[str, Any]] = field(default_factory=list)
    # Token counts when the provider reports them, for cost/context telemetry.
    usage: dict[str, int] = field(default_factory=dict)

    @property
    def hit_output_limit(self) -> bool:
        """True when the reply was cut off by the output-token ceiling rather
        than finishing. A truncated turn may carry half-built tool arguments."""
        return str(self.stop_reason or "") in {"max_tokens", "length", "MAX_TOKENS"}

    @property
    def refused(self) -> bool:
        """True when a safety check stopped the reply. Any text is a partial
        and any tool calls were never meant to run."""
        return str(self.stop_reason or "") in {"refusal", "content_filter"}


# Output ceilings. 4096 was hardcoded for every model and is the tightest
# artificial limit in the system — it caps how long a note OMNIA can rewrite.
DEFAULT_MAX_OUTPUT = 8192
MAX_OUTPUT_TOKENS: dict[str, int] = {
    "claude-opus-5-5": 32000,
    "claude-sonnet-5-5": 32000,
    "claude-fable-5-1": 16000,
    "claude-opus-4-8": 32000,
    "claude-sonnet-5": 32000,
    "claude-haiku-4-5": 16000,
    "claude-fable-5": 16000,
    "gpt-6.1-sol": 32000,
    "gpt-6-astra": 32000,
    "gpt-6-luna": 16000,
    "gpt-5.6-sol": 32000,
    "gpt-5.6-terra": 16000,
    "gpt-5.6-luna": 16000,
    "gpt-5.4": 16000,
    "gemini-3.6-flash": 32000,
    "gemini-3.5-flash": 16000,
    "gemini-3.5-flash-lite": 8192,
    "gemini-2.5-pro": 32000,
}


# A long agentic turn can legitimately take a while, but not forever.
REQUEST_TIMEOUT = 180.0
# The Anthropic and OpenAI SDKs retry 429s and 5xx with backoff when asked;
# google-genai does not, so GeminiProvider retries itself below.
MAX_RETRIES = 3


RETRYABLE_MARKERS = (
    "429",
    "rate limit",
    "resource_exhausted",
    "unavailable",
    "deadline",
    "timeout",
    "500",
    "502",
    "503",
    "504",
    "internal error",
)


def is_retryable(exc: Exception) -> bool:
    """Transient provider failures worth another attempt. Deliberately
    conservative: a bad key or a malformed request must fail immediately."""
    text = str(exc).lower()
    return any(marker in text for marker in RETRYABLE_MARKERS)


async def with_retries(call, *, attempts: int = MAX_RETRIES, base_delay: float = 1.0):
    """Retry a provider call with exponential backoff.

    The Anthropic and OpenAI SDKs do this internally once configured;
    google-genai does not, so Gemini would fail outright on a rate limit.
    """
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return await call()
        except Exception as exc:  # noqa: BLE001 - re-raised below
            last = exc
            if attempt == attempts - 1 or not is_retryable(exc):
                raise
            # Jitter so repeated failures don't resynchronize.
            delay = base_delay * (2**attempt) + random.uniform(0, 0.4)
            log.warning(
                "provider call failed (%s), retrying in %.1fs", str(exc)[:120], delay
            )
            await asyncio.sleep(delay)
    raise last  # pragma: no cover - loop always returns or raises


def max_output_tokens(provider: str, model: str) -> int:
    return MAX_OUTPUT_TOKENS.get(model, DEFAULT_MAX_OUTPUT)


# --- reasoning depth ---------------------------------------------------------
#
# One user-facing knob ("default" | "low" | "medium" | "high" | "max"), mapped
# to whatever each provider actually accepts. "default" sends nothing at all,
# preserving each provider's own behavior. Pure functions so tests can pin the
# exact request shapes without a network.

# Haiku 4.5 rejects output_config.effort — it still uses a manual thinking
# budget, which must stay under max_tokens.
_HAIKU_BUDGETS = {"low": 2048, "medium": 8192, "high": 16384, "max": 32000}
# Gemini thinking budgets; 24576 is the documented flash ceiling.
_GEMINI_BUDGETS = {"low": 2048, "medium": 8192, "high": 16384, "max": 24576}


def anthropic_reasoning_kwargs(model: str, level: str, max_tokens: int) -> dict:
    if level == "default":
        return {}
    if model.startswith("claude-haiku"):
        budget = min(_HAIKU_BUDGETS[level], max_tokens - 1024)
        if budget < 1024:  # thinking needs at least 1024 under the output cap
            return {}
        return {"thinking": {"type": "enabled", "budget_tokens": budget}}
    # Effort-capable models (Opus 4.8 and later, Sonnet 5 and later, Fable):
    # adaptive thinking plus the effort level. Fable, Opus 5.5 and Sonnet 5.5
    # have thinking on by default and accept adaptive.
    return {
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": level},
    }


def openai_reasoning_kwargs(model: str, level: str) -> dict:
    """The Responses API `reasoning` field. GPT-6 models take "max"; the
    GPT-5 ladder tops out at "high"."""
    if level == "default":
        return {}
    if level == "max" and not model.startswith("gpt-6"):
        level = "high"
    return {"reasoning": {"effort": level}}


def google_thinking_config(level: str) -> dict:
    if level == "default":
        return {}
    return {"thinking_config": {"thinking_budget": _GEMINI_BUDGETS[level]}}


def _getter(raw: Any) -> Callable[[str], Any]:
    return (lambda k: getattr(raw, k, None)) if not isinstance(raw, dict) else raw.get


def _usage(raw: Any) -> dict[str, int]:
    """Normalize each SDK's usage object into a flat token count.

    The keys mean the same on every provider: `input` is uncached prompt
    tokens at full price, `cache_read`/`cache_write` the discounted and
    surcharged prompt tokens, `output` everything billed as output.
    """
    if raw is None:
        return {}
    get = _getter(raw)
    out: dict[str, int] = {}
    for key, source in (
        ("input", ("input_tokens", "prompt_tokens", "prompt_token_count")),
        ("output", ("output_tokens", "completion_tokens", "candidates_token_count")),
        ("cache_read", ("cache_read_input_tokens", "cached_content_token_count")),
        ("cache_write", ("cache_creation_input_tokens",)),
    ):
        for name in source:
            value = get(name)
            if isinstance(value, int):
                out[key] = value
                break
    # OpenAI nests its cache hits under prompt_tokens_details (an object, or
    # a dict); the key is absent on models and responses without caching.
    if isinstance(get("prompt_tokens"), int):
        details = get("prompt_tokens_details")
        cached = _getter(details)("cached_tokens") if details is not None else None
        if isinstance(cached, int):
            out["cache_read"] = cached
    # The Responses API reports input_tokens like Anthropic but, like Chat
    # Completions, counts the cached tokens inside it, under
    # input_tokens_details.
    responses_details = get("input_tokens_details")
    if responses_details is not None:
        details = _getter(responses_details)
        for key, name in (("cache_read", "cached_tokens"), ("cache_write", "cache_write_tokens")):
            value = details(name)
            if isinstance(value, int):
                out[key] = value
    # Anthropic's input_tokens is already the uncached remainder after the
    # last breakpoint; OpenAI's prompt_tokens and input_tokens and Gemini's
    # prompt_token_count include the cache traffic, so that comes back out.
    if "input" in out and (
        not isinstance(get("input_tokens"), int) or responses_details is not None
    ):
        out["input"] = max(
            0, out["input"] - out.get("cache_read", 0) - out.get("cache_write", 0)
        )
    # Gemini reports thinking apart from the answer; both bill as output.
    thoughts = get("thoughts_token_count")
    if isinstance(thoughts, int):
        out["output"] = out.get("output", 0) + thoughts
    return out


def _enum_name(value: Any) -> str:
    """A google-genai finish or block reason by name ("MAX_TOKENS"), or "".

    Its enums are str Enums without __str__, so str() gives
    "FinishReason.MAX_TOKENS", which hit_output_limit never matched."""
    if value is None:
        return ""
    name = getattr(value, "name", None)
    return name if isinstance(name, str) else str(value)


def _system_parts(system: str | list[str]) -> list[str]:
    """Normalize a system prompt to its ordered parts.

    The agent hands providers a two-part prompt — the stable prefix (persona +
    app knowledge) and the memory note with the learned habits — so Anthropic
    can give each its own cache breakpoint. Everything else in the codebase,
    and every test, still passes one plain string; both shapes arrive here.
    """
    if isinstance(system, str):
        return [system]
    return [part for part in system if part]


# Block types Anthropic accepts a cache_control on. Notably absent: thinking
# and redacted_thinking, which reject it — a reply that ends in one gets the
# breakpoint on the last block before it, or none at all.
CACHEABLE_BLOCKS = {"text", "image", "document", "tool_use", "tool_result"}


def with_history_breakpoint(messages: list[dict]) -> list[dict]:
    """A copy of `messages` with a cache breakpoint on its final content block.

    One turn can take up to fifteen provider calls (the agent loop's round
    limit), each re-uploading the entire conversation; marking the tail turns
    every round after the first into a cache read of everything before it.

    The copy is not optional. `messages` is the live session history: it is
    persisted to disk, replayed to whichever provider the user switches to
    next, and neither of those understands Anthropic's cache_control. Only the
    last message and its blocks are cloned — the rest is shared, since nothing
    below the tail is touched.
    """
    if not messages:
        return messages
    last = dict(messages[-1])
    content = last.get("content")
    if isinstance(content, str):
        if not content.strip():
            return messages  # nothing to mark, and an empty text block 400s
        blocks: list[Any] = [{"type": "text", "text": content}]
    elif isinstance(content, list):
        blocks = list(content)
    else:
        return messages
    index = next(
        (
            i
            for i in range(len(blocks) - 1, -1, -1)
            if isinstance(blocks[i], dict) and blocks[i].get("type") in CACHEABLE_BLOCKS
        ),
        None,
    )
    if index is None:
        return messages
    blocks[index] = {**blocks[index], "cache_control": {"type": "ephemeral"}}
    last["content"] = blocks
    return [*messages[:-1], last]


# The per-turn context (clock, open note, selection, ...) is prompt text, not
# the user's words. Where a model takes no mid-conversation system message it
# rides as the last text block of the turn's user message instead; the break
# keeps it off the typed text when a converter joins text blocks.
CONTEXT_HEADING = "## Right now"
CONTEXT_BLOCK_PREFIX = "\n\n"


def with_context_block(message: dict, context: str) -> dict:
    """A copy of `message` (a user turn) with `context` as its trailing text
    block. Whoever calls it decides whether the copy is persisted."""
    content = message.get("content")
    if isinstance(content, list):
        blocks = list(content)
    elif isinstance(content, str) and content:
        blocks = [{"type": "text", "text": content}]
    else:
        blocks = []
    blocks.append({"type": "text", "text": CONTEXT_BLOCK_PREFIX + context})
    return {**message, "content": blocks}


def without_context_block(content: list) -> list:
    """A user turn's blocks minus a trailing turn context — for code that reads
    the turn for what the user said, not for what the model was shown."""
    last = content[-1] if content else None
    if (
        isinstance(last, dict)
        and last.get("type") == "text"
        and str(last.get("text") or "").startswith(CONTEXT_BLOCK_PREFIX + CONTEXT_HEADING)
    ):
        return content[:-1]
    return content


def _with_context(messages: list[dict], context: str | None) -> list[dict]:
    """The request's messages with `context` folded into the last one, for a
    model that cannot take it as a system message. The caller's list is left
    alone."""
    if not context or not messages:
        return messages
    return [*messages[:-1], with_context_block(messages[-1], context)]


# Models that take {"role": "system"} entries inside messages[] (no beta). Not
# Sonnet 5 or Haiku, which 400 on the role. Not the preserved-thinking models
# either (Fable 5.1, Opus 5.5, Sonnet 5.5): a per-request system message that
# is gone from the next request is a history edit there, and it invalidates
# every later thinking block — they get the persisted user-message block
# instead.
_SYSTEM_MESSAGE_MODELS = ("claude-opus-4-8", "claude-opus-5", "claude-fable-5")
_PRESERVED_THINKING_MODELS = ("claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5-5")

# Models whose safety classifiers can decline a benign request (HTTP 200,
# stop_reason "refusal"). They ask the API to rerun a declined request on a
# model it picks by the refusal's category, in the same round trip.
_FALLBACK_MODELS = _PRESERVED_THINKING_MODELS
_FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Blocks from a declined attempt that may not be echoed back: the fallback
# model's answer supersedes them, and only its tool calls are real.
_DECLINED_BLOCK_TYPES = {"thinking", "redacted_thinking", "tool_use", "server_tool_use"}


def takes_system_messages(model: str) -> bool:
    return model.startswith(_SYSTEM_MESSAGE_MODELS) and not model.startswith(
        _PRESERVED_THINKING_MODELS
    )


def after_fallback(blocks: list[Any]) -> list[Any]:
    """A reply's content without what a declined attempt left behind.

    A mid-output refusal keeps the partial in `content`, marks the switch with
    a `fallback` block and continues on another model. Text before the last
    marker is kept (the fallback model continued from it); thinking and tool
    calls before it are dropped, and so are the markers themselves, which no
    request accepts back without the beta."""
    last = max(
        (i for i, b in enumerate(blocks) if getattr(b, "type", None) == "fallback"),
        default=-1,
    )
    if last < 0:
        return blocks
    return [
        b
        for i, b in enumerate(blocks)
        if getattr(b, "type", None) != "fallback"
        and (i > last or getattr(b, "type", None) not in _DECLINED_BLOCK_TYPES)
    ]


def image_block(media_type: str, data_b64: str) -> dict[str, Any]:
    """A provider-neutral image block. Stored in history in Anthropic shape and
    translated by each provider's message converter."""
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": data_b64},
    }


def document_block(media_type: str, data_b64: str, title: str = "") -> dict[str, Any]:
    """A PDF. Anthropic and Gemini accept these natively; OpenAI's chat API
    does not, so its converter falls back to extracted text.

    `title` is the file name, under Anthropic's own key for it. Blocks stored
    before 2026-10 carry it as "name", which the API rejects; readers take
    either, and :func:`with_document_titles` renames it on the way out."""
    block: dict[str, Any] = {"type": "document"}
    if title:
        block["title"] = title
    block["source"] = {"type": "base64", "media_type": media_type, "data": data_b64}
    return block


def _titled(block: Any) -> Any:
    """`block`, or a copy with an old "name" key renamed to "title" in place."""
    if not isinstance(block, dict) or block.get("type") != "document" or "name" not in block:
        return block
    out: dict[str, Any] = {}
    for key, value in block.items():
        if key != "name":
            out[key] = value
        elif "title" not in block and isinstance(value, str) and value:
            out["title"] = value
    return out


def with_document_titles(messages: list[dict]) -> list[dict]:
    """`messages` with every stored document block's "name" sent as "title".

    The Messages API has no "name" on a document ("Extra inputs are not
    permitted"), and a thread keeps the rejected block, so every later turn in
    it failed too, even a plain "hello". The stored history stays as it is:
    this runs per request, renames the key where it sits and touches nothing
    else, so the same history gives the same bytes every turn and the same
    blocks for the cache breakpoint to land on. Messages without such a block
    are shared, not copied."""
    out: list[dict] | None = None
    for index, message in enumerate(messages):
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list) or all(_titled(b) is b for b in content):
            continue
        if out is None:
            out = list(messages)
        out[index] = {**message, "content": [_titled(b) for b in content]}
    return messages if out is None else out


# --- fences for untrusted text ----------------------------------------------
# Text the user did not type into this message — the open note, a selection, an
# attached file — reaches the model inside <data-NONCE source="..."> tags. The
# old fence was a literal '"""', which any note containing one could close and
# then carry on as if it were prompt. A random nonce is not in the content, so
# the content cannot close it.


def fence_nonce(*texts: str) -> str:
    """Six random hex characters that occur in none of `texts`."""
    while True:
        nonce = uuid.uuid4().hex[:6]
        if not any(nonce in text for text in texts):
            return nonce


def fence(text: str, nonce: str, source: str, name: str = "") -> str:
    """`text` wrapped as quoted data. A nonce the text happens to contain is
    re-picked for this span alone, so no content can close its own fence."""
    if nonce in text:
        nonce = fence_nonce(text)
    attrs = f' source="{source}"'
    label = " ".join("".join(ch for ch in name if ch not in '"<>').split())
    if label:
        attrs += f' name="{label}"'
    return f"<data-{nonce}{attrs}>\n{text}\n</data-{nonce}>"


# Keyed per process. Every agent round re-converts the whole history for
# OpenAI, so a PDF's fence has to come out the same each time or everything from
# the PDF onward misses the provider's prefix cache; keyed, it is still nothing
# the PDF's author can predict.
_FENCE_KEY = os.urandom(16)


def _stable_nonce(text: str) -> str:
    return hmac.new(_FENCE_KEY, text.encode(), hashlib.sha256).hexdigest()[:6]


class LLMProvider(ABC):
    name: str = "base"
    model: str = ""
    # Whether `context` can ride as a trailing {"role": "system"} message. When
    # it cannot, the agent writes it into the turn's user message itself, so the
    # history stays append-only and the next turn's prefix still matches.
    supports_system_messages: bool = False
    # An output ceiling for this instance in place of the model's entry in
    # MAX_OUTPUT_TOKENS: a recipe drawing at high effort thinks through most
    # of 128K tokens before it draws. None keeps the table's value.
    max_output: int | None = None

    def output_ceiling(self) -> int:
        return self.max_output or max_output_tokens(self.name, self.model)

    @abstractmethod
    async def chat(
        self,
        system: str | list[str],
        messages: list[dict],
        tools: list[ToolDef],
        on_text: TextCallback | None = None,
        allow_tools: bool = True,
        context: str | None = None,
    ) -> ProviderReply:
        """`allow_tools=False` keeps `tools` in the request but forbids calls.

        A history that already holds tool calls needs its definitions to stay
        (Anthropic rejects tool_use blocks with no tools declared), and sending
        the same array keeps the cached tools prefix intact.

        `context` is per-turn state that must sit AFTER the conversation, where
        it cannot move the cached prefix: a trailing system message on this
        request only (never in `messages`), or, on a provider without them, a
        trailing text block on the request's last message.
        """


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(
        self,
        api_key: str,
        model: str,
        reasoning: str = "default",
        max_output: int | None = None,
    ):
        from anthropic import AsyncAnthropic

        # Without an explicit timeout a hung request holds the thread's
        # generation lock forever and every queued message blocks behind it.
        self.client = AsyncAnthropic(
            api_key=api_key, timeout=REQUEST_TIMEOUT, max_retries=MAX_RETRIES
        )
        self.model = model
        self.reasoning = reasoning
        self.max_output = max_output

    @property
    def supports_system_messages(self) -> bool:
        return takes_system_messages(self.model)

    async def chat(
        self,
        system: str | list[str],
        messages: list[dict],
        tools: list[ToolDef],
        on_text: TextCallback | None = None,
        allow_tools: bool = True,
        context: str | None = None,
    ) -> ProviderReply:
        # Caching is a prefix match in the order tools -> system -> messages, so
        # the system breakpoint already covers the tool schemas; a breakpoint of
        # their own bought nothing and spent one of the four allowed.
        tool_defs = [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema}
            for t in tools
        ]
        # One block per system part, each with its own breakpoint: the stable
        # prompt never changes, the memory note and habits only when OMNIA
        # learns something, so a remember() re-writes the second block and not
        # the ~10k-token first. Nothing per-turn may sit up here — the clock
        # alone would re-write the whole conversation behind it every turn.
        # Three at most, which leaves the fourth for the history below.
        system_blocks: list[dict[str, Any]] = [
            {"type": "text", "text": part} for part in _system_parts(system)
        ]
        for block in system_blocks[:3]:
            block["cache_control"] = {"type": "ephemeral"}
        # Before the breakpoint: it renames a key and moves no block.
        messages = with_document_titles(messages)
        if context and self.supports_system_messages:
            # After the history breakpoint and on this request only, so every
            # round of the turn and the next turn read the history from cache.
            # The last message is always a user turn (tool results included),
            # which is where the API allows a system message.
            sent = [
                *with_history_breakpoint(messages),
                {"role": "system", "content": context},
            ]
        else:
            sent = with_history_breakpoint(_with_context(messages, context))
        kwargs = {
            "model": self.model,
            "max_tokens": self.output_ceiling(),
            "system": system_blocks,
            "messages": sent,
            "tools": tool_defs,
            **anthropic_reasoning_kwargs(
                self.model, self.reasoning, self.output_ceiling()
            ),
        }
        if tool_defs and not allow_tools:
            kwargs["tool_choice"] = {"type": "none"}
        messages_api = self.client.messages
        if self.model.startswith(_FALLBACK_MODELS):
            # "default" lets the API choose the substitute per refusal
            # category, so no model list here goes stale. Passed as extra_body:
            # the SDK types `fallbacks` as a list and would iterate the string.
            messages_api = self.client.beta.messages
            kwargs["betas"] = [_FALLBACK_BETA]
            kwargs["extra_body"] = {"fallbacks": "default"}
        if on_text is None:
            resp = await messages_api.create(**kwargs)
        else:
            async with messages_api.stream(**kwargs) as stream:
                async for delta in stream.text_stream:
                    await on_text(delta)
                resp = await stream.get_final_message()
        content = after_fallback(list(resp.content))
        text = "".join(b.text for b in content if b.type == "text")
        calls = [
            ToolCall(id=b.id, name=b.name, input=b.input or {})
            for b in content
            if b.type == "tool_use"
        ]
        if resp.stop_reason == "refusal":
            # The whole chain declined. Nothing it started may run.
            calls = []
            content = [b for b in content if b.type == "text"]
        raw = [b.model_dump(exclude_none=True) for b in content]
        return ProviderReply(
            text=text,
            tool_calls=calls,
            stop_reason=resp.stop_reason,
            raw_content=raw,
            usage=_usage(getattr(resp, "usage", None)),
        )


class OpenAIProvider(LLMProvider):
    """ChatGPT models via the OpenAI Responses API.

    Chat Completions stopped carrying tools for the GPT-6 models (Astra and
    6.1 Sol take none there, Sol and Luna only with reasoning off), so this
    speaks /v1/responses. It translates the neutral (Anthropic-shaped) history
    to Responses input items on every request and returns raw_content in the
    neutral shape, so the agent loop never notices the difference.
    """

    name = "openai"

    def __init__(
        self,
        api_key: str,
        model: str,
        reasoning: str = "default",
        max_output: int | None = None,
    ):
        from openai import AsyncOpenAI

        self.client = AsyncOpenAI(
            api_key=api_key, timeout=REQUEST_TIMEOUT, max_retries=MAX_RETRIES
        )
        self.model = model
        self.reasoning = reasoning
        self.max_output = max_output
        # Effort ladders differ per model (Astra has no "none", GPT-5 has no
        # "max") and change under us, so instead of a model table we learn
        # from the rejection: one retry at the model's own default effort,
        # remembered for this provider instance.
        self._reasoning_rejected = False

    async def _create(self, kwargs: dict, **extra):
        from openai import BadRequestError

        try:
            return await self.client.responses.create(**kwargs, **extra)
        except BadRequestError as e:
            param = str(getattr(e, "param", None) or "")
            rejected_reasoning = param.startswith("reasoning") or "reasoning" in str(e)
            if not rejected_reasoning or "reasoning" not in kwargs:
                raise
            self._reasoning_rejected = True
            log.warning(
                "%s rejected the reasoning effort (%s) — retrying at the model's "
                "default; the reasoning setting will be ignored for this model",
                self.model,
                e,
            )
            retry = {k: v for k, v in kwargs.items() if k != "reasoning"}
            return await self.client.responses.create(**retry, **extra)

    async def chat(
        self,
        system: str | list[str],
        messages: list[dict],
        tools: list[ToolDef],
        on_text: TextCallback | None = None,
        allow_tools: bool = True,
        context: str | None = None,
    ) -> ProviderReply:
        # No per-block cache controls here — the split prompt collapses back
        # into the single instructions string this API takes; OpenAI caches
        # the prefix on its own.
        instructions = "\n\n".join(_system_parts(system))
        messages = _with_context(messages, context)
        # pypdf on a slide deck runs for seconds, and every round re-converts
        # the whole history. Extract off the event loop first, so the
        # converter below only looks the text up.
        pdf_texts = await _pdf_texts(messages)
        kwargs: dict[str, Any] = {
            "model": self.model,
            "instructions": instructions,
            "input": to_openai_input(messages, pdf_texts),
            "max_output_tokens": self.output_ceiling(),
            # The history travels with every request; nothing needs to live
            # on OpenAI's side, and notes should not.
            "store": False,
            **(
                {}
                if self._reasoning_rejected
                else openai_reasoning_kwargs(self.model, self.reasoning)
            ),
        }
        # The tool-less callers (summaries, housekeeping, autocorrect) pass [].
        # Leave the key out then rather than send an empty array.
        if tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.input_schema,
                    # Responses defaults to strict, which demands every
                    # property be required and additionalProperties false.
                    # The tool schemas are written for Anthropic's lenient
                    # validation.
                    "strict": False,
                }
                for t in tools
            ]
            if not allow_tools:
                kwargs["tool_choice"] = "none"
        if on_text is None:
            return _openai_reply(await self._create(kwargs))
        stream = await self._create(kwargs, stream=True)
        final = None
        async for event in stream:
            kind = getattr(event, "type", "")
            if kind == "response.output_text.delta":
                if event.delta:
                    await on_text(event.delta)
            elif kind in ("response.completed", "response.incomplete"):
                final = event.response
            elif kind == "response.failed":
                error = getattr(event.response, "error", None)
                raise RuntimeError(
                    f"OpenAI response failed: {getattr(error, 'message', None) or error}"
                )
            elif kind == "error":
                raise RuntimeError(f"OpenAI stream error: {getattr(event, 'message', event)}")
        if final is None:
            raise RuntimeError("OpenAI stream ended without a final response")
        return _openai_reply(final)


def _openai_reply(resp: Any) -> ProviderReply:
    """A Responses API response → the neutral ProviderReply."""
    text_parts: list[str] = []
    refused = False
    calls: list[ToolCall] = []
    for item in getattr(resp, "output", None) or []:
        kind = getattr(item, "type", None)
        if kind == "message":
            for part in getattr(item, "content", None) or []:
                if getattr(part, "type", None) == "output_text":
                    text_parts.append(part.text or "")
                elif getattr(part, "type", None) == "refusal":
                    refused = True
        elif kind == "function_call":
            try:
                args = json.loads(item.arguments or "{}")
            except (json.JSONDecodeError, TypeError):
                args = {}
            if not isinstance(args, dict):
                args = {}
            call_id = item.call_id or f"ocall_{len(calls)}"
            calls.append(ToolCall(id=call_id, name=item.name, input=args))
    text = "".join(text_parts)
    incomplete = getattr(getattr(resp, "incomplete_details", None), "reason", None)
    if refused or incomplete == "content_filter":
        stop_reason = "content_filter"
        calls = []
    elif incomplete == "max_output_tokens":
        stop_reason = "max_tokens"
    else:
        stop_reason = "tool_use" if calls else "end_turn"
    raw: list[dict[str, Any]] = []
    if text:
        raw.append({"type": "text", "text": text})
    raw.extend(
        {"type": "tool_use", "id": c.id, "name": c.name, "input": c.input} for c in calls
    )
    return ProviderReply(
        text=text,
        tool_calls=calls,
        stop_reason=stop_reason,
        raw_content=raw,
        usage=_usage(getattr(resp, "usage", None)),
    )


async def _pdf_texts(messages: list[dict]) -> dict[str, str]:
    """Extracted text for every PDF block in `messages`, keyed by its base64
    data, each extracted in a worker thread."""
    from . import vision

    def extract(data: str) -> str:
        return vision.extract_pdf_text(base64.b64decode(data))

    texts: dict[str, str] = {}
    for message in messages:
        content = message.get("content")
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict) or block.get("type") != "document":
                continue
            data = (block.get("source") or {}).get("data") or ""
            if data and data not in texts:
                texts[data] = await asyncio.to_thread(extract, data)
    return texts


def to_openai_input(
    messages: list[dict], pdf_texts: dict[str, str] | None = None
) -> list[dict]:
    """Neutral (Anthropic-shaped) history → OpenAI Responses input items.

    Tool calls and their results are items of their own (`function_call`,
    `function_call_output`) beside the messages. `pdf_texts` is what
    :func:`_pdf_texts` extracted ahead of time; a PDF it does not cover is
    extracted here, synchronously."""
    out: list[dict] = []
    for m in messages:
        role, content = m["role"], m["content"]
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        if role == "assistant":
            text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
            if text:
                out.append({"role": "assistant", "content": text})
            for b in content:
                if b.get("type") == "tool_use":
                    out.append(
                        {
                            "type": "function_call",
                            "call_id": b["id"],
                            "name": b["name"],
                            "arguments": json.dumps(b.get("input") or {}),
                        }
                    )
            continue
        # User turn: tool_result blocks become function_call_output items,
        # the rest one user message.
        for b in content:
            if b.get("type") == "tool_result":
                output = b.get("content") or ""
                out.append(
                    {
                        "type": "function_call_output",
                        "call_id": b["tool_use_id"],
                        "output": output if isinstance(output, str) else json.dumps(output),
                    }
                )
        # Text and images become one multimodal user message.
        parts: list[dict] = []
        text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
        # The PDF is degraded to its extracted text — layout, figures and
        # handwriting are lost, and the note says so rather than letting the
        # model imply it saw the file.
        for b in content:
            if b.get("type") == "document":
                from .vision import extract_pdf_text

                data = (b.get("source") or {}).get("data") or ""
                if pdf_texts is not None and data in pdf_texts:
                    extracted = pdf_texts[data]
                else:
                    extracted = extract_pdf_text(base64.b64decode(data))
                label = b.get("title") or b.get("name") or "document.pdf"
                text += (
                    f"\n\n[Text extracted from the attached PDF {label}. This "
                    "provider cannot display the document itself, so figures, "
                    "handwriting and layout are not available.]\n"
                    + (
                        fence(extracted, _stable_nonce(extracted), "attachment", label)
                        if extracted
                        else "(no extractable text — it may be scanned)"
                    )
                )
        if text:
            parts.append({"type": "input_text", "text": text})
        for b in content:
            if b.get("type") == "image":
                source = b.get("source") or {}
                parts.append(
                    {
                        "type": "input_image",
                        "image_url": f"data:{source.get('media_type')};base64,{source.get('data')}",
                    }
                )
        if len(parts) == 1 and parts[0]["type"] == "input_text":
            out.append({"role": "user", "content": text})
        elif parts:
            out.append({"role": "user", "content": parts})
    return out


class GeminiProvider(LLMProvider):
    """Google Gemini models via the google-genai SDK.

    Same pattern as OpenAIProvider: translate the neutral (Anthropic-shaped)
    history to Gemini's content format per request; return raw_content in the
    neutral shape. Gemini function calls carry no stable ids, so we assign
    sequential ids and match function responses back by tool name."""

    name = "google"

    def __init__(
        self,
        api_key: str,
        model: str,
        reasoning: str = "default",
        max_output: int | None = None,
    ):
        from google import genai

        self.client = genai.Client(api_key=api_key)
        self.model = model
        self.reasoning = reasoning
        self.max_output = max_output

    async def chat(
        self,
        system: str | list[str],
        messages: list[dict],
        tools: list[ToolDef],
        on_text: TextCallback | None = None,
        allow_tools: bool = True,
        context: str | None = None,
    ) -> ProviderReply:
        messages = _with_context(messages, context)
        config: dict[str, Any] = {
            # Gemini's implicit prefix caching works off the raw prefix, so
            # the split prompt just rejoins in stable-then-memory order.
            "system_instruction": "\n\n".join(_system_parts(system)),
            "max_output_tokens": self.output_ceiling(),
            **google_thinking_config(self.reasoning),
        }
        # Same as OpenAI: no empty declaration list for the tool-less callers.
        if tools:
            config["tools"] = [
                {
                    "function_declarations": [
                        {
                            "name": t.name,
                            "description": t.description,
                            "parameters": t.input_schema,
                        }
                        for t in tools
                    ]
                }
            ]
            if not allow_tools:
                config["tool_config"] = {"function_calling_config": {"mode": "NONE"}}
        kwargs = {
            "model": self.model,
            "contents": to_gemini_contents(messages),
            "config": config,
        }
        if on_text is not None:
            text = ""
            calls: list[ToolCall] = []
            raw: list[dict[str, Any]] = []
            call_index = 0
            # Unique per request. A per-request counter alone restarted at 0 on
            # every round, so round two's first call reused round one's id and
            # history repair could no longer tell a satisfied call from an
            # interrupted one — the bricked thread it exists to prevent.
            call_prefix = f"gcall_{uuid.uuid4().hex[:8]}"
            usage: dict[str, int] = {}
            finish = ""
            stream = await with_retries(
                lambda: self.client.aio.models.generate_content_stream(**kwargs)
            )
            async for chunk in stream:
                # Usage arrives on the chunks; without this the streaming path
                # (the one the UI uses) reported no tokens at all.
                if getattr(chunk, "usage_metadata", None):
                    usage = _usage(chunk.usage_metadata)
                candidates = chunk.candidates or []
                # The reason arrives on the final chunk, which may carry no
                # content; keep the last one seen.
                if candidates:
                    reason = getattr(candidates[0], "finish_reason", None)
                    finish = _enum_name(reason) or finish
                content = candidates[0].content if candidates else None
                parts = (content.parts if content else None) or []
                for part in parts:
                    sig = _encode_signature(getattr(part, "thought_signature", None))
                    if getattr(part, "text", None):
                        text += part.text
                        _append_text(raw, part.text, sig)
                        await on_text(part.text)
                    fc = getattr(part, "function_call", None)
                    if fc:
                        call_id = f"{call_prefix}_{call_index}"
                        call_index += 1
                        args = dict(fc.args or {})
                        calls.append(ToolCall(id=call_id, name=fc.name, input=args))
                        block = {
                            "type": "tool_use",
                            "id": call_id,
                            "name": fc.name,
                            "input": args,
                        }
                        if sig:
                            block["thought_signature"] = sig
                        raw.append(block)
            return ProviderReply(
                text=text,
                tool_calls=calls,
                # Gemini sends each function call as a whole part, never
                # half-built, so a reply carrying calls is a tool round even
                # when the reason is MAX_TOKENS.
                stop_reason="tool_use" if calls else finish or "end_turn",
                raw_content=raw,
                usage=usage,
            )

        resp = await with_retries(
            lambda: self.client.aio.models.generate_content(**kwargs)
        )
        if not resp.candidates:
            # A prompt-level block returns no candidates at all. Name the
            # reason: callers here are unattended and only log the error.
            feedback = getattr(resp, "prompt_feedback", None)
            blocked = _enum_name(getattr(feedback, "block_reason", None))
            raise RuntimeError(
                f"Gemini returned no candidates (blocked: {blocked})"
                if blocked
                else "Gemini returned no candidates"
            )
        candidate = resp.candidates[0]
        text = ""
        calls: list[ToolCall] = []
        raw: list[dict[str, Any]] = []
        parts = (candidate.content.parts if candidate.content else None) or []
        call_prefix = f"gcall_{uuid.uuid4().hex[:8]}"
        for i, part in enumerate(parts):
            sig = _encode_signature(getattr(part, "thought_signature", None))
            if getattr(part, "text", None):
                text += part.text
                _append_text(raw, part.text, sig)
            fc = getattr(part, "function_call", None)
            if fc:
                call_id = f"{call_prefix}_{i}"
                args = dict(fc.args or {})
                calls.append(ToolCall(id=call_id, name=fc.name, input=args))
                block = {"type": "tool_use", "id": call_id, "name": fc.name, "input": args}
                if sig:
                    block["thought_signature"] = sig
                raw.append(block)
        return ProviderReply(
            text=text,
            tool_calls=calls,
            stop_reason="tool_use"
            if calls
            else _enum_name(getattr(candidate, "finish_reason", None)) or "end_turn",
            raw_content=raw,
            usage=_usage(getattr(resp, "usage_metadata", None)),
        )


def to_gemini_contents(messages: list[dict]) -> list[dict]:
    """Neutral (Anthropic-shaped) history → Gemini contents.

    Roles map user→user, assistant→model. tool_result blocks become
    function_response parts, matched to the call by name via the tool_use ids
    seen earlier in the history."""
    id_to_name: dict[str, str] = {}
    out: list[dict] = []
    for m in messages:
        role = "model" if m["role"] == "assistant" else "user"
        content = m["content"]
        if isinstance(content, str):
            out.append({"role": role, "parts": [{"text": content}]})
            continue
        parts: list[dict] = []
        for b in content:
            btype = b.get("type")
            sig = _decode_signature(b.get("thought_signature"))
            if btype == "text" and b.get("text"):
                part: dict[str, Any] = {"text": b["text"]}
                if sig:
                    part["thought_signature"] = sig
                parts.append(part)
            elif btype in ("image", "document"):
                # Gemini takes images and PDFs through the same inline_data part.
                source = b.get("source") or {}
                parts.append(
                    {
                        "inline_data": {
                            "mime_type": source.get("media_type"),
                            "data": source.get("data"),
                        }
                    }
                )
            elif btype == "tool_use":
                id_to_name[b["id"]] = b["name"]
                # Gemini rejects a replayed function_call whose thought_signature
                # is missing (400 INVALID_ARGUMENT), so echo it back verbatim.
                part = {"function_call": {"name": b["name"], "args": b.get("input") or {}}}
                if sig:
                    part["thought_signature"] = sig
                parts.append(part)
            elif btype == "tool_result":
                name = id_to_name.get(b.get("tool_use_id", ""), "tool")
                parts.append(
                    {
                        "function_response": {
                            "name": name,
                            "response": {"result": b.get("content") or ""},
                        }
                    }
                )
        if parts:
            out.append({"role": role, "parts": parts})
    return out


class FakeProvider(LLMProvider):
    """Scripted provider for offline tests: pops replies from a queue."""

    name = "fake"
    model = "fake-1"

    def __init__(self, script: list[ProviderReply], system_messages: bool = True):
        self.script = list(script)
        # Like the default model (Opus 4.8) unless a test asks otherwise; off,
        # the agent writes the turn's context into the user message instead.
        self.supports_system_messages = system_messages
        self.seen: list[list[dict]] = []
        # The joined prompt plus the turn's context, so assertions read as "is
        # this phrase in there?" regardless of where it landed; system_parts
        # and contexts keep the split for the tests that care.
        self.systems: list[str] = []
        self.system_parts: list[list[str]] = []
        self.contexts: list[str | None] = []
        # Per call: whether tools were allowed, and how many were declared.
        self.allow_tools_seen: list[bool] = []
        self.tool_counts: list[int] = []

    async def chat(
        self,
        system: str | list[str],
        messages: list[dict],
        tools: list[ToolDef],
        on_text: TextCallback | None = None,
        allow_tools: bool = True,
        context: str | None = None,
    ) -> ProviderReply:
        self.seen.append(messages)
        self.allow_tools_seen.append(allow_tools)
        self.tool_counts.append(len(tools))
        parts = _system_parts(system)
        self.system_parts.append(parts)
        self.contexts.append(context)
        self.systems.append("\n\n".join([*parts, context] if context else parts))
        if not self.script:
            reply = ProviderReply(text="(script exhausted)", stop_reason="end_turn")
        else:
            reply = self.script.pop(0)
        if on_text is not None and reply.text:
            midpoint = max(1, len(reply.text) // 2)
            await on_text(reply.text[:midpoint])
            if midpoint < len(reply.text):
                await on_text(reply.text[midpoint:])
        return reply


def build_provider(
    settings,
    provider_id: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    reasoning: str = "default",
    max_output: int | None = None,
) -> LLMProvider | None:
    """Returns None when no API key is configured — the app runs AI-free.
    `max_output` overrides the model's output ceiling for this instance."""
    pid = provider_id or settings.ai_provider
    if pid == "anthropic":
        key = api_key or settings.anthropic_api_key
        if key:
            return AnthropicProvider(
                key, model or settings.anthropic_model, reasoning, max_output
            )
    elif pid == "openai":
        key = api_key or settings.openai_api_key
        if key:
            return OpenAIProvider(key, model or settings.openai_model, reasoning, max_output)
    elif pid == "google":
        key = api_key or settings.google_api_key
        if key:
            return GeminiProvider(key, model or settings.google_model, reasoning, max_output)
    return None
