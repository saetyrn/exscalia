"""Attachments in provider history: costed by what the model sees, capped by
what the request can carry.

The budget used to count base64 at face value, so a single photo outweighed
the whole 400k history: saving the turn dropped every earlier exchange, and
one follow-up later the photo went too, leaving the next question about it to
be answered blind.
"""

from __future__ import annotations

import asyncio
import base64
import io
import os

from app.overseer import threads as threads_module
from app.overseer.agent import Overseer
from app.overseer.history import (
    DOCUMENT_CHARS,
    DOCUMENT_CHARS_DIVISOR,
    IMAGE_CHARS,
    MAX_MEDIA_BYTES,
    history_chars,
    normalize,
    repair_history,
    shed_media,
    user_turn_starts,
)
from app.overseer.provider import FakeProvider, ProviderReply, document_block, image_block
from app.overseer.threads import MAX_HISTORY_CHARS, ThreadStore

MB = 1024 * 1024


def _b64(size: int) -> str:
    return base64.b64encode(os.urandom(size)).decode("ascii")


def _payload(block: dict) -> str:
    return block["source"]["data"]


def _media(messages: list[dict]) -> list[dict]:
    return [
        block
        for message in messages
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if block.get("type") in ("image", "document")
    ]


def test_an_attached_image_keeps_its_conversation_and_itself():
    image = image_block("image/jpeg", _b64(600 * 1024))
    assert len(_payload(image)) > MAX_HISTORY_CHARS  # alone it outweighed everything
    messages = [
        {"role": "user", "content": "I am planning the kitchen shelves"},
        {"role": "assistant", "content": "Tell me the wall width."},
        {"role": "user", "content": [{"type": "text", "text": "this wall"}, image]},
        {"role": "assistant", "content": "About 2.4 m wide."},
        {"role": "user", "content": "how many shelves fit?"},
        {"role": "assistant", "content": "Four."},
        {"role": "user", "content": "and at what spacing?"},
        {"role": "assistant", "content": "30 cm."},
    ]

    normalized, notes = normalize(messages, MAX_HISTORY_CHARS)

    assert notes == []
    assert normalized == messages
    assert _media(normalized) == [image]


def test_an_image_costs_an_estimate_not_its_base64():
    text = {"type": "text", "text": "what is this?"}
    plain = {"role": "user", "content": [text]}
    image = image_block("image/png", _b64(MB))
    with_image = {"role": "user", "content": [text, image]}

    cost = history_chars([with_image]) - history_chars([plain])

    assert IMAGE_CHARS < cost < IMAGE_CHARS + 200
    # Costing works on a copy; the stored payload is untouched.
    assert len(_payload(image)) > MB


def test_a_document_costs_the_larger_of_a_floor_and_its_length():
    text = {"type": "text", "text": "summarize this"}
    plain = {"role": "user", "content": [text]}
    big = document_block("application/pdf", _b64(2 * MB), "report-q3.pdf")
    small = document_block("application/pdf", _b64(20 * 1024), "memo.pdf")

    big_cost = history_chars([{"role": "user", "content": [text, big]}]) - history_chars([plain])
    small_cost = history_chars([{"role": "user", "content": [text, small]}]) - history_chars(
        [plain]
    )

    expected = max(DOCUMENT_CHARS, len(_payload(big)) // DOCUMENT_CHARS_DIVISOR)
    assert expected > DOCUMENT_CHARS
    assert expected < big_cost < expected + 200
    assert DOCUMENT_CHARS < small_cost < DOCUMENT_CHARS + 200


def _media_heavy_history() -> list[dict]:
    """Five turns, each carrying just over 5 MB of base64."""
    data = "A" * (5 * MB + 1)
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "read the deck"},
                document_block("application/pdf", data, "deck-a.pdf"),
            ],
        },
        {"role": "assistant", "content": "Read."},
        {"role": "user", "content": "now look at the page"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "looking"},
                {"type": "tool_use", "id": "toolu_1", "name": "read_page", "input": {}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": '{"ok": true}'},
                image_block("image/png", data),
            ],
        },
        {"role": "assistant", "content": "Seen."},
        {
            "role": "user",
            "content": [{"type": "text", "text": "photo"}, image_block("image/jpeg", data)],
        },
        {"role": "assistant", "content": "A desk."},
        {
            "role": "user",
            "content": [{"type": "text", "text": "another"}, image_block("image/jpeg", data)],
        },
        {"role": "assistant", "content": "A chair."},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "latest"},
                document_block("application/pdf", data, "latest-b.pdf"),
            ],
        },
        {"role": "assistant", "content": "Got it."},
    ]


