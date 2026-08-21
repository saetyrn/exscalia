"""/api/attachments: what the browser is told about caching a blob.

Attachment filenames carry a digest of the bytes, so a URL's content can never
change under it. Saying so lets the browser skip the round-trip entirely — the
difference between a media hub that repaints instantly and one that refetches
every poster on every visit.
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import attachments as attachments_api
from app.vault.notes import NoteError, NoteStore


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
