"""/api/attachments: what the browser is told about caching a blob.

Attachment filenames carry a digest of the bytes, so a URL's content can never
change under it. Saying so lets the browser skip the round-trip entirely — the
difference between a media hub that repaints instantly and one that refetches
every poster on every visit.
"""
import os
from urllib.parse import unquote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import attachments as attachments_api
from app.vault.notes import NoteError, NoteStore, safe_stem


def make_client(state) -> TestClient:
    app = FastAPI()
    app.state = state

    @app.exception_handler(NoteError)
    async def handler(_request, exc: NoteError):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=exc.status, content={"detail": exc.detail})

    app.include_router(attachments_api.router)
    return TestClient(app)


PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


def test_upload_then_fetch_is_cacheable_forever(state):
    client = make_client(state)

    uploaded = client.post(
        "/api/attachments", files={"file": ("Poster Art.png", PNG, "image/png")}
    )
    assert uploaded.status_code == 200, uploaded.text
    url = uploaded.json()["url"]

    fetched = client.get(url)
    assert fetched.status_code == 200
    assert fetched.headers["cache-control"] == "private, max-age=31536000, immutable"
    # The rest of the contract the route has always had, unchanged.
    assert fetched.headers["content-type"] == "image/png"
    assert fetched.headers["content-disposition"] == "inline"
    assert fetched.headers["x-content-type-options"] == "nosniff"


def test_unhashed_names_revalidate_every_time(state):
    """A file dropped into the vault by hand carries no digest, so nothing
    promises its bytes are fixed — it must revalidate on every use."""
    client = make_client(state)
    target = state.store.resolve_attachment(f"{NoteStore.ATTACHMENT_DIR}/hand-placed.png")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(PNG)

    fetched = client.get("/api/attachments/hand-placed.png")
    assert fetched.status_code == 200
    assert fetched.headers["cache-control"] == "private, no-cache"


