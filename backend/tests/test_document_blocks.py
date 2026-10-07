"""Every content block the app sends to Anthropic carries only keys the
Messages API accepts.

PDF attachments went out as document blocks with a "name" key. The API's
document block has no such key (it names itself with "title") and rejected the
request: "messages.N.content.M.document.name: Extra inputs are not permitted".
The rejected block stayed in the thread's history, so every later turn there
failed the same way, even a plain "hello". These pin the request shapes to the
installed SDK's own request types, so a key the API does not know fails here.
"""
import asyncio
import base64
import copy
import io
import json
from types import SimpleNamespace

import pytest
from anthropic.types import (
    Base64ImageSourceParam,
    Base64PDFSourceParam,
    CacheControlEphemeralParam,
    DocumentBlockParam,
    ImageBlockParam,
    RedactedThinkingBlockParam,
    TextBlock,
    TextBlockParam,
    ThinkingBlockParam,
    ToolResultBlockParam,
    ToolUseBlock,
    ToolUseBlockParam,
)
from anthropic.types.beta import (
    BetaBase64ImageSourceParam,
    BetaBase64PDFSourceParam,
    BetaCacheControlEphemeralParam,
    BetaImageBlockParam,
    BetaRedactedThinkingBlockParam,
    BetaRequestDocumentBlockParam,
    BetaTextBlockParam,
    BetaThinkingBlockParam,
    BetaToolResultBlockParam,
    BetaToolUseBlockParam,
)

from app.overseer.agent import Overseer
from app.overseer.history import shed_media
from app.overseer.provider import (
    AnthropicProvider,
    document_block,
    image_block,
    to_gemini_contents,
    to_openai_input,
    with_context_block,
    with_document_titles,
    with_history_breakpoint,
)

# Block type -> the SDK's request type for it, on each of the two APIs the
# provider calls: plain messages, and beta messages for the fallback models.
PLAIN = {
    "text": TextBlockParam,
    "image": ImageBlockParam,
    "document": DocumentBlockParam,
    "tool_use": ToolUseBlockParam,
    "tool_result": ToolResultBlockParam,
    "thinking": ThinkingBlockParam,
    "redacted_thinking": RedactedThinkingBlockParam,
    "image.source": Base64ImageSourceParam,
    "document.source": Base64PDFSourceParam,
    "cache_control": CacheControlEphemeralParam,
}
BETA = {
    "text": BetaTextBlockParam,
    "image": BetaImageBlockParam,
    "document": BetaRequestDocumentBlockParam,
    "tool_use": BetaToolUseBlockParam,
    "tool_result": BetaToolResultBlockParam,
    "thinking": BetaThinkingBlockParam,
    "redacted_thinking": BetaRedactedThinkingBlockParam,
    "image.source": BetaBase64ImageSourceParam,
    "document.source": BetaBase64PDFSourceParam,
    "cache_control": BetaCacheControlEphemeralParam,
}
APIS = pytest.mark.parametrize("types", [PLAIN, BETA], ids=["messages", "beta"])
FALLBACK_MODEL = "claude-opus-5-5"  # sent through beta.messages


def _keys(typed) -> set[str]:
    return set(typed.__required_keys__) | set(typed.__optional_keys__)


def _problems(block: dict, types: dict, where: str) -> list[str]:
    """What in `block` the SDK's request type does not allow, worded like the
    API's own rejection ("document.name: extra")."""
    kind = block.get("type")
    typed = types.get(kind)
    if typed is None:
        return [f"{where}: no request type for {kind!r}"]
    out = [f"{where}.{kind}.{k}: extra" for k in sorted(set(block) - _keys(typed))]
    out += [f"{where}.{kind}.{k}: missing" for k in sorted(typed.__required_keys__ - set(block))]
    source = block.get("source")
    if isinstance(source, dict):
        allowed = _keys(types[f"{kind}.source"])
        out += [f"{where}.{kind}.source.{k}: extra" for k in sorted(set(source) - allowed)]
    marker = block.get("cache_control")
    if isinstance(marker, dict):
        allowed = _keys(types["cache_control"])
        out += [f"{where}.{kind}.cache_control.{k}: extra" for k in sorted(set(marker) - allowed)]
    nested = block.get("content") if kind == "tool_result" else None
    for j, inner in enumerate(nested if isinstance(nested, list) else []):
        out += _problems(inner, types, f"{where}.content.{j}")
    return out


