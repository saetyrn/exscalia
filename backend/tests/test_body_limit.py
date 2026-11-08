"""The Content-Length guard in front of the Paper uploads.

Starlette spools every multipart file part to disk before a route's own
``cap + 1`` read runs, so the per-part caps cannot stop a huge upload from
landing in the container's tmp. ``body_limit`` answers 413 from the declared
length alone; these tests pin that the body never reaches the route, that
honest requests still do, and that nothing outside ``/api/paper`` is limited.
"""

import json
import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app import body_limit
from app.api import paper as paper_api
from app.body_limit import limit_for, reject_oversized_bodies
from app.paper_store import (
    MAX_ASSET_BYTES,
    MAX_DRAWING_BYTES,
    MAX_RENDER_BYTES,
    MAX_TEXT_BYTES,
)
from app.vault.notes import NoteError

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32
PAGE_ID = "a" * 32
CADDYFILE = Path(__file__).resolve().parents[2] / "Caddyfile"


def make_client(state) -> TestClient:
    app = FastAPI()
    app.state = state

    @app.exception_handler(NoteError)
    async def handler(_request, exc: NoteError):
        return JSONResponse(status_code=exc.status, content={"detail": exc.detail})

    app.middleware("http")(reject_oversized_bodies)
    app.include_router(paper_api.router)
    return TestClient(app)


@pytest.fixture
def tight(monkeypatch):
    """Shrink the limits so a few KB is oversized. The routes keep their real
    caps, so a 413 here can only have come from the middleware."""
    for name in (
        "MAX_DRAWING_BYTES",
        "MAX_RENDER_BYTES",
        "MAX_TEXT_BYTES",
        "MAX_ASSET_BYTES",
        "MAX_SELECTION_IMAGE_BYTES",
    ):
        monkeypatch.setattr(body_limit, name, 64)
    monkeypatch.setattr(body_limit, "SLACK_BYTES", 1024)
    monkeypatch.setattr(body_limit, "MAX_JSON_BODY_BYTES", 256)


def new_notebook(client, title="Ideas") -> dict:
    response = client.post("/api/paper/notebooks", json={"title": title})
    assert response.status_code == 200, response.text
    return response.json()


def put_drawing(client, notebook: str, drawing: bytes):
    return client.put(
        f"/api/paper/pages/{notebook}/{PAGE_ID}",
        data={"base_revision": "0"},
        files={"drawing": ("drawing.pkdrawing", drawing, "application/octet-stream")},
    )


def assert_rejected_by_middleware(response) -> None:
    assert response.status_code == 413, response.text
    assert response.json()["detail"].startswith("Request is larger than")


# -- the limit table ---------------------------------------------------------


def test_each_paper_upload_route_gets_its_parts_plus_slack():
    page = limit_for("PUT", f"/api/paper/pages/paper/Work/Ideas.paper/{PAGE_ID}")
    assert page == (
        MAX_DRAWING_BYTES + MAX_RENDER_BYTES + 2 * MAX_TEXT_BYTES + body_limit.SLACK_BYTES
    )
    asset = limit_for("POST", "/api/paper/assets/paper/Ideas.paper")
    assert asset == MAX_ASSET_BYTES + body_limit.SLACK_BYTES
    selection = limit_for("POST", "/api/paper/selection-action")
    assert selection == paper_api.MAX_SELECTION_IMAGE_BYTES + body_limit.SLACK_BYTES


def test_other_paper_writes_are_json_sized():
    for method, path in (
        ("POST", "/api/paper/notebooks"),
        ("POST", "/api/paper/notebooks/paper/Ideas.paper/pages"),
        ("PATCH", "/api/paper/notebooks/paper/Ideas.paper"),
        ("POST", "/api/paper/trash/abc/restore"),
        ("POST", f"/api/paper/history/paper/Ideas.paper/{PAGE_ID}/restore"),
    ):
        assert limit_for(method, path) == body_limit.MAX_JSON_BODY_BYTES, path


