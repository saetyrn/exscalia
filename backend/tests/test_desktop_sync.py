"""Backend surface the desktop app depends on: bearer-token auth and the
sync manifest endpoint."""
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.api import auth as auth_module
from app.api import notes as notes_module
from app.index.db import content_hash


def make_auth_app(tmp_path: Path) -> FastAPI:
    app = FastAPI()
    app.state.auth = auth_module.SessionAuth(
        "alice", "correct horse", "test-secret", tmp_path
    )
    app.include_router(auth_module.router)
    return app


def test_login_with_create_token_returns_token_without_cookie(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_module, "_approximate_location", lambda _ip: "Test location")
    client = TestClient(make_auth_app(tmp_path), base_url="https://testserver")

    response = client.post(
        "/api/auth/login",
        json={
            "username": "alice",
            "password": "correct horse",
            "create_token": True,
            "client": "OmniaNote Desktop",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["token"]
    assert "set-cookie" not in response.headers

    # The token works as a bearer header, and the session carries the client
    # label so the sessions list identifies the desktop app.
    client.cookies.clear()
    headers = {"Authorization": f"Bearer {body['token']}"}
    sessions = client.get("/api/auth/sessions", headers=headers).json()["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["current"] is True
    assert sessions[0]["browser"] == "OmniaNote Desktop"

    # Logout via bearer revokes the session server-side.
    assert client.post("/api/auth/logout", headers=headers).json() == {"ok": True}
    assert client.get("/api/auth/sessions", headers=headers).status_code == 401


def test_cookie_login_still_sets_host_cookie(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_module, "_approximate_location", lambda _ip: "Test location")
    client = TestClient(make_auth_app(tmp_path), base_url="https://testserver")

    response = client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "correct horse"},
    )
    assert response.status_code == 200
    assert "token" not in response.json()
    assert "__Host-omnia_session=" in response.headers["set-cookie"]


def test_bearer_token_rejected_when_invalid(tmp_path):
    client = TestClient(make_auth_app(tmp_path), base_url="https://testserver")
    response = client.get(
        "/api/auth/sessions", headers={"Authorization": "Bearer not-a-real-token"}
    )
    assert response.status_code == 401


def test_manifest_lists_every_note_with_revision(state):
    app = FastAPI()
    app.state = state
    app.include_router(notes_module.router)

    @app.exception_handler(Exception)
    async def err(_request: Request, exc: Exception):  # pragma: no cover
        return JSONResponse(status_code=500, content={"detail": str(exc)})

    client = TestClient(app)

    first = client.post(
        "/api/notes", json={"path": "Alpha.md", "content": "# Alpha\n\nHello.\n"}
    ).json()
    client.post("/api/notes", json={"path": "sub/Beta.md", "content": "Beta body.\n"})
    client.post("/api/notes/folders", json={"path": "empty-folder"})
    client.post("/api/notes/Alpha.md/pin", json={"pinned": True})

    manifest = client.get("/api/notes/manifest").json()
    notes = {n["path"]: n for n in manifest["notes"]}

    assert set(notes) >= {"Alpha.md", "sub/Beta.md"}
    assert "empty-folder" in manifest["folders"]
    assert notes["Alpha.md"]["pinned"] is True
    assert notes["sub/Beta.md"]["pinned"] is False

    # Revisions must match what GET /api/notes/{path} reports, and be the
    # sha256 of the current content — the invariant the sync engine relies on.
    read = client.get("/api/notes/Alpha.md").json()
    assert notes["Alpha.md"]["revision"] == read["revision"]
    assert read["revision"] == content_hash(read["content"])
    # Pinning rewrote the frontmatter, so the revision moved past the one the
    # create returned — clients must refetch after pin, never assume.
    assert first["revision"] != read["revision"]


def test_conflict_copy_of_note_with_tasks_gets_fresh_task_ids(state):
    app = FastAPI()
    app.state = state
    app.include_router(notes_module.router)
    client = TestClient(app)

    created = client.post(
        "/api/notes",
        json={"path": "Tasky.md", "content": "# Tasky\n\n- [ ] do the thing\n"},
    ).json()
    base = created["revision"]

    # Device A saves normally; device B then saves off the old base with the
    # copy fallback. Before the fix this 500ed whenever the note contained a
    # task, because the copy carried the original's task ids into the index.
    a = client.put(
        f"/api/notes/Tasky.md",
        json={"content": created["content"] + "\nA's line.\n", "base_revision": base},
    ).json()
    assert a["status"] == "saved"

    b = client.put(
        f"/api/notes/Tasky.md",
        json={
            "content": created["content"] + "\nB's line.\n",
            "base_revision": base,
            "on_conflict": "copy",
        },
    ).json()
    assert b["status"] == "conflict_copy"
    assert b["conflict_path"]

    copy = client.get(f"/api/notes/{b['conflict_path']}")
    assert copy.status_code == 200
    import re

    def task_ids(text: str) -> set[str]:
        return set(re.findall(r'"id":"([0-9a-f]+)"', text))

    original_ids = task_ids(client.get("/api/notes/Tasky.md").json()["content"])
    copy_ids = task_ids(copy.json()["content"])
    assert original_ids and copy_ids
    assert original_ids.isdisjoint(copy_ids)