def _request_problems(kwargs: dict, types: dict) -> list[str]:
    out: list[str] = []
    for i, message in enumerate(kwargs["messages"]):
        content = message["content"]
        for j, block in enumerate(content if isinstance(content, list) else []):
            out += _problems(block, types, f"messages.{i}.content.{j}")
    for j, block in enumerate(kwargs["system"]):
        out += _problems(block, types, f"system.{j}")
    return out


def _pdf(pages: int = 2) -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _png() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (10, 120, 200)).save(buf, format="PNG")
    return buf.getvalue()


def _put(state, name: str, data: bytes) -> str:
    rel = f"attachments/{name}"
    path = state.store.resolve_attachment(rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return rel


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _anthropic(model: str, replies: list[tuple[list, str]] | None = None):
    """An AnthropicProvider whose API calls record their kwargs and answer
    with real SDK response blocks, so the reply is dumped as in production."""
    captured: list[dict] = []
    script = list(replies or [])

    async def create(**kwargs):
        captured.append(copy.deepcopy(kwargs))
        content, stop = script.pop(0) if script else ([TextBlock(type="text", text="ok")], "end_turn")
        return SimpleNamespace(
            content=content,
            stop_reason=stop,
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        )

    provider = AnthropicProvider("test-key", model)
    api = SimpleNamespace(create=create)
    provider.client = SimpleNamespace(messages=api, beta=SimpleNamespace(messages=api))
    return provider, captured


def _legacy(title: str, data: str) -> dict:
    """A document block as threads stored it before the fix."""
    return {
        "type": "document",
        "name": title,
        "source": {"type": "base64", "media_type": "application/pdf", "data": data},
    }


# --- the contract, helper by helper ------------------------------------------


@APIS
def test_every_block_the_helpers_build_fits_the_sdk_request_type(types):
    reply = [
        TextBlock(type="text", text="Looking.").model_dump(exclude_none=True),
        ToolUseBlock(type="tool_use", id="toolu_1", name="read_attachment", input={}).model_dump(
            exclude_none=True
        ),
    ]
    user = with_context_block({"role": "user", "content": "what is in this?"}, "## Right now")
    blocks = [
        *user["content"],
        image_block("image/png", "QUJD"),
        document_block("application/pdf", "QUJD", "slides.pdf"),
        document_block("application/pdf", "QUJD"),
        *reply,
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "{}"},
    ]
    assert {b["type"] for b in blocks} >= {"text", "image", "document", "tool_use", "tool_result"}
    problems: list[str] = []
    for j, block in enumerate(blocks):
        problems += _problems(block, types, f"block.{j}")
        # Each also as the breakpoint leaves it, on the tail of the history.
        [marked] = with_history_breakpoint([{"role": "user", "content": [block]}])
        assert "cache_control" in marked["content"][0]
        problems += _problems(marked["content"][0], types, f"marked.{j}")
    assert problems == []


def test_a_new_document_block_is_titled_and_never_named():
    block = document_block("application/pdf", "QUJD", "slides.pdf")
    assert list(block) == ["type", "title", "source"]
    assert block["title"] == "slides.pdf"
    # No file name: no title, rather than an empty one.
    assert "title" not in document_block("application/pdf", "QUJD", "")
    assert "title" not in document_block("application/pdf", "QUJD")


# --- the contract, through a real turn ---------------------------------------


