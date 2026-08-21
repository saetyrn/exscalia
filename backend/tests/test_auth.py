from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import auth as auth_module


def make_auth(tmp_path: Path) -> auth_module.SessionAuth:
    return auth_module.SessionAuth("alice", "correct horse", "test-secret", tmp_path)


def test_requires_both_username_and_password(tmp_path: Path):
    auth = make_auth(tmp_path)

    assert auth.required
    assert auth.verify_credentials("alice", "correct horse")
    assert not auth.verify_credentials("owner", "correct horse")
    assert not auth.verify_credentials("alice", "wrong")


def test_sessions_are_server_side_listed_and_revocable(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(auth_module, "_approximate_location", lambda _ip: "Portland, Oregon")
    auth = make_auth(tmp_path)

    token = auth.create_session(
        "203.0.113.10",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 Version/18.0 Safari/605.1.15",
    )
    session = auth.session_for_token(token)
    assert session is not None

    listed = auth.list_sessions(token)
    assert len(listed) == 1
    assert listed[0]["current"] is True
    assert listed[0]["browser"] == "Safari"
    assert listed[0]["os"] == "macOS"
    assert listed[0]["location"] == "Portland, Oregon"

    assert auth.revoke_session(listed[0]["id"])
    assert auth.session_for_token(token) is None


def test_logout_invalidates_only_that_token(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(auth_module, "_approximate_location", lambda _ip: "Test location")
    auth = make_auth(tmp_path)
    first = auth.create_session("203.0.113.10", "Firefox/130 Windows")
    second = auth.create_session("203.0.113.11", "Chrome/130 Linux")

    auth.revoke_token(first)

    assert auth.session_for_token(first) is None
    assert auth.session_for_token(second) is not None


def test_repeated_failures_trigger_temporary_lockout(tmp_path: Path):
    auth = make_auth(tmp_path)
    ip = "203.0.113.10"

    for _ in range(auth_module.MAX_FAILURES):
        auth.record_failure(ip)

    assert auth.retry_after(ip) > 0
    auth.record_success(ip)
    assert auth.retry_after(ip) == 0


def test_login_and_current_session_api(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(auth_module, "_approximate_location", lambda _ip: "Test location")
    app = FastAPI()
    app.state.auth = make_auth(tmp_path)
    app.include_router(auth_module.router)
    client = TestClient(app, base_url="https://testserver")

    assert client.get("/api/auth/sessions").status_code == 401
    assert (
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "wrong"},
        ).status_code
        == 401
    )

    response = client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "correct horse"},
    )
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert "__Host-omnia_session=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    assert "Secure" in cookie

    sessions = client.get("/api/auth/sessions").json()["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["current"] is True

    failures = client.get("/api/auth/failed-logins").json()["attempts"]
    assert len(failures) == 1
    assert failures[0]["location"] == "Test location"
    assert failures[0]["ip_address"] == "testclient"

    assert client.delete("/api/auth/failed-logins").json() == {"ok": True}
    assert client.get("/api/auth/failed-logins").json()["attempts"] == []

    revoked = client.delete(f"/api/auth/sessions/{sessions[0]['id']}")
    assert revoked.json() == {"ok": True, "current": True}
    assert client.get("/api/auth/sessions").status_code == 401
