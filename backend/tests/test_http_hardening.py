"""The HTTP edge: request-body caps, the web app's CSP, the WARNING log file
that outlives a deploy, and the reference id on an unexpected 500."""
import asyncio
import base64
import hashlib
import logging
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app import main as main_module
from app.api import attachments as attachments_api
from app.api import auth as auth_module
from app.body_limit import MB, BodyLimitMiddleware
from app.config import Settings
from app.vault.notes import NoteError

LIMITS = [
    ("/api/auth/", 64 * 1024),
    ("/api/attachments", 30 * MB),
    ("/api/paper/", 64 * MB),
]


def limited(app: FastAPI) -> FastAPI:
    app.add_middleware(BodyLimitMiddleware, limits=LIMITS, default=64 * MB)
    return app


def never(*_args, **_kwargs):
    raise AssertionError("the request body reached the route")


def auth_client(tmp_path: Path, monkeypatch) -> TestClient:
    monkeypatch.setattr(auth_module, "_approximate_location", lambda _ip: "Test location")
    app = FastAPI()
    app.state.auth = auth_module.SessionAuth("alice", "correct horse", "s", tmp_path)
    app.include_router(auth_module.router)
    return TestClient(limited(app), base_url="https://testserver")


def test_oversized_login_body_is_refused_before_the_route(tmp_path: Path, monkeypatch):
    client = auth_client(tmp_path, monkeypatch)
    monkeypatch.setattr(auth_module.SessionAuth, "verify_credentials", never)
    response = client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "x" * MB},
    )
    assert response.status_code == 413
    assert response.json() == {"detail": "Request body is larger than 64 KB"}


def test_chunked_login_body_is_counted_as_it_arrives(tmp_path: Path, monkeypatch):
    """httpx sends an iterable as Transfer-Encoding: chunked, with no
    Content-Length to refuse up front: only counting the bytes catches it."""
    client = auth_client(tmp_path, monkeypatch)
    monkeypatch.setattr(auth_module.SessionAuth, "verify_credentials", never)
    response = client.post(
        "/api/auth/login",
        content=iter([b"x" * 65536] * 3),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 413
    assert response.json() == {"detail": "Request body is larger than 64 KB"}


def test_declared_oversize_never_reaches_the_app():
    """A Content-Length over the cap is answered without calling the app, so
    nothing is buffered or spooled; counting alone would read the first MBs."""
    calls = []

    async def app(scope, receive, send):
        calls.append(scope["path"])

    sent = []

    async def receive():
        raise AssertionError("the body was read")

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/attachments",
        "headers": [(b"content-length", str(30 * MB + 1).encode())],
    }
    asyncio.run(BodyLimitMiddleware(app, LIMITS, 64 * MB)(scope, receive, send))
    assert calls == []
    assert sent[0]["status"] == 413


def test_ordinary_logins_are_unaffected(tmp_path: Path, monkeypatch):
    client = auth_client(tmp_path, monkeypatch)
    wrong = client.post("/api/auth/login", json={"username": "alice", "password": "no"})
    assert wrong.status_code == 401
    right = client.post(
        "/api/auth/login", json={"username": "alice", "password": "correct horse"}
    )
    assert right.status_code == 200
    assert right.json() == {"ok": True}


def attachments_client(state) -> TestClient:
    app = FastAPI()
    app.state = state

    @app.exception_handler(NoteError)
    async def handler(_request, exc: NoteError):
        return JSONResponse(status_code=exc.status, content={"detail": exc.detail})

    app.include_router(attachments_api.router)
    return TestClient(limited(app))


def test_oversized_upload_is_refused_before_it_is_spooled(state, monkeypatch):
    monkeypatch.setattr(state.store, "resolve_attachment", never)
    client = attachments_client(state)
    big = b"\x89PNG\r\n\x1a\n" + b"0" * (31 * MB)

    declared = client.post("/api/attachments", files={"file": ("big.png", big, "image/png")})
    assert declared.status_code == 413
    assert declared.json() == {"detail": "Request body is larger than 30 MB"}

    # The same upload streamed without a Content-Length: the cap trips inside
    # the multipart parser and still answers 413, not a generic 400.
    boundary = "hardening-boundary"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="big.png"\r\n'
        "Content-Type: image/png\r\n\r\n"
    ).encode() + big + f"\r\n--{boundary}--\r\n".encode()
    chunked = client.post(
        "/api/attachments",
        content=iter([body]),
        headers={"content-type": f"multipart/form-data; boundary={boundary}"},
    )
    assert chunked.status_code == 413
    assert chunked.json() == {"detail": "Request body is larger than 30 MB"}


def test_uploads_under_the_cap_still_land(state):
    client = attachments_client(state)
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 32
    uploaded = client.post("/api/attachments", files={"file": ("ok.png", png, "image/png")})
    assert uploaded.status_code == 200, uploaded.text


def sha256_source(script: str) -> str:
    digest = hashlib.sha256(script.encode("utf-8")).digest()
    return f"'sha256-{base64.b64encode(digest).decode('ascii')}'"


THEME_BOOT = "\n\t\t\ttry { document.documentElement.dataset.theme = 'dark'; } catch {}\n\t\t"
KIT_INIT = '\n\t\t\t{ __sveltekit_abc123 = { base: "" }; }\n\t\t'