@pytest.mark.parametrize("model", ["claude-sonnet-5", "claude-opus-4-8", FALLBACK_MODEL])
def test_a_turn_with_attachments_and_a_tool_document_sends_only_known_keys(state, model):
    """An attached PDF and image, a read_attachment round that brings a PDF
    in beside its tool result, then a plain follow-up in the same thread."""
    pdf = _put(state, "slides-dk1.pdf", _pdf())
    deck = _put(state, "deck-dk2.pdf", _pdf(3))
    png = _put(state, "photo-dk3.png", _png())
    provider, captured = _anthropic(
        model,
        [
            (
                [
                    TextBlock(type="text", text="Opening the deck."),
                    ToolUseBlock(
                        type="tool_use", id="toolu_1", name="read_attachment", input={"path": deck}
                    ),
                ],
                "tool_use",
            ),
            ([TextBlock(type="text", text="Two decks and a photo.")], "end_turn"),
            ([TextBlock(type="text", text="Hello.")], "end_turn"),
        ],
    )
    overseer = Overseer(provider, state)

    result = asyncio.run(overseer.chat("what is in these?", images=[pdf, png]))
    asyncio.run(overseer.chat("hello", session_id=result["session_id"]))

    assert len(captured) == 3
    types = BETA if model == FALLBACK_MODEL else PLAIN
    for kwargs in captured:
        assert _request_problems(kwargs, types) == []
    # The attachment and the tool's document both went out titled.
    last = captured[-1]["messages"]
    documents = [
        b for m in last if isinstance(m["content"], list) for b in m["content"] if b["type"] == "document"
    ]
    assert [d["title"] for d in documents] == ["slides-dk1.pdf", "deck-dk2.pdf"]
    tool_turn = next(
        m for m in last if isinstance(m["content"], list) and m["content"][0]["type"] == "tool_result"
    )
    assert [b["type"] for b in tool_turn["content"]] == ["tool_result", "document"]


# --- threads saved before the fix --------------------------------------------


def _poisoned_history(data: str) -> list[dict]:
    """A thread from before the fix: an attached PDF, and one a tool read."""
    return [
        {
            "role": "user",
            "content": [{"type": "text", "text": "read this"}, _legacy("old-dk4.pdf", data)],
        },
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "And the other."},
                {"type": "tool_use", "id": "toolu_9", "name": "read_attachment", "input": {}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_9", "content": "{}"},
                _legacy("tool-dk5.pdf", data),
            ],
        },
        {"role": "assistant", "content": "Read both."},
    ]


@pytest.mark.parametrize("model", ["claude-sonnet-5", "claude-opus-4-8", FALLBACK_MODEL])
def test_an_old_thread_with_named_documents_works_again(state, model):
    history = _poisoned_history(_b64(_pdf()))
    stored = copy.deepcopy(history)
    provider, captured = _anthropic(model)
    overseer = Overseer(provider, state)
    overseer.sessions["old-thread"] = history

    result = asyncio.run(overseer.chat("hello", session_id="old-thread"))

    assert result["reply"] == "ok"
    [kwargs] = captured
    assert _request_problems(kwargs, BETA if model == FALLBACK_MODEL else PLAIN) == []
    sent = kwargs["messages"]
    assert sent[0]["content"][1] == {
        "type": "document",
        "title": "old-dk4.pdf",
        "source": history[0]["content"][1]["source"],
    }
    assert list(sent[2]["content"][1]) == ["type", "title", "source"]
    assert sent[2]["content"][1]["title"] == "tool-dk5.pdf"
    # The stored history is left as it was saved; only the request changed.
    assert overseer.sessions["old-thread"][: len(stored)] == stored
    assert "name" in overseer.sessions["old-thread"][0]["content"][1]


