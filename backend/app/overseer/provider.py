"""Thin LLM-provider abstraction. One interface, one env var per provider.

Message dicts use the Anthropic wire shape (role + content blocks) as the
neutral format; additional providers adapt to it in their own class. That
keeps the agent loop and stored chat history provider-agnostic — you can
switch providers mid-conversation.
"""
import asyncio
import base64
import json
import logging
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


# Output ceilings. 4096 was hardcoded for every model and is the tightest
# artificial limit in the system — it caps how long a note OMNIA can rewrite.
DEFAULT_MAX_OUTPUT = 8192
MAX_OUTPUT_TOKENS: dict[str, int] = {
    "claude-opus-4-8": 32000,
    "claude-sonnet-5": 32000,
    "claude-haiku-4-5": 16000,
    "claude-fable-5": 16000,
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
    # Effort-capable models (Opus 4.8, Sonnet 5, Fable 5): adaptive thinking
    # plus the effort level. Fable has thinking always on and accepts adaptive.
    return {
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": level},
    }


def openai_reasoning_kwargs(level: str) -> dict:
    if level == "default":
        return {}
    # OpenAI's ladder tops out at "high".
    return {"reasoning_effort": "high" if level == "max" else level}


def google_thinking_config(level: str) -> dict:
    if level == "default":
        return {}
    return {"thinking_config": {"thinking_budget": _GEMINI_BUDGETS[level]}}


def _usage(raw: Any) -> dict[str, int]:
    """Normalize each SDK's usage object into a flat token count."""
    if raw is None:
        return {}
    get = (lambda k: getattr(raw, k, None)) if not isinstance(raw, dict) else raw.get
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
    return out


def image_block(media_type: str, data_b64: str) -> dict[str, Any]:
    """A provider-neutral image block. Stored in history in Anthropic shape and
    translated by each provider's message converter."""
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": data_b64},
    }


def document_block(media_type: str, data_b64: str, name: str = "document.pdf") -> dict[str, Any]:
    """A PDF. Anthropic and Gemini accept these natively; OpenAI's chat API
    does not, so its converter falls back to extracted text."""
    return {
        "type": "document",
        "name": name,
        "source": {"type": "base64", "media_type": media_type, "data": data_b64},
    }