def spa_client(tmp_path: Path, monkeypatch) -> tuple[TestClient, Path]:
    build = tmp_path / "build"
    (build / "_app" / "immutable").mkdir(parents=True)
    (build / "_app" / "immutable" / "x.js").write_text("export const x = 1;\n")
    index = build / "index.html"
    index.write_text(
        "<!doctype html><html><head>"
        f"<script>{THEME_BOOT}</script>"
        '<script type="module" src="/_app/immutable/x.js"></script>'
        "</head><body><div>"
        f"<script>{KIT_INIT}</script>"
        "</div></body></html>",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        main_module, "settings", Settings(data_dir=tmp_path / "data", frontend_dir=build)
    )
    app = FastAPI()
    app.get("/{path:path}")(main_module.spa)
    return TestClient(app), index


def test_html_pages_carry_a_csp_pinning_their_inline_scripts(tmp_path: Path, monkeypatch):
    client, index = spa_client(tmp_path, monkeypatch)

    for url in ("/", "/notes/some/deep/link", "/index.html"):
        response = client.get(url)
        assert response.status_code == 200
        policy = response.headers["content-security-policy"]
        directives = {
            name: value
            for name, _, value in (part.partition(" ") for part in policy.split("; "))
        }
        assert directives["script-src"] == (
            f"'self' {sha256_source(THEME_BOOT)} {sha256_source(KIT_INIT)}"
        )
        assert directives["default-src"] == "'self'"
        assert directives["connect-src"] == "'self'"
        assert directives["object-src"] == "'none'"
        assert directives["frame-ancestors"] == "'none'"

    # Assets are not documents: no policy on them.
    asset = client.get("/_app/immutable/x.js")
    assert asset.status_code == 200
    assert "content-security-policy" not in asset.headers

    # A rebuild writes a new SvelteKit init script; the next page load must pin
    # it without a server restart, or the rebuilt app is blocked.
    rebuilt = '\n\t\t\t{ __sveltekit_rebuilt9 = { base: "" }; }\n\t\t'
    index.write_text(
        index.read_text(encoding="utf-8").replace(KIT_INIT, rebuilt), encoding="utf-8"
    )
    policy = client.get("/").headers["content-security-policy"]
    assert sha256_source(rebuilt) in policy
    assert sha256_source(KIT_INIT) not in policy


def test_unhandled_errors_answer_with_a_reference_in_the_log(caplog):
    app = FastAPI()
    app.add_exception_handler(Exception, main_module.unhandled_error_handler)

    @app.get("/boom")
    async def boom():
        raise RuntimeError("disk on fire")

    client = TestClient(app, raise_server_exceptions=False)
    with caplog.at_level(logging.ERROR, logger="app.main"):
        response = client.get("/boom")

    assert response.status_code == 500
    detail = response.json()["detail"]
    assert detail.startswith("Server error (ref ")
    ref = detail.removeprefix("Server error (ref ").removesuffix(")")
    assert len(ref) == 8
    [record] = [r for r in caplog.records if ref in r.getMessage()]
    assert "GET /boom" in record.getMessage()
    assert record.exc_info and isinstance(record.exc_info[1], RuntimeError)


def file_handlers() -> list[logging.Handler]:
    return [
        h
        for h in logging.getLogger().handlers
        if h.get_name() == main_module.LOG_FILE_HANDLER
    ]


def test_warnings_reach_a_log_file_in_the_data_volume(tmp_path: Path, monkeypatch):
    settings = Settings(
        data_dir=tmp_path / "app-data",
        frontend_dir=tmp_path / "no-frontend",
        embedding_backend="none",
        autocommit_debounce=0.01,
        auth_password=None,
        anthropic_api_key=None,
        openai_api_key=None,
        google_api_key=None,
        backup_remote=None,
    )
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    log_path = tmp_path / "app-data" / "logs" / "app.log"

    logger = logging.getLogger("app.test_http_hardening")
    logger.setLevel(logging.INFO)

    with TestClient(main_module.app) as client:
        assert len(file_handlers()) == 1
        logger.warning("reminder tick failed")
        logger.info("routine chatter")

        # The real stack: the cap answers before the route, and the desktop
        # origin still gets its CORS headers on the 413.
        refused = client.post(
            "/api/auth/login",
            content=iter([b"x" * 65536] * 3),
            headers={"content-type": "application/json", "origin": "tauri://localhost"},
        )
        assert refused.status_code == 413
        assert refused.headers["access-control-allow-origin"] == "tauri://localhost"

    written = log_path.read_text(encoding="utf-8")
    assert "WARNING app.test_http_hardening: reminder tick failed" in written
    assert "routine chatter" not in written
    # Timestamped, so the file makes sense without Docker's stamps.
    assert written[:4].isdigit()
    assert file_handlers() == []

    # A lifespan whose shutdown never ran left its handler behind; the next
    # one replaces it instead of writing every line twice.
    stale = logging.FileHandler(tmp_path / "stale.log")
    stale.set_name(main_module.LOG_FILE_HANDLER)
    logging.getLogger().addHandler(stale)
    try:
        with TestClient(main_module.app):
            [handler] = file_handlers()
            assert handler is not stale
            assert Path(handler.baseFilename) == log_path
    finally:
        logging.getLogger().removeHandler(stale)
        stale.close()
    assert file_handlers() == []


@pytest.fixture(autouse=True)
def _no_leaked_file_handlers():
    yield
    for handler in file_handlers():
        main_module.detach_log_file(handler)
