"""Uploading an image directly to OMNIA in a chat message."""
import asyncio
import io

import pytest

from app.api.overseer import _validated_images
from app.overseer.agent import MAX_MESSAGE_IMAGES, Overseer
from app.overseer.provider import FakeProvider, ProviderReply
from app.vault.notes import NoteError


def _png(size=(40, 30)) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, (10, 120, 200)).save(buf, format="PNG")
    return buf.getvalue()


def _put(state, name: str) -> str:
    rel = f"attachments/{name}"
    p = state.store.resolve_attachment(rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(_png())
    return rel


def test_attached_image_reaches_the_model_as_an_image_block(state):
    rel = _put(state, "slide-a1.png")
    provider = FakeProvider([ProviderReply(text="A blue slide.", stop_reason="end_turn")])

    asyncio.run(Overseer(provider, state).chat("what is this?", images=[rel]))

    sent = provider.seen[-1][0]
    kinds = [b["type"] for b in sent["content"]]
    assert kinds == ["text", "image"]
    image = sent["content"][1]
    assert image["source"]["media_type"] == "image/png"
    assert image["source"]["data"]  # base64 payload present
    # The vault path must not leak into the text the model reads.
    assert "attachments/" not in sent["content"][0]["text"]


def test_image_only_message_is_allowed(state):
    rel = _put(state, "board-b2.png")
    provider = FakeProvider([ProviderReply(text="A whiteboard.", stop_reason="end_turn")])

    asyncio.run(Overseer(provider, state).chat("", images=[rel]))

    sent = provider.seen[-1][0]
    assert [b["type"] for b in sent["content"]] == ["image"]


def test_plain_messages_keep_their_simple_shape(state):
    provider = FakeProvider([ProviderReply(text="ok", stop_reason="end_turn")])
    asyncio.run(Overseer(provider, state).chat("no images here"))
    assert provider.seen[-1][0]["content"] == "no images here"


def test_validation_rejects_traversal_missing_and_unsupported_types(state):
    real = _put(state, "ok-c3.png")
    # SVG uploads are allowed but can carry script, so they are never handed
    # to the model as an attachment.
    svg = state.store.resolve_attachment("attachments/logo-e5.svg")
    svg.parent.mkdir(parents=True, exist_ok=True)
    svg.write_bytes(b"<svg xmlns='http://www.w3.org/2000/svg'></svg>")

    assert _validated_images(state, [real, real]) == [real]  # deduped

    for bad in (
        ["attachments/../../etc/passwd.png"],
        ["attachments/ghost.png"],
        ["attachments/logo-e5.svg"],
    ):
        with pytest.raises(NoteError):
            _validated_images(state, bad)


def test_too_many_images_is_refused(state):
    many = [_put(state, f"img-{i}.png") for i in range(MAX_MESSAGE_IMAGES + 1)]
    with pytest.raises(NoteError):
        _validated_images(state, many)


def _pdf(pages: int = 2) -> bytes:
    """A small real PDF with extractable text."""
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _put_pdf(state, name: str, pages: int = 2) -> str:
    rel = f"attachments/{name}"
    p = state.store.resolve_attachment(rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(_pdf(pages))
    return rel


def test_pdf_reaches_anthropic_and_gemini_as_a_document(state):
    from app.overseer.provider import to_gemini_contents

    rel = _put_pdf(state, "slides-f6.pdf")
    provider = FakeProvider([ProviderReply(text="Six slides.", stop_reason="end_turn")])

    asyncio.run(Overseer(provider, state).chat("summarize these slides", images=[rel]))

    sent = provider.seen[-1][0]
    assert [b["type"] for b in sent["content"]] == ["text", "document"]
    doc = sent["content"][1]
    assert doc["source"]["media_type"] == "application/pdf"
    assert doc["name"].endswith(".pdf")

    # Gemini takes PDFs through the same inline_data part as images.
    inline = to_gemini_contents([sent])[0]["parts"][1]["inline_data"]
    assert inline["mime_type"] == "application/pdf"


def test_openai_degrades_a_pdf_to_labelled_text(state):
    """For OpenAI the PDF becomes extracted text — and the message says so
    rather than implying the model saw it."""
    from app.overseer.provider import document_block, to_openai_input

    history = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "what is in this?"},
                document_block("application/pdf", _b64(_pdf()), "slides-g7.pdf"),
            ],
        }
    ]

    converted = to_openai_input(history)
    text = converted[0]["content"]
    assert "slides-g7.pdf" in text
    assert "cannot display the document itself" in text


def test_oversized_and_overlong_pdfs_are_refused(state):
    from app.overseer.vision import MAX_DOCUMENT_PAGES, load_document

    too_many = _put_pdf(state, "huge-h8.pdf", pages=MAX_DOCUMENT_PAGES + 1)
    with pytest.raises(NoteError) as exc:
        load_document(state.store, too_many)
    assert exc.value.status == 413

    # A non-PDF must not be loadable as a document.
    png = _put(state, "not-a-doc-i9.png")
    with pytest.raises(NoteError):
        load_document(state.store, png)


def test_pdfs_are_attachable_alongside_images(state):
    pdf = _put_pdf(state, "sheet-j10.pdf")
    png = _put(state, "photo-k11.png")
    assert _validated_images(state, [pdf, png]) == [pdf, png]


def _b64(raw: bytes) -> str:
    import base64

    return base64.b64encode(raw).decode("ascii")


def _docx_bytes(paragraphs: list[str]) -> bytes:
    from docx import Document

    doc = Document()
    for line in paragraphs:
        doc.add_paragraph(line)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _put_docx(state, name: str, paragraphs: list[str]) -> str:
    rel = f"attachments/{name}"
    p = state.store.resolve_attachment(rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(_docx_bytes(paragraphs))
    return rel


def test_attached_docx_reaches_the_model_as_a_text_block(state):
    rel = _put_docx(state, "brief-l12.docx", ["Quarterly brief", "Revenue is up 12%."])
    provider = FakeProvider([ProviderReply(text="Got it.", stop_reason="end_turn")])

    asyncio.run(Overseer(provider, state).chat("summarize this", images=[rel]))

    sent = provider.seen[-1][0]
    # No document/image block — the docx becomes a second text block.
    assert [b["type"] for b in sent["content"]] == ["text", "text"]
    doc_block = sent["content"][1]
    assert "[Attached document: brief-l12.docx]" in doc_block["text"]
    assert "Quarterly brief" in doc_block["text"]
    assert "Revenue is up 12%." in doc_block["text"]


def test_corrupt_docx_degrades_gracefully(state):
    rel = "attachments/broken-m13.docx"
    p = state.store.resolve_attachment(rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"definitely not a docx")
    provider = FakeProvider([ProviderReply(text="ok", stop_reason="end_turn")])

    # The turn still completes; the model just gets a note that it failed.
    asyncio.run(Overseer(provider, state).chat("read this", images=[rel]))

    sent = provider.seen[-1][0]
    assert [b["type"] for b in sent["content"]] == ["text", "text"]
    assert "could not be read" in sent["content"][1]["text"]


def test_docx_is_attachable_alongside_images_and_pdfs(state):
    docx = _put_docx(state, "memo-n14.docx", ["Hello"])
    pdf = _put_pdf(state, "sheet-o15.pdf")
    png = _put(state, "photo-p16.png")
    assert _validated_images(state, [docx, pdf, png]) == [docx, pdf, png]
