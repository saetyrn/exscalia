"""Attachment work stays off the event loop.

Decoding and downscaling a phone photo, counting a PDF's pages or pulling its
text is CPU work — tens of milliseconds for one 12 MP JPEG, seconds for a slide
deck through pypdf. Run inline in the async chat path, it froze the single
event loop: autosaves from other tabs, SSE heartbeats, the iPad's sync and the
chat's own first deltas all waited behind it.
"""
import asyncio
import base64
import time
from types import SimpleNamespace

from app.overseer import agent, tool_expansion, vision
from app.overseer.agent import Overseer
from app.overseer.provider import FakeProvider, OpenAIProvider, ProviderReply, document_block

# A loop that can run a 10 ms sleep within this much of on time was never
# blocked; the stand-in loaders below each hold their thread for 200 ms.
MAX_LAG = 0.1
SLOW = 0.2


async def _worst_lag(work):
    """Await `work` while a probe sleeps 10 ms at a time. Returns the worst
    overshoot and what `work` returned."""
    loop = asyncio.get_running_loop()
    done = asyncio.Event()
    worst = 0.0

    async def probe():
        nonlocal worst
        while not done.is_set():
            start = loop.time()
            await asyncio.sleep(0.01)
            worst = max(worst, loop.time() - start - 0.01)

    task = asyncio.create_task(probe())
    await asyncio.sleep(0)
    try:
        result = await work
    finally:
        done.set()
        await task
    return worst, result


def test_attached_images_load_off_the_event_loop(state, monkeypatch):
    calls: list[str] = []

    def slow_load_image(store, rel_path):
        calls.append(rel_path)
        time.sleep(SLOW)
        return {"media_type": "image/png", "data": "AAAA"}

    monkeypatch.setattr(agent, "load_image", slow_load_image)
    provider = FakeProvider([ProviderReply(text="Two photos.", stop_reason="end_turn")])
    overseer = Overseer(provider, state)

    lag, _ = asyncio.run(
        _worst_lag(
            overseer.chat(
                "what are these?", images=["attachments/a.png", "attachments/b.png"]
            )
        )
    )

    assert calls == ["attachments/a.png", "attachments/b.png"]
    assert [b["type"] for b in provider.seen[-1][0]["content"]] == ["text", "image", "image"]
    assert lag < MAX_LAG, f"the event loop stalled for {lag:.3f}s"


def test_viewed_attachments_load_off_the_event_loop(state, monkeypatch):
    def slow_load_image(store, rel_path):
        time.sleep(SLOW)
        return {"media_type": "image/png", "data": "AAAA"}

    monkeypatch.setattr(tool_expansion, "load_image", slow_load_image)

    lag, result = asyncio.run(
        _worst_lag(
            tool_expansion.read_attachment(
                state, "t1", {"paths": ["attachments/a.png", "attachments/b.png"]}
            )
        )
    )

    assert [item["kind"] for item in result["_images"]] == ["image", "image"]
    assert lag < MAX_LAG, f"the event loop stalled for {lag:.3f}s"


class _Responses:
    def __init__(self):
        self.kwargs: list[dict] = []

    async def create(self, **kwargs):
        self.kwargs.append(kwargs)
        part = SimpleNamespace(type="output_text", text="A deck.")
        return SimpleNamespace(
            output=[SimpleNamespace(type="message", content=[part])],
            incomplete_details=None,
            usage=None,
        )


def test_openai_extracts_pdf_text_off_the_event_loop(monkeypatch):
    """Every PDF in the history is reduced to its text for OpenAI — on every
    round, before this, on the loop."""
    calls: list[bytes] = []

    def slow_extract(raw, limit=60_000):
        calls.append(raw)
        time.sleep(SLOW)
        return f"Text of {raw.decode()}."

    monkeypatch.setattr(vision, "extract_pdf_text", slow_extract)
    responses = _Responses()
    provider = OpenAIProvider("test-key", "gpt-6.1-sol")
    provider.client = SimpleNamespace(responses=responses)
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "compare these"},
                document_block("application/pdf", base64.b64encode(b"deck one").decode(), "a.pdf"),
                document_block("application/pdf", base64.b64encode(b"deck two").decode(), "b.pdf"),
            ],
        }
    ]

    lag, _ = asyncio.run(_worst_lag(provider.chat("system", messages, [])))

    # Once per document: the converter looked the text up rather than
    # extracting again on the loop.
    assert calls == [b"deck one", b"deck two"]
    sent = responses.kwargs[-1]["input"][-1]["content"]
    assert "Text of deck one." in sent and "Text of deck two." in sent
    assert lag < MAX_LAG, f"the event loop stalled for {lag:.3f}s"