def test_the_rename_leaves_the_callers_history_alone_and_shares_what_it_can():
    data = _b64(b"%PDF-1.4")
    history = _poisoned_history(data)
    before = copy.deepcopy(history)

    sent = with_document_titles(history)

    assert history == before
    assert sent is not history
    # Messages with nothing to rename are the very same objects.
    assert sent[1] is history[1] and sent[3] is history[3]
    # And within a renamed message, so are the other blocks.
    assert sent[2]["content"][0] is history[2]["content"][0]
    assert sent[0]["content"][1]["source"] is history[0]["content"][1]["source"]
    # A history with nothing to rename comes straight back.
    fresh = [{"role": "user", "content": [document_block("application/pdf", data, "new.pdf")]}]
    assert with_document_titles(fresh) is fresh


def test_the_rename_keeps_a_title_already_there_and_drops_an_empty_name():
    data = _b64(b"%PDF-1.4")
    both = {**_legacy("named.pdf", data), "title": "titled.pdf"}
    empty = _legacy("", data)

    [message] = with_document_titles([{"role": "user", "content": [both, empty]}])

    assert message["content"][0] == {
        "type": "document",
        "source": both["source"],
        "title": "titled.pdf",
    }
    assert message["content"][1] == {"type": "document", "source": empty["source"]}


@pytest.mark.parametrize("model", ["claude-sonnet-5", "claude-opus-4-8"])
def test_an_old_thread_sends_the_same_bytes_every_turn(model):
    """The prompt cache is a prefix match: the renamed history must come out
    byte for byte the same on every request, with the breakpoint where it
    would sit had the blocks been stored titled."""
    data = _b64(b"%PDF-1.4")
    old = _poisoned_history(data)
    titled = copy.deepcopy(old)
    for message in titled:
        for block in message["content"] if isinstance(message["content"], list) else []:
            if block["type"] == "document":
                block["title"] = block.pop("name")
                block["source"] = block.pop("source")  # title before source, as stored
    provider, captured = _anthropic(model)
    turn = [*old, {"role": "user", "content": "hello"}]

    for messages in (turn, turn, [*titled, {"role": "user", "content": "hello"}]):
        asyncio.run(provider.chat(["S", "M"], messages, [], context="## Right now\nnoon"))

    first, second, reference = (json.dumps(k["messages"]) for k in captured)
    assert first == second == reference


# --- the other readers of the key --------------------------------------------


def test_openai_labels_the_extracted_text_from_either_key():
    data = _b64(b"%PDF-1.4")
    history = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "compare"},
                _legacy("old-dk6.pdf", data),
                document_block("application/pdf", data, "new-dk7.pdf"),
            ],
        }
    ]

    text = to_openai_input(history, {data: "Page one."})[0]["content"]

    assert "attached PDF old-dk6.pdf" in text
    assert "attached PDF new-dk7.pdf" in text
    # Untitled falls back to a generic label.
    untitled = [{"role": "user", "content": [document_block("application/pdf", data)]}]
    assert "attached PDF document.pdf" in to_openai_input(untitled, {data: "x"})[0]["content"]


def test_gemini_sends_either_shape_as_the_same_inline_pdf():
    """Gemini's inline_data part has no file name, so there is nothing to
    label; both shapes must still arrive as the PDF itself."""
    data = _b64(b"%PDF-1.4")
    old = to_gemini_contents([{"role": "user", "content": [_legacy("old.pdf", data)]}])
    new = to_gemini_contents(
        [{"role": "user", "content": [document_block("application/pdf", data, "new.pdf")]}]
    )

    assert old == new == [
        {"role": "user", "parts": [{"inline_data": {"mime_type": "application/pdf", "data": data}}]}
    ]


def test_a_shed_attachment_is_named_from_either_key():
    data = "A" * 1000
    messages = [
        {"role": "user", "content": [_legacy("old-dk8.pdf", data)]},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": [document_block("application/pdf", data, "new-dk9.pdf")]},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": [document_block("application/pdf", data, "keep.pdf")]},
    ]

    shed, _ = shed_media(messages, 1000)

    assert shed[0]["content"][0]["text"] == "[attachment no longer in context: old-dk8.pdf]"
    assert shed[2]["content"][0]["text"] == "[attachment no longer in context: new-dk9.pdf]"
    assert shed[4] is messages[4]