def test_reads_and_everything_outside_paper_are_unlimited():
    for method, path in (
        ("GET", f"/api/paper/pages/paper/Ideas.paper/{PAGE_ID}"),
        ("DELETE", f"/api/paper/pages/paper/Ideas.paper/{PAGE_ID}"),
        ("GET", "/api/paper"),
        ("POST", "/api/attachments"),
        ("PUT", "/api/notes/Inbox/idea.md"),
        ("POST", "/api/auth/login"),
        ("GET", "/api/health"),
        ("POST", "/api/paperwork"),
    ):
        assert limit_for(method, path) is None, (method, path)


def test_limits_follow_the_caps_at_call_time(tight):
    assert limit_for("POST", "/api/paper/assets/paper/Ideas.paper") == 64 + 1024
    assert limit_for("POST", "/api/paper/notebooks") == 256


def test_caddy_cap_sits_above_every_app_limit():
    """Caddy's outer cap must never refuse what a route would accept."""
    if not CADDYFILE.is_file():
        pytest.skip("no Caddyfile next to the backend")
    match = re.search(r"max_size\s+(\d+)MB", CADDYFILE.read_text())
    assert match, "the site block has no request_body max_size"
    caddy_bytes = int(match.group(1)) * 1000 * 1000  # Caddy's MB is decimal
    largest = max(
        limit_for("PUT", f"/api/paper/pages/paper/Ideas.paper/{PAGE_ID}"),
        limit_for("POST", "/api/paper/assets/paper/Ideas.paper"),
        limit_for("POST", "/api/paper/selection-action"),
    )
    assert largest < caddy_bytes


# -- the middleware ----------------------------------------------------------


def test_an_oversized_page_save_is_refused_before_the_route(state, tight):
    client = make_client(state)
    nb = new_notebook(client)

    assert_rejected_by_middleware(put_drawing(client, nb["path"], b"x" * 4096))
    assert state.paper.read_notebook(nb["path"])["pages"] == []
    assert client.get(f"/api/paper/pages/{nb['path']}/{PAGE_ID}").status_code == 404

    saved = put_drawing(client, nb["path"], b"ink")
    assert saved.status_code == 200, saved.text
    assert saved.json()["revision"] == 1


def test_an_oversized_asset_is_refused_before_the_route(state, tight):
    client = make_client(state)
    nb = new_notebook(client)
    assets = state.paper.resolve_notebook(nb["path"]) / "assets"

    refused = client.post(
        f"/api/paper/assets/{nb['path']}",
        files={"file": ("Scan.png", PNG + b"0" * 4096, "image/png")},
    )
    assert_rejected_by_middleware(refused)
    assert not assets.exists()

    stored = client.post(
        f"/api/paper/assets/{nb['path']}",
        files={"file": ("Scan.png", PNG, "image/png")},
    )
    assert stored.status_code == 200, stored.text
    assert [p.name for p in assets.iterdir()] == [stored.json()["name"]]


def test_an_oversized_selection_crop_is_refused_before_the_route(state, tight):
    client = make_client(state)
    response = client.post(
        "/api/paper/selection-action",
        data={"notebook_id": "nb", "page_id": PAGE_ID, "action": "ask"},
        files={"image": ("crop.png", PNG + b"0" * 4096, "image/png")},
    )
    assert_rejected_by_middleware(response)


def test_an_oversized_json_write_is_refused(state, tight):
    client = make_client(state)
    refused = client.post("/api/paper/notebooks", json={"title": "x" * 1024})
    assert_rejected_by_middleware(refused)
    assert state.paper.list_library()["notebooks"] == []


def test_a_body_without_content_length_passes_through(state, tight):
    """Chunked bodies declare nothing to check; Caddy's cap covers them."""
    client = make_client(state)
    payload = json.dumps({"title": "Chunked " + "x" * 1024}).encode()

    def chunks():
        yield payload[:100]
        yield payload[100:]

    response = client.post(
        "/api/paper/notebooks",
        content=chunks(),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 200, response.text
    assert len(state.paper.list_library()["notebooks"]) == 1


def test_reads_pass_through_whatever_they_declare(state, tight):
    client = make_client(state)
    response = client.request("GET", "/api/paper", content=b"x" * 4096)
    assert response.status_code == 200, response.text