def place(state, name: str, data: bytes = b"PK\x03\x04") -> None:
    target = state.store.resolve_attachment(f"{NoteStore.ATTACHMENT_DIR}/{name}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def test_download_names_outside_latin1_are_sent_as_rfc5987(state):
    """Starlette encodes header values as latin-1, so a hand-built
    `filename="日本-….docx"` raised on the way out and the download was a 500."""
    client = make_client(state)
    place(state, "日本-deadbeef.docx")

    fetched = client.get("/api/attachments/日本-deadbeef.docx")
    assert fetched.status_code == 200
    disposition = fetched.headers["content-disposition"]
    assert disposition.startswith("attachment; filename*=utf-8''"), disposition
    assert unquote(disposition.split("''", 1)[1]) == "日本-deadbeef.docx"
    assert fetched.headers["content-type"] == "application/octet-stream"
    assert fetched.headers["x-content-type-options"] == "nosniff"
    assert fetched.headers["cache-control"] == "private, no-cache"


def test_a_quote_in_a_download_name_cannot_break_the_header(state):
    client = make_client(state)
    place(state, 'say "hi".docx')

    fetched = client.get("/api/attachments/say%20%22hi%22.docx")
    assert fetched.status_code == 200
    disposition = fetched.headers["content-disposition"]
    assert disposition == "attachment; filename*=utf-8''say%20%22hi%22.docx"
    assert unquote(disposition.split("''", 1)[1]) == 'say "hi".docx'


def test_plain_download_names_keep_the_quoted_form(state):
    client = make_client(state)
    place(state, "diagram-0123456789abcdef.docx")

    fetched = client.get("/api/attachments/diagram-0123456789abcdef.docx")
    assert fetched.status_code == 200
    assert (
        fetched.headers["content-disposition"]
        == 'attachment; filename="diagram-0123456789abcdef.docx"'
    )
    assert fetched.headers["cache-control"] == "private, max-age=31536000, immutable"


# The iPad names Paper assets with AssetStore.safeStem, which must agree with
# safe_stem byte for byte. AssetStoreTests.testStemMatchesTheVaultsSafeStem
# carries this same table; change both together.
SAFE_STEM_VECTORS = [
    ("Scan 01.PNG", "scan-01"),
    ("../../etc/passwd", "passwd"),
    (".....", "file"),
    ("a/b/c.pdf", "c"),
    ("a/b/", "b"),
    ("x/..", "file"),
    ("", "file"),
    ("a\\b.png", "a-b"),
    (".hidden", "hidden"),
    ("__init__.py", "init"),
    ("my.photo.v2.jpg", "my.photo.v2"),
    ("photo.tar.gz", "photo.tar"),
    ("IMG_2024 (1).HEIC", "img_2024-1"),
    ("Caf\u00e9 menu.jpg", "caf-menu"),
    ("Cafe\u0301 menu.jpg", "cafe-menu"),
    ("\u00dcbersicht Q3.pdf", "bersicht-q3"),
    ("\u65e5\u672c\u8a9e.pdf", "file"),
    ("A" * 70 + ".png", "a" * 60),
    ("a" * 59 + " b.png", "a" * 59 + "-"),
]


@pytest.mark.parametrize(("name", "stem"), SAFE_STEM_VECTORS)
def test_safe_stem_vectors_shared_with_the_ipad(name, stem):
    assert safe_stem(name) == stem
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


def attachment_files(state) -> list[str]:
    folder = state.store.dir / NoteStore.ATTACHMENT_DIR
    return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []


def test_svg_renders_as_an_image_under_a_sandbox(state):
    """An SVG must decode inside <img>, and opening its URL directly must not
    run its script with the app's origin."""
    client = make_client(state)
    uploaded = client.post(
        "/api/attachments", files={"file": ("Diagram.svg", SVG, "image/svg+xml")}
    )
    assert uploaded.status_code == 200, uploaded.text

    fetched = client.get(uploaded.json()["url"])
    assert fetched.status_code == 200 and fetched.content == SVG
    assert fetched.headers["content-type"] == "image/svg+xml"
    assert fetched.headers["content-security-policy"] == (
        "sandbox; default-src 'none'; style-src 'unsafe-inline'"
    )
    assert fetched.headers["content-disposition"] == "inline"
    assert fetched.headers["x-content-type-options"] == "nosniff"
    assert fetched.headers["cache-control"] == "private, max-age=31536000, immutable"


def test_types_without_an_inline_rule_still_download(state):
    client = make_client(state)
    uploaded = client.post(
        "/api/attachments",
        files={"file": ("Report.docx", b"PK\x03\x04", "application/octet-stream")},
    )
    fetched = client.get(uploaded.json()["url"])
    assert fetched.headers["content-type"] == "application/octet-stream"
    assert fetched.headers["content-disposition"].startswith("attachment;")
    assert "content-security-policy" not in fetched.headers


def test_a_short_file_under_the_hashed_name_is_repaired(state):
    """A write killed part-way leaves a truncated blob under its final name.
    The dedupe check must not trust it: the same bytes arriving again rewrite
    it, and no temp file is left behind."""
    client = make_client(state)
    first = client.post(
        "/api/attachments", files={"file": ("Poster.png", PNG, "image/png")}
    ).json()
    target = state.store.resolve_attachment(first["path"])
    target.write_bytes(PNG[:10])

    again = client.post(
        "/api/attachments", files={"file": ("Poster.png", PNG, "image/png")}
    ).json()
    assert again["path"] == first["path"]
    assert target.stat().st_size == len(PNG) and target.read_bytes() == PNG
    assert attachment_files(state) == [target.name]


def test_a_failed_write_leaves_nothing_under_the_final_name(state, monkeypatch):
    client = make_client(state)

    def refuse(_src, _dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(OSError):
        client.post("/api/attachments", files={"file": ("Poster.png", PNG, "image/png")})
    assert attachment_files(state) == []