class LLMProvider(ABC):
    name: str = "base"
    model: str = ""

    @abstractmethod
    async def chat(
        self,
        system: str,
        messages: list[dict],
        tools: list[ToolDef],
        on_text: TextCallback | None = None,
    ) -> ProviderReply: ...


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, api_key: str, model: str, reasoning: str = "default"):
        from anthropic import AsyncAnthropic

        # Without an explicit timeout a hung request holds the thread's
        # generation lock forever and every queued message blocks behind it.
        self.client = AsyncAnthropic(
            api_key=api_key, timeout=REQUEST_TIMEOUT, max_retries=MAX_RETRIES
        )
        self.model = model
        self.reasoning = reasoning

    async def chat(
        self,
        system: str,
        messages: list[dict],
        tools: list[ToolDef],
        on_text: TextCallback | None = None,
    ) -> ProviderReply:
        tool_defs = [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema}
            for t in tools
        ]
        # The system prompt and tool schemas are byte-identical on every round
        # of every turn. Marking the tail of each for caching turns ~10k tokens
        # of re-upload into a cache read.
        if tool_defs:
            tool_defs[-1] = {**tool_defs[-1], "cache_control": {"type": "ephemeral"}}
        kwargs = {
            "model": self.model,
            "max_tokens": max_output_tokens(self.name, self.model),
            "system": [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": messages,
            "tools": tool_defs,
            **anthropic_reasoning_kwargs(
                self.model, self.reasoning, max_output_tokens(self.name, self.model)
            ),
        }
        if on_text is None:
            resp = await self.client.messages.create(**kwargs)
        else:
            async with self.client.messages.stream(**kwargs) as stream:
                async for delta in stream.text_stream:
                    await on_text(delta)
                resp = await stream.get_final_message()
        text = "".join(b.text for b in resp.content if b.type == "text")
        calls = [
            ToolCall(id=b.id, name=b.name, input=b.input or {})
            for b in resp.content
            if b.type == "tool_use"
        ]
        raw = [b.model_dump(exclude_none=True) for b in resp.content]
        return ProviderReply(
            text=text,
            tool_calls=calls,
            stop_reason=resp.stop_reason,
            raw_content=raw,
            usage=_usage(getattr(resp, "usage", None)),
        )


class OpenAIProvider(LLMProvider):
    """ChatGPT models via the OpenAI Chat Completions API.

    Translates the neutral (Anthropic-shaped) history to OpenAI's format on
    every request, and returns raw_content in the neutral shape so the agent
    loop never notices the difference.
    """

    name = "openai"

    def __init__(self, api_key: str, model: str, reasoning: str = "default"):
        from openai import AsyncOpenAI

        self.client = AsyncOpenAI(
            api_key=api_key, timeout=REQUEST_TIMEOUT, max_retries=MAX_RETRIES
        )
        self.model = model
        self.reasoning = reasoning
        # Some models refuse any reasoning effort when function tools are
        # present (gpt-5.6-luna: 400, "use /v1/responses or set
        # reasoning_effort to 'none'"). Omitting the param is NOT enough —
        # the model's implicit default effort still trips the same 400 — it
        # must be explicitly "none". Capabilities vary per model and change
        # under us, so instead of a model list we learn from the rejection:
        # one retry with "none", remembered for this provider instance.
        self._reasoning_rejected = False

    async def _create(self, kwargs: dict, **extra):
        from openai import BadRequestError

        try:
            return await self.client.chat.completions.create(**kwargs, **extra)
        except BadRequestError as e:
            rejected_reasoning = (
                getattr(e, "param", None) == "reasoning_effort"
                or "reasoning_effort" in str(e)
            )
            if not rejected_reasoning or kwargs.get("reasoning_effort") == "none":
                raise
            self._reasoning_rejected = True
            log.warning(
                "%s rejected reasoning_effort (%s) — retrying with 'none'; the "
                "reasoning setting will be ignored for this model",
                self.model,
                e,
            )
            retry = {**kwargs, "reasoning_effort": "none"}
            return await self.client.chat.completions.create(**retry, **extra)

    async def chat(
        self,
        system: str,
        messages: list[dict],
        tools: list[ToolDef],
        on_text: TextCallback | None = None,
    ) -> ProviderReply:
        kwargs = {
            "model": self.model,
            "max_completion_tokens": max_output_tokens(self.name, self.model),
            **(
                {"reasoning_effort": "none"}
                if self._reasoning_rejected
                else openai_reasoning_kwargs(self.reasoning)
            ),
            "messages": [{"role": "system", "content": system}, *to_openai_messages(messages)],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.input_schema,
                    },
                }
                for t in tools
            ],
        }
        if on_text is not None:
            stream = await self._create(
                kwargs, stream=True, stream_options={"include_usage": True}
            )
            text_parts: list[str] = []
            tool_parts: dict[int, dict[str, str]] = {}
            stop_reason = None
            usage: dict[str, int] = {}
            async for chunk in stream:
                # The usage-only final chunk carries no choices.
                if getattr(chunk, "usage", None):
                    usage = _usage(chunk.usage)
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                stop_reason = choice.finish_reason or stop_reason
                delta = choice.delta
                if delta.content:
                    text_parts.append(delta.content)
                    await on_text(delta.content)
                for call in delta.tool_calls or []:
                    part = tool_parts.setdefault(
                        call.index,
                        {"id": "", "name": "", "arguments": ""},
                    )
                    if call.id:
                        part["id"] = call.id
                    if call.function:
                        if call.function.name:
                            part["name"] += call.function.name
                        if call.function.arguments:
                            part["arguments"] += call.function.arguments
            text = "".join(text_parts)
            calls: list[ToolCall] = []
            raw: list[dict[str, Any]] = []
            if text:
                raw.append({"type": "text", "text": text})
            for index, part in sorted(tool_parts.items()):
                try:
                    args = json.loads(part["arguments"] or "{}")
                except (json.JSONDecodeError, TypeError):
                    args = {}
                call_id = part["id"] or f"ocall_{index}"
                calls.append(ToolCall(id=call_id, name=part["name"], input=args))
                raw.append(
                    {
                        "type": "tool_use",
                        "id": call_id,
                        "name": part["name"],
                        "input": args,
                    }
                )
            return ProviderReply(
                text=text,
                tool_calls=calls,
                stop_reason=stop_reason or ("tool_use" if calls else "end_turn"),
                raw_content=raw,
                usage=usage,
            )

        resp = await self._create(kwargs)
        msg_finish = resp.choices[0].finish_reason
        msg = resp.choices[0].message
        text = msg.content or ""
        calls: list[ToolCall] = []
        raw: list[dict[str, Any]] = []
        if text:
            raw.append({"type": "text", "text": text})
        for tc in msg.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except (json.JSONDecodeError, TypeError):
                args = {}
            calls.append(ToolCall(id=tc.id, name=tc.function.name, input=args))
            raw.append(
                {"type": "tool_use", "id": tc.id, "name": tc.function.name, "input": args}
            )
        return ProviderReply(
            text=text,
            tool_calls=calls,
            stop_reason=msg_finish or ("tool_use" if calls else "end_turn"),
            raw_content=raw,
            usage=_usage(getattr(resp, "usage", None)),
        )