def test_past_the_byte_ceiling_the_oldest_attachments_become_a_note():
    messages = _media_heavy_history()
    media = _media(messages)
    assert sum(len(_payload(b)) for b in media) > 25 * MB

    shed, notes = shed_media(messages, MAX_MEDIA_BYTES)

    assert notes
    remaining = _media(shed)
    assert sum(len(_payload(b)) for b in remaining) <= MAX_MEDIA_BYTES
    # Oldest first: the deck, then the image beside the tool result.
    assert shed[0]["content"][1] == {
        "type": "text",
        "text": "[attachment no longer in context: deck-a.pdf]",
    }
    assert shed[4]["content"][1] == {
        "type": "text",
        "text": "[attachment no longer in context: image]",
    }
    # The newest attachments are the very same blocks.
    assert len(remaining) == 3
    assert all(a is b for a, b in zip(remaining, media[2:]))
    # Tool traffic is untouched, so pairs and turn boundaries hold.
    assert shed[3] is messages[3]
    assert shed[4]["content"][0] is messages[4]["content"][0]
    assert repair_history(shed) == (shed, [])
    assert user_turn_starts(shed) == user_turn_starts(messages)
    # The caller's history is not edited in place.
    assert _media(messages) == media

    # normalize() applies the same guard on every load and persist.
    normalized, normalize_notes = normalize(messages, MAX_HISTORY_CHARS)
    assert normalized == shed
    assert notes[0] in normalize_notes


def test_media_under_the_ceiling_is_handed_straight_back():
    messages = _media_heavy_history()[6:]  # three attachments, ~15 MB

    shed, notes = shed_media(messages, MAX_MEDIA_BYTES)

    assert shed is messages
    assert notes == []


def _noise_png(size=(120, 120)) -> bytes:
    """A PNG that does not compress, so its base64 is large for its size."""
    from PIL import Image

    buf = io.BytesIO()
    Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3)).save(buf, format="PNG")
    return buf.getvalue()


def test_follow_ups_still_see_the_attached_image(state, tmp_path, monkeypatch):
    """The real path: load the thread's history, run the turn, persist it —
    as api/overseer.py does — with a ceiling the image's base64 alone breaks."""
    monkeypatch.setattr(threads_module, "MAX_HISTORY_CHARS", 20_000)
    rel = "attachments/wall-q1.png"
    path = state.store.resolve_attachment(rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_noise_png())

    threads = ThreadStore(state.settings.vault_dir, tmp_path / "data")
    thread_id = threads.create()["id"]
    provider = FakeProvider(
        [
            ProviderReply(text="A bare wall.", stop_reason="end_turn"),
            ProviderReply(text="Off-white.", stop_reason="end_turn"),
            ProviderReply(text="About 2.4 m.", stop_reason="end_turn"),
        ]
    )
    overseer = Overseer(provider, state)

    for message, images in (
        ("what is this?", [rel]),
        ("what colour is it?", None),
        ("how wide is it?", None),
    ):
        overseer.sessions[thread_id] = threads.provider_history(thread_id)
        asyncio.run(overseer.chat(message, session_id=thread_id, images=images))
        threads.set_provider_history(thread_id, list(overseer.sessions[thread_id]))

    sent = provider.seen[-1]
    image = _media(sent)
    assert len(image) == 1 and len(_payload(image[0])) > 20_000
    # The opening question is still there, with the image beside it.
    assert sent[0]["content"] == [{"type": "text", "text": "what is this?"}, image[0]]
