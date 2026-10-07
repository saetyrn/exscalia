import threading
import time
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

    # Geolocation runs in the background; settle it before reading.
    app.state.auth.resolve_pending_locations()
    failures = client.get("/api/auth/failed-logins").json()["attempts"]
    assert len(failures) == 1
    assert failures[0]["location"] == "Test location"
    assert failures[0]["ip_address"] == "testclient"

    assert client.delete("/api/auth/failed-logins").json() == {"ok": True}
    assert client.get("/api/auth/failed-logins").json()["attempts"] == []

    revoked = client.delete(f"/api/auth/sessions/{sessions[0]['id']}")
    assert revoked.json() == {"ok": True, "current": True}
    assert client.get("/api/auth/sessions").status_code == 401


def make_app(auth: auth_module.SessionAuth) -> FastAPI:
    app = FastAPI()
    app.state.auth = auth
    app.include_router(auth_module.router)
    return app


def test_non_ascii_wrong_credentials_are_a_counted_401(tmp_path: Path):
    auth = make_auth(tmp_path)
    client = TestClient(make_app(auth), base_url="https://testserver")

    assert not auth.verify_credentials("álice", "correct horse")
    assert not auth.verify_credentials("alice", "pässwörd")
    response = client.post(
        "/api/auth/login", json={"username": "alice", "password": "pässwörd"}
    )
    assert response.status_code == 401
    assert len(auth.list_failed_logins()) == 1

    # A lone surrogate survives JSON decoding; it must not become a 500 either.
    response = client.post(
        "/api/auth/login",
        content=b'{"username": "alice", "password": "\\ud800"}',
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 401
    assert len(auth.list_failed_logins()) == 2


def test_non_ascii_configured_password_signs_in(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(auth_module, "_approximate_location", lambda _ip: "Test location")
    auth = auth_module.SessionAuth("zoë", "pässwörd", "test-secret", tmp_path)
    client = TestClient(make_app(auth), base_url="https://testserver")

    response = client.post(
        "/api/auth/login", json={"username": "zoë", "password": "pässwörd"}
    )
    assert response.status_code == 200
    assert auth.list_failed_logins() == []


def test_throttle_key_buckets_ipv6_by_64():
    key = auth_module._throttle_key
    assert key("2001:db8:1:2::a") == key("2001:db8:1:2:ffff::b") == "2001:db8:1:2::/64"
    assert key("2001:db8:1:3::a") != key("2001:db8:1:2::a")
    assert key("203.0.113.10") == "203.0.113.10"
    # An IPv4-mapped address is its IPv4 address, not the shared ::/64.
    assert key("::ffff:203.0.113.10") == "203.0.113.10"
    assert key("testclient") == "testclient"


def test_ipv6_addresses_in_one_64_share_the_lockout(tmp_path: Path):
    auth = make_auth(tmp_path)
    first, second = "2001:db8:1:2::a", "2001:db8:1:2:ffff::b"

    for _ in range(3):
        auth.record_failure(first)
    for _ in range(auth_module.MAX_FAILURES - 3):
        auth.record_failure(second)

    assert auth.retry_after(first) > 0
    assert auth.retry_after(second) > 0
    assert auth.retry_after("2001:db8:1:3::a") == 0
    assert auth.retry_after("203.0.113.10") == 0


def test_lockout_survives_a_restart(tmp_path: Path):
    ip = "203.0.113.10"
    auth = make_auth(tmp_path)
    for _ in range(auth_module.MAX_FAILURES):
        auth.record_failure(ip)

    restarted = make_auth(tmp_path)
    assert restarted.retry_after(ip) > 0
    assert restarted.retry_after("203.0.113.11") == 0

    # Fewer failures than the limit do not lock anyone out after a restart.
    for _ in range(auth_module.MAX_FAILURES - 1):
        auth.record_failure("203.0.113.12")
    assert make_auth(tmp_path).retry_after("203.0.113.12") == 0

    # A success clears the persisted counter too, but keeps the log.
    restarted.record_success(ip)
    assert make_auth(tmp_path).retry_after(ip) == 0
    assert len(restarted.list_failed_logins()) == 2 * auth_module.MAX_FAILURES - 1


def test_failed_login_table_migrates_in_place(tmp_path: Path):
    import sqlite3

    with sqlite3.connect(tmp_path / "auth.sqlite3") as db:
        db.execute(
            """
            CREATE TABLE failed_login_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                occurred_at TEXT NOT NULL,
                ip_address TEXT NOT NULL,
                location TEXT NOT NULL,
                browser TEXT NOT NULL,
                os_name TEXT NOT NULL,
                device_type TEXT NOT NULL
            )
            """
        )
        db.execute(
            "INSERT INTO failed_login_attempts (occurred_at, ip_address, location,"
            " browser, os_name, device_type) VALUES (?, ?, ?, ?, ?, ?)",
            (auth_module._iso(auth_module._now()), "203.0.113.10", "Old", "B", "O", "D"),
        )

    auth = make_auth(tmp_path)
    make_auth(tmp_path)  # a second boot finds the column already there

    with sqlite3.connect(tmp_path / "auth.sqlite3") as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(failed_login_attempts)")}
        indexes = {row[1] for row in db.execute("PRAGMA index_list(failed_login_attempts)")}
    assert "throttle_key" in columns
    assert "failed_login_throttle" in indexes
    assert [a["location"] for a in auth.list_failed_logins()] == ["Old"]
    for _ in range(auth_module.MAX_FAILURES - 1):
        auth.record_failure("203.0.113.10")
    assert make_auth(tmp_path).retry_after("203.0.113.10") == 0


def test_failure_is_counted_before_the_log_write(tmp_path: Path, monkeypatch):
    auth = make_auth(tmp_path)
    seen: list[int] = []
    monkeypatch.setattr(
        auth, "log_failure", lambda ip, _ua="": seen.append(auth.retry_after(ip))
    )
    client = TestClient(make_app(auth), base_url="https://testserver")

    for _ in range(auth_module.MAX_FAILURES):
        response = client.post(
            "/api/auth/login", json={"username": "alice", "password": "wrong"}
        )
        assert response.status_code == 401

    assert seen[:-1] == [0] * (auth_module.MAX_FAILURES - 1)
    assert seen[-1] > 0


def test_concurrent_burst_gets_no_more_than_the_limit(tmp_path: Path):
    import asyncio

    import httpx

    app = make_app(make_auth(tmp_path))

    async def burst() -> list[int]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="https://testserver"
        ) as client:
            responses = await asyncio.gather(
                *(
                    client.post(
                        "/api/auth/login",
                        json={"username": "alice", "password": f"guess {n}"},
                    )
                    for n in range(12)
                )
            )
        return sorted(response.status_code for response in responses)

    codes = asyncio.run(burst())
    assert codes.count(401) == auth_module.MAX_FAILURES
    assert codes.count(429) == 12 - auth_module.MAX_FAILURES


def test_throttle_memory_is_swept_across_all_keys(tmp_path: Path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(auth_module.time, "monotonic", lambda: clock[0])
    auth = make_auth(tmp_path)

    for n in range(20):
        auth.count_failure(f"203.0.113.{n}")
    for _ in range(auth_module.MAX_FAILURES):
        auth.count_failure("198.51.100.1")
    assert len(auth._failures) == 20
    assert list(auth._blocked_until) == ["198.51.100.1"]

    clock[0] += auth_module.FAILURE_WINDOW_SECONDS + 1
    auth.count_failure("192.0.2.1")
    assert list(auth._failures) == ["192.0.2.1"]
    assert list(auth._blocked_until) == ["198.51.100.1"]

    clock[0] += auth_module.LOCKOUT_SECONDS
    auth.count_failure("192.0.2.2")
    assert list(auth._failures) == ["192.0.2.2"]
    assert not auth._blocked_until


def test_global_failure_budget_slows_every_login(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(auth_module, "_approximate_location", lambda _ip: "Test location")
    monkeypatch.setattr(auth_module, "SLOW_LOGIN_SECONDS", 0.3)
    auth = make_auth(tmp_path)

    for n in range(auth_module.GLOBAL_FAILURE_BUDGET):
        auth.record_failure(f"203.0.113.{n}")
    assert auth.global_delay() == 0
    auth.record_failure("198.51.100.1")
    assert auth.global_delay() == 0.3

    # Slowed, not refused: the owner still signs in from an unblocked address.
    client = TestClient(make_app(auth), base_url="https://testserver")
    started = time.perf_counter()
    response = client.post(
        "/api/auth/login", json={"username": "alice", "password": "correct horse"}
    )
    assert response.status_code == 200
    assert time.perf_counter() - started >= 0.3


def test_failures_are_geolocated_after_the_fact(tmp_path: Path, monkeypatch):
    lookups: list[str] = []

    def locate(ip: str) -> str:
        lookups.append(ip)
        return f"Place {ip}"

    monkeypatch.setattr(auth_module, "_approximate_location", locate)
    auth = make_auth(tmp_path)

    auth.record_failure("203.0.113.10")
    auth.record_failure("203.0.113.10")
    auth.record_failure("203.0.113.11")
    assert lookups == []
    assert {a["location"] for a in auth.list_failed_logins()} == {
        auth_module.PENDING_LOCATION
    }

    assert auth.resolve_pending_locations(limit=1) == 1
    assert auth.resolve_pending_locations() == 1
    assert auth.resolve_pending_locations() == 0
    assert sorted(lookups) == ["203.0.113.10", "203.0.113.11"]
    assert sorted(a["location"] for a in auth.list_failed_logins()) == [
        "Place 203.0.113.10",
        "Place 203.0.113.10",
        "Place 203.0.113.11",
    ]


def test_failed_logins_list_does_not_wait_on_geolocation(tmp_path: Path, monkeypatch):
    """The list answered only after up to LOCATION_BATCH sequential lookups;
    now it answers at once and the lookup runs in the background."""
    started = threading.Event()
    release = threading.Event()

    def locate(ip: str) -> str:
        if ip == "203.0.113.10":
            started.set()
            release.wait(5)
        return f"Place {ip}"

    monkeypatch.setattr(auth_module, "_approximate_location", locate)
    auth = make_auth(tmp_path)
    auth.log_failure("203.0.113.10")

    with TestClient(make_app(auth), base_url="https://testserver") as client:
        assert client.post(
            "/api/auth/login", json={"username": "alice", "password": "correct horse"}
        ).status_code == 200
        attempts = client.get("/api/auth/failed-logins").json()["attempts"]
        assert [a["location"] for a in attempts] == [auth_module.PENDING_LOCATION]
        assert started.wait(5)  # the listing started a background pass
        release.set()
        for _ in range(100):
            if auth.list_failed_logins()[0]["location"] != auth_module.PENDING_LOCATION:
                break
            time.sleep(0.05)
        attempts = client.get("/api/auth/failed-logins").json()["attempts"]
        assert [a["location"] for a in attempts] == ["Place 203.0.113.10"]


def test_sessions_survive_a_restart_with_the_same_credentials(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(auth_module, "_approximate_location", lambda _ip: "Test location")
    token = make_auth(tmp_path).create_session("203.0.113.10", "Firefox/130 Windows")

    assert make_auth(tmp_path).session_for_token(token) is not None
    assert make_auth(tmp_path).session_for_token(token) is not None


def test_changing_the_password_signs_every_device_out(tmp_path: Path, monkeypatch, caplog):
    import logging

    monkeypatch.setattr(auth_module, "_approximate_location", lambda _ip: "Test location")
    auth = make_auth(tmp_path)
    first = auth.create_session("203.0.113.10", "Firefox/130 Windows")
    second = auth.create_session("203.0.113.11", "Chrome/130 Linux")

    with caplog.at_level(logging.INFO, logger=auth_module.__name__):
        changed = auth_module.SessionAuth("alice", "new horse", "test-secret", tmp_path)
    assert changed.session_for_token(first) is None
    assert changed.session_for_token(second) is None
    assert changed.list_sessions(None) == []
    assert "signed out 2 session(s)" in caplog.text

    # Sessions issued under the new password survive its next restart.
    fresh = changed.create_session("203.0.113.10", "Firefox/130 Windows")
    again = auth_module.SessionAuth("alice", "new horse", "test-secret", tmp_path)
    assert again.session_for_token(fresh) is not None

    # A new username counts as new credentials too.
    renamed = auth_module.SessionAuth("bob", "new horse", "test-secret", tmp_path)
    assert renamed.session_for_token(fresh) is None


def test_first_boot_with_the_fingerprint_keeps_existing_sessions(tmp_path: Path, monkeypatch):
    import sqlite3

    monkeypatch.setattr(auth_module, "_approximate_location", lambda _ip: "Test location")
    token = make_auth(tmp_path).create_session("203.0.113.10", "Firefox/130 Windows")
    # A database from before the fingerprint existed.
    with sqlite3.connect(tmp_path / "auth.sqlite3") as db:
        db.execute("DROP TABLE auth_meta")

    upgraded = make_auth(tmp_path)
    assert upgraded.session_for_token(token) is not None

    # From then on, a password change is noticed.
    changed = auth_module.SessionAuth("alice", "new horse", "test-secret", tmp_path)
    assert changed.session_for_token(token) is None
