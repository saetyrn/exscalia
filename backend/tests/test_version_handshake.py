"""GET /api/auth/status tells a client which API and build it is talking to and
the oldest desktop build the server still supports, and logs each client
version that calls in (gap-version-skew#2)."""

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import auth as auth_module
from app.config import Settings


def make_client(tmp_path: Path, monkeypatch, **settings) -> TestClient:
    monkeypatch.setattr(auth_module, "_CLIENTS_SEEN", set())
    app = FastAPI()
    app.state.auth = auth_module.SessionAuth("alice", "correct horse", "test-secret", tmp_path)
    app.state.settings = Settings(data_dir=tmp_path / "app-data", **settings)
    app.include_router(auth_module.router)
    return TestClient(app, base_url="https://testserver")


def seen_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "client version seen" in r.getMessage()]


def test_status_advertises_versions(tmp_path: Path, monkeypatch):
    client = make_client(tmp_path, monkeypatch, git_sha="abc1234", min_desktop_version="0.1.30")

    assert client.get("/api/auth/status").json() == {
        "auth_required": True,
        "authenticated": False,
        "api_version": 1,
        "server_build": "abc1234",
        "min_client": {"desktop": "0.1.30"},
    }


def test_status_without_app_settings_falls_back_to_env(tmp_path: Path):
    app = FastAPI()
    app.state.auth = auth_module.SessionAuth("alice", "correct horse", "test-secret", tmp_path)
    app.include_router(auth_module.router)

    body = TestClient(app, base_url="https://testserver").get("/api/auth/status").json()
    assert isinstance(body["api_version"], int)
    assert "desktop" in body["min_client"]


def test_each_client_version_is_logged_once(tmp_path: Path, monkeypatch, caplog):
    client = make_client(tmp_path, monkeypatch, git_sha="abc1234", min_desktop_version="0.1.30")

    with caplog.at_level(logging.INFO, logger=auth_module.__name__):
        client.get("/api/auth/status", params={"client": "desktop/0.1.29"})
        client.get("/api/auth/status", params={"client": "desktop/0.1.29"})
        client.get("/api/auth/status")
    assert seen_lines(caplog) == ["client version seen: desktop/0.1.29"]

    with caplog.at_level(logging.INFO, logger=auth_module.__name__):
        client.get("/api/auth/status", params={"client": "desktop/0.1.30"})
    assert seen_lines(caplog)[-1] == "client version seen: desktop/0.1.30"


def test_client_labels_are_cleaned_and_capped(tmp_path: Path, monkeypatch, caplog):
    client = make_client(tmp_path, monkeypatch)
    monkeypatch.setattr(auth_module, "MAX_CLIENTS_SEEN", 2)

    with caplog.at_level(logging.INFO, logger=auth_module.__name__):
        # A forged second log line must not survive into the log.
        client.get("/api/auth/status", params={"client": "desktop/1.0\nERROR forged"})
        client.get("/api/auth/status", params={"client": "x" * 500})
        client.get("/api/auth/status", params={"client": "desktop/2.0"})
    assert seen_lines(caplog) == [
        "client version seen: desktop/1.0ERRORforged",
        f"client version seen: {'x' * 64}",
    ]