def to_openai_messages(messages: list[dict]) -> list[dict]:
    """Neutral (Anthropic-shaped) history → OpenAI chat.completions messages."""
    out: list[dict] = []
    for m in messages:
        role, content = m["role"], m["content"]
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        if role == "assistant":
            text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
            tool_calls = [
                {
                    "id": b["id"],
                    "type": "function",
                    "function": {
                        "name": b["name"],
                        "arguments": json.dumps(b.get("input") or {}),
                    },
                }
                for b in content
                if b.get("type") == "tool_use"
            ]
            entry: dict = {"role": "assistant", "content": text or None}
            if tool_calls:
                entry["tool_calls"] = tool_calls
            out.append(entry)
        else:
            # User turn: tool_result blocks become role:"tool" messages,
            # text blocks a regular user message.
            for b in content:
                if b.get("type") == "tool_result":
                    out.append(
                        {
                            "role": "tool",
                            "tool_call_id": b["tool_use_id"],
                            "content": b.get("content") or "",
                        }
                    )
            # Text and images become one multimodal user message.
            parts: list[dict] = []
            text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
            # Chat Completions has no document part, so a PDF is degraded to its
            # extracted text — layout, figures and handwriting are lost, and the
            # note says so rather than letting the model imply it saw the file.
            for b in content:
                if b.get("type") == "document":
                    from .vision import extract_pdf_text

                    raw = base64.b64decode((b.get("source") or {}).get("data") or "")
                    extracted = extract_pdf_text(raw)
                    label = b.get("name") or "document.pdf"
                    text += (
                        f"\n\n[Text extracted from the attached PDF {label}. This "
                        "provider cannot display the document itself, so figures, "
                        "handwriting and layout are not available.]\n"
                        + (extracted or "(no extractable text — it may be scanned)")
                    )
            if text:
                parts.append({"type": "text", "text": text})
            for b in content:
                if b.get("type") == "image":
                    source = b.get("source") or {}
                    parts.append(
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{source.get('media_type')};base64,{source.get('data')}"
                            },
                        }
                    )
            if len(parts) == 1 and parts[0]["type"] == "text":
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

    def __init__(self, api_key: str, model: str, reasoning: str = "default"):
        from google import genai

        self.client = genai.Client(api_key=api_key)
        self.model = model
        self.reasoning = reasoning

    async def chat(
        self,
        system: str,
        messages: list[dict],
        tools: list[ToolDef],
        on_text: TextCallback | None = None,
    ) -> ProviderReply:
        kwargs = {
            "model": self.model,
            "contents": to_gemini_contents(messages),
            "config": {
                "system_instruction": system,
                "max_output_tokens": max_output_tokens(self.name, self.model),
                **google_thinking_config(self.reasoning),
                "tools": [
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
                ],
            },
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
            stream = await with_retries(
                lambda: self.client.aio.models.generate_content_stream(**kwargs)
            )
            async for chunk in stream:
                # Usage arrives on the chunks; without this the streaming path
                # (the one the UI uses) reported no tokens at all.
                if getattr(chunk, "usage_metadata", None):
                    usage = _usage(chunk.usage_metadata)
                candidates = chunk.candidates or []
                parts = candidates[0].content.parts or [] if candidates else []
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
                stop_reason="tool_use" if calls else "end_turn",
                raw_content=raw,
                usage=usage,
            )

        resp = await with_retries(
            lambda: self.client.aio.models.generate_content(**kwargs)
        )
        text = ""
        calls: list[ToolCall] = []
        raw: list[dict[str, Any]] = []
        parts = resp.candidates[0].content.parts or [] if resp.candidates else []
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
            stop_reason=str(getattr(resp.candidates[0], "finish_reason", "") or "")
            or ("tool_use" if calls else "end_turn"),
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

    def __init__(self, script: list[ProviderReply]):
        self.script = list(script)
        self.seen: list[list[dict]] = []
        self.systems: list[str] = []

    async def chat(
        self,
        system: str,
        messages: list[dict],
        tools: list[ToolDef],
        on_text: TextCallback | None = None,
    ) -> ProviderReply:
        self.seen.append(messages)
        self.systems.append(system)
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
) -> LLMProvider | None:
    """Returns None when no API key is configured — the app runs AI-free."""
    pid = provider_id or settings.ai_provider
    if pid == "anthropic":
        key = api_key or settings.anthropic_api_key
        if key:
            return AnthropicProvider(key, model or settings.anthropic_model, reasoning)
    elif pid == "openai":
        key = api_key or settings.openai_api_key
        if key:
            return OpenAIProvider(key, model or settings.openai_model, reasoning)
    elif pid == "google":
        key = api_key or settings.google_api_key
        if key:
            return GeminiProvider(key, model or settings.google_model, reasoning)
    return None
