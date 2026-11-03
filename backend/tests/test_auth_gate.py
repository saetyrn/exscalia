"""The require_auth gate, with auth ON, against the real app.

Every other full-app test boots with ``auth_password=None``, where
``SessionAuth.authenticated()`` short-circuits to True, and test_auth.py mounts
the auth router on a bare FastAPI() with no middleware at all. The gate in
main.py is the only thing between the internet and every /api route, so this
module boots main.app with a password and checks it from the outside.
"""
import asyncio
import re
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import auth as auth_module
from app.config import Settings

USERNAME = "owner"
PASSWORD = "pw"
SECRET = "TOP-SECRET"

# The only /api routes an anonymous caller may reach. Everything under
# /api/auth/ skips the gate, so any other route added there must guard itself;
# the sweep below holds it to 401 like the rest.
PUBLIC = {
    ("GET", "/api/health"),
    ("GET", "/api/auth/status"),
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/logout"),
}


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    from app import main as main_module

    root = tmp_path_factory.mktemp("auth-gate")
    # A file beside both served roots: no URL may ever return it.
    secret = root / "secret.txt"
    secret.write_text(SECRET, encoding="utf-8")
    frontend = root / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text("SPA-INDEX", encoding="utf-8")
    (frontend / "app.js").write_text("SPA-ASSET", encoding="utf-8")
    updates = root / "updates"
    updates.mkdir()
    (updates / "latest.json").write_text('{"version": "0.0.0"}', encoding="utf-8")

    settings = Settings(
        data_dir=root / "data",
        frontend_dir=frontend,
        updates_dir=updates,
        embedding_backend="none",
        autocommit_debounce=0.01,
        auth_username=USERNAME,
        auth_password=PASSWORD,
        anthropic_api_key=None,
        openai_api_key=None,
        google_api_key=None,
        backup_remote=None,
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(main_module, "get_settings", lambda: settings)
        # /updates and the SPA route read the module-level settings.
        mp.setattr(main_module, "settings", settings)
        mp.setattr(auth_module, "_approximate_location", lambda _ip: "Test location")
        # https, or the Secure __Host- session cookie is never sent back.
        with TestClient(main_module.app, base_url="https://testserver") as client:
            yield SimpleNamespace(
                app=main_module.app, client=client, secret=secret
            )


@pytest.fixture
def client(site):
    site.client.cookies.clear()
    yield site.client
    site.client.cookies.clear()


def _flatten(routes, prefix=""):
    for route in routes:
        included = getattr(route, "original_router", None)
        if included is not None:
            # FastAPI wraps each include_router() in an _IncludedRouter whose
            # .path is None; skipping it would make the sweep silently empty.
            yield from _flatten(included.routes, prefix + route.include_context.prefix)
        elif getattr(route, "path", None) is not None:
            yield prefix + route.path, route


def _fill(path: str) -> str:
    return re.sub(
        r"\{(\w+)(?::(\w+))?\}",
        lambda m: "x.md" if m.group(2) == "path" else "x",
        path,
    )


def _api_endpoints(app) -> list[tuple[str, str]]:
    return sorted(
        (method, path)
        for path, route in _flatten(app.routes)
        if path.startswith("/api/")
        for method in (getattr(route, "methods", None) or ())
    )


def _login(http, **extra):
    return http.post(
        "/api/auth/login",
        json={"username": USERNAME, "password": PASSWORD, **extra},
    )


# -- the gate -----------------------------------------------------------------


def test_every_api_route_is_401_without_a_session(site):
    endpoints = _api_endpoints(site.app)
    assert len(endpoints) > 100, "route flattening found almost nothing to probe"
    assert PUBLIC <= set(endpoints), "PUBLIC names a route that no longer exists"
    # The gate runs before routing, so an unknown path does not reveal itself.
    endpoints.append(("GET", "/api/no-such-route"))

    async def sweep() -> list[str]:
        # Not TestClient: it buffers whole bodies, so a gate that let /api/live
        # through would hang the suite on its endless stream instead of failing.
        transport = httpx.ASGITransport(app=site.app, raise_app_exceptions=False)
        leaks = []
        async with httpx.AsyncClient(
            transport=transport, base_url="https://testserver"
        ) as http:
            for method, path in endpoints:
                if (method, path) in PUBLIC:
                    continue
                try:
                    response = await asyncio.wait_for(
                        http.request(method, _fill(path)), 5
                    )
                except asyncio.TimeoutError:
                    leaks.append(f"{method} {path} -> no response")
                    continue
                if response.status_code != 401:
                    leaks.append(f"{method} {path} -> {response.status_code}")
        return leaks

    assert asyncio.run(sweep()) == []


@pytest.mark.parametrize(
    "method, path",
    [
        ("GET", "/api/auth/sessions"),
        ("DELETE", "/api/auth/sessions/0123456789abcdef01234567"),
        ("GET", "/api/auth/failed-logins"),
        ("DELETE", "/api/auth/failed-logins"),
    ],
)
def test_private_auth_routes_guard_themselves(client, method, path):
    # /api/auth/ is exempt from the gate as a whole prefix.
    response = client.request(method, path)
    assert response.status_code == 401
    assert response.json() == {"detail": "Not authenticated"}


def test_public_routes_answer_anonymously(client):
    assert client.get("/api/health").json()["ok"] is True
    status = client.get("/api/auth/status").json()
    assert (status["auth_required"], status["authenticated"]) == (True, False)
    assert client.post("/api/auth/logout").status_code == 200
    wrong = client.post(
        "/api/auth/login", json={"username": USERNAME, "password": "nope"}
    )
    assert wrong.status_code == 401
    assert wrong.json() == {"detail": "Wrong username or password"}


# -- sessions -----------------------------------------------------------------


def test_cookie_session_works_as_a_bearer_until_revoked(client):
    assert client.get("/api/notes").status_code == 401
    assert _login(client).status_code == 200
    token = client.cookies.get(auth_module.COOKIE)
    assert token
    assert client.get("/api/notes").status_code == 200
    assert client.get("/api/auth/status").json()["authenticated"] is True

    # HANDOFF 6.6: the iPad reads the session cookie and sends it as a bearer.
    client.cookies.clear()
    assert client.get("/api/notes").status_code == 401
    ipad = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/paper", headers=ipad).status_code == 200
    assert client.get("/api/notes", headers=ipad).status_code == 200
    ipad_session = next(
        s
        for s in client.get("/api/auth/sessions", headers=ipad).json()["sessions"]
        if s["current"]
    )

    # Revoke the iPad from another device, as the Settings sessions list does.
    desktop_login = _login(client, create_token=True, client="Desktop")
    client.cookies.clear()
    desktop = {"Authorization": f"Bearer {desktop_login.json()['token']}"}
    revoked = client.delete(
        f"/api/auth/sessions/{ipad_session['id']}", headers=desktop
    )
    assert revoked.json() == {"ok": True, "current": False}

    assert client.get("/api/notes", headers=ipad).status_code == 401
    assert client.get("/api/paper", headers=ipad).status_code == 401
    assert client.get("/api/notes", headers=desktop).status_code == 200


def test_a_forged_bearer_is_401(client):
    forged = {"Authorization": "Bearer not-a-real-token"}
    assert client.get("/api/notes", headers=forged).status_code == 401


def test_repeated_bad_logins_lock_the_address_out(site):
    # The lockout is per client address and lives on app.state.auth, so the
    # attacker gets its own address rather than locking out the shared client.
    # No `with`: entering would run the lifespan again and replace that state.
    attacker = TestClient(
        site.app, base_url="https://testserver", client=("203.0.113.7", 50000)
    )
    for _ in range(auth_module.MAX_FAILURES):
        wrong = attacker.post(
            "/api/auth/login", json={"username": USERNAME, "password": "nope"}
        )
        assert wrong.status_code == 401

    # Locked out even with the right password.
    locked = _login(attacker)
    assert locked.status_code == 429
    assert 0 < int(locked.headers["Retry-After"]) <= auth_module.LOCKOUT_SECONDS
    assert auth_module.COOKIE not in locked.headers.get("set-cookie", "")

    # Another address is unaffected.
    site.client.cookies.clear()
    assert _login(site.client).status_code == 200
    site.client.cookies.clear()


# -- CORS -----------------------------------------------------------------------


@pytest.mark.parametrize("origin", ["tauri://localhost", "http://tauri.localhost"])
def test_desktop_preflight_is_answered_before_the_gate(client, origin):
    preflight = client.options(
        "/api/notes",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == origin

    # CORS is outermost, so even the gate's 401 is readable by the desktop app.
    anonymous = client.get("/api/notes", headers={"Origin": origin})
    assert anonymous.status_code == 401
    assert anonymous.headers["access-control-allow-origin"] == origin


def test_other_origins_get_no_cors_grant(client):
    preflight = client.options(
        "/api/notes",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert preflight.status_code == 400
    assert "access-control-allow-origin" not in preflight.headers


# -- unauthenticated file routes --------------------------------------------------
# httpx normalizes a literal ".." out of the URL before it is sent, so the
# escapes are percent-encoded; the server decodes them back into the path.


def _escapes(secret: Path) -> list[str]:
    return [
        "%2e%2e/secret.txt",
        "%2e%2e%2fsecret.txt",
        "..%2fsecret.txt",
        quote(str(secret), safe=""),
    ]


def test_updates_never_serve_a_file_outside_the_feed(site, client):
    served = client.get("/updates/latest.json")
    assert served.status_code == 200
    assert served.json() == {"version": "0.0.0"}

    for escape in _escapes(site.secret):
        response = client.get(f"/updates/{escape}")
        assert response.status_code == 404, escape
        assert SECRET not in response.text


def test_spa_never_serves_a_file_outside_the_build(site, client):
    if not any(getattr(r, "name", None) == "spa" for r in site.app.routes):
        pytest.skip("frontend/build was absent at import, so no SPA route")

    assert client.get("/app.js").text == "SPA-ASSET"
    for escape in [*_escapes(site.secret), "%2e%2e%2f%2e%2e%2fetc/passwd"]:
        response = client.get(f"/{escape}")
        assert response.status_code in (200, 404), escape
        assert SECRET not in response.text
        if response.status_code == 200:
            assert response.text == "SPA-INDEX"


def test_auth_prefix_cannot_be_used_to_reach_other_routers(client):
    # /api/auth/ skips the gate; a dot segment after it must not route onward.
    response = client.get("/api/auth/%2e%2e/notes")
    assert response.status_code in (401, 404)
