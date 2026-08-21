"""Single-owner authentication with revocable, server-side sessions.

Authentication is disabled only when no password is configured. In production,
both OMNIA_AUTH_USERNAME and OMNIA_AUTH_PASSWORD are required to sign in.
"""

import asyncio
import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request as URLRequest
from urllib.request import urlopen

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

router = APIRouter(prefix="/api/auth", tags=["auth"])

COOKIE = "__Host-omnia_session"
LEGACY_COOKIE = "omnia_session"
SESSION_LIFETIME = timedelta(days=30)
TOUCH_INTERVAL = timedelta(seconds=60)
MAX_FAILURES = 5
FAILURE_WINDOW_SECONDS = 10 * 60
LOCKOUT_SECONDS = 15 * 60
FAILED_LOGIN_RETENTION = timedelta(days=90)
MAX_FAILED_LOGIN_EVENTS = 200
LOCATION_CACHE_LIFETIME = timedelta(hours=24)
MAX_LOCATION_CACHE_ENTRIES = 500


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _client_ip(request: Request) -> str:
    """Return Uvicorn's proxy-validated client address."""
    return request.client.host if request.client else "unknown"


def _device_details(user_agent: str) -> tuple[str, str, str]:
    ua = user_agent or ""

    if "Edg/" in ua:
        browser = "Edge"
    elif "CriOS/" in ua or ("Chrome/" in ua and "Chromium/" not in ua):
        browser = "Chrome"
    elif "Firefox/" in ua or "FxiOS/" in ua:
        browser = "Firefox"
    elif "Safari/" in ua and "Chrome/" not in ua and "Chromium/" not in ua:
        browser = "Safari"
    else:
        browser = "Browser"

    if "iPad" in ua:
        os_name, device_type = "iPadOS", "Tablet"
    elif "iPhone" in ua or "iPod" in ua:
        os_name, device_type = "iOS", "Phone"
    elif "Android" in ua:
        os_name = "Android"
        device_type = "Tablet" if "Mobile" not in ua else "Phone"
    elif "Windows" in ua:
        os_name, device_type = "Windows", "Computer"
    elif "CrOS" in ua:
        os_name, device_type = "ChromeOS", "Computer"
    elif "Mac OS X" in ua or "Macintosh" in ua:
        os_name, device_type = "macOS", "Computer"
    elif "Linux" in ua:
        os_name, device_type = "Linux", "Computer"
    else:
        os_name, device_type = "Unknown system", "Device"

    return browser, os_name, device_type


def _approximate_location(ip: str) -> str:
    """Resolve a public IP once at login. Failure never prevents sign-in."""
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return "Location unavailable"
    if not address.is_global:
        return "Local network"

    try:
        url = (
            f"https://ipwho.is/{quote(ip, safe='')}?"
            "fields=success,city,region,country"
        )
        request = URLRequest(url, headers={"User-Agent": "EXSCALIA/1.0"})
        with urlopen(request, timeout=2.5) as response:
            data = json.load(response)
        if not data.get("success"):
            return "Location unavailable"
        parts: list[str] = []
        for value in (data.get("city"), data.get("region"), data.get("country")):
            cleaned = str(value or "").strip()
            if cleaned and cleaned not in parts:
                parts.append(cleaned)
        return ", ".join(parts) if parts else "Location unavailable"
    except Exception:
        return "Location unavailable"


class SessionAuth:
    def __init__(
        self,
        username: str | None,
        password: str | None,
        secret: str,
        data_dir: Path,
    ):
        self.username = (username or "owner").strip()
        self.password = password
        self.secret = secret.encode()
        self.db_path = data_dir / "auth.sqlite3"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._failures: dict[str, list[float]] = {}
        self._blocked_until: dict[str, float] = {}
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path)
        db.row_factory = sqlite3.Row
        return db

    def _init_db(self) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS auth_sessions (
                    id TEXT PRIMARY KEY,
                    token_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    ip_address TEXT NOT NULL,
                    location TEXT NOT NULL,
                    browser TEXT NOT NULL,
                    os_name TEXT NOT NULL,
                    device_type TEXT NOT NULL,
                    user_agent TEXT NOT NULL
                )
                """
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS auth_sessions_expiry "
                "ON auth_sessions(expires_at)"
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS failed_login_attempts (
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
                "CREATE INDEX IF NOT EXISTS failed_login_time "
                "ON failed_login_attempts(occurred_at)"
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS ip_location_cache (
                    ip_address TEXT PRIMARY KEY,
                    location TEXT NOT NULL,
                    looked_up_at TEXT NOT NULL
                )
                """
            )
            # Web Push subscriptions — one row per browser/device that opted in.
            # Durable owner state, so it belongs here beside sessions rather than
            # in the rebuildable index.
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS push_subscriptions (
                    endpoint   TEXT PRIMARY KEY,
                    p256dh     TEXT NOT NULL,
                    auth       TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_ok_at TEXT,
                    user_agent TEXT NOT NULL DEFAULT ''
                )
                """
            )
        self.db_path.chmod(0o600)

    # --- Web Push subscriptions -------------------------------------------
    def add_push_subscription(
        self, endpoint: str, p256dh: str, auth: str, user_agent: str = ""
    ) -> None:
        """Store (or refresh) a browser's push subscription, keyed by endpoint."""
        with self._lock, self._connect() as db:
            db.execute(
                """
                INSERT INTO push_subscriptions (endpoint, p256dh, auth, created_at, user_agent)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(endpoint) DO UPDATE SET
                    p256dh = excluded.p256dh,
                    auth = excluded.auth,
                    user_agent = excluded.user_agent
                """,
                (endpoint, p256dh, auth, _iso(_now()), user_agent),
            )

    def list_push_subscriptions(self) -> list[dict]:
        with self._lock, self._connect() as db:
            rows = db.execute(
                "SELECT endpoint, p256dh, auth, created_at, last_ok_at, user_agent "
                "FROM push_subscriptions"
            ).fetchall()
        return [dict(row) for row in rows]

    def remove_push_subscription(self, endpoint: str) -> None:
        with self._lock, self._connect() as db:
            db.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))

    def mark_push_ok(self, endpoint: str) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE push_subscriptions SET last_ok_at = ? WHERE endpoint = ?",
                (_iso(_now()), endpoint),
            )

    @property
    def required(self) -> bool:
        return bool(self.password)

    def verify_credentials(self, username: str, password: str) -> bool:
        if not self.required:
            return True
        # Evaluate both comparisons so the response does not reveal which
        # credential was wrong through a simple timing difference.
        username_ok = hmac.compare_digest(username.strip(), self.username)
        password_ok = hmac.compare_digest(password, self.password or "")
        return username_ok and password_ok

    def retry_after(self, ip: str) -> int:
        with self._lock:
            remaining = self._blocked_until.get(ip, 0) - time.monotonic()
            return max(0, int(remaining + 0.999))

    def record_failure(self, ip: str, user_agent: str = "") -> None:
        with self._lock:
            now = time.monotonic()
            recent = [
                attempt
                for attempt in self._failures.get(ip, [])
                if now - attempt < FAILURE_WINDOW_SECONDS
            ]
            recent.append(now)
            self._failures[ip] = recent
            if len(recent) >= MAX_FAILURES:
                self._blocked_until[ip] = now + LOCKOUT_SECONDS
                self._failures.pop(ip, None)
        browser, os_name, device_type = _device_details(user_agent)
        location = self._location_for_ip(ip)
        with self._lock, self._connect() as db:
            self._prune(db)
            db.execute(
                """
                INSERT INTO failed_login_attempts (
                    occurred_at, ip_address, location, browser, os_name, device_type
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    _iso(_now()),
                    ip,
                    location,
                    browser,
                    os_name,
                    device_type,
                ),
            )

    def record_success(self, ip: str) -> None:
        with self._lock:
            self._failures.pop(ip, None)
            self._blocked_until.pop(ip, None)

    def _token_hash(self, token: str) -> str:
        return hmac.new(self.secret, token.encode(), hashlib.sha256).hexdigest()

    def create_session(
        self, ip: str, user_agent: str, client_label: str | None = None
    ) -> str:
        token = secrets.token_urlsafe(32)
        session_id = secrets.token_hex(12)
        created = _now()
        browser, os_name, device_type = _device_details(user_agent)
        if client_label:
            browser = client_label[:100]
        location = self._location_for_ip(ip)
        with self._lock, self._connect() as db:
            self._prune(db)
            db.execute(
                """
                INSERT INTO auth_sessions (
                    id, token_hash, created_at, last_seen_at, expires_at,
                    ip_address, location, browser, os_name, device_type, user_agent
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    self._token_hash(token),
                    _iso(created),
                    _iso(created),
                    _iso(created + SESSION_LIFETIME),
                    ip,
                    location,
                    browser,
                    os_name,
                    device_type,
                    user_agent[:1000],
                ),
            )
        return token

    def _prune(self, db: sqlite3.Connection) -> None:
        now = _now()
        db.execute("DELETE FROM auth_sessions WHERE expires_at <= ?", (_iso(now),))
        db.execute(
            "DELETE FROM failed_login_attempts WHERE occurred_at < ?",
            (_iso(now - FAILED_LOGIN_RETENTION),),
        )
        db.execute(
            """
            DELETE FROM failed_login_attempts
            WHERE id NOT IN (
                SELECT id FROM failed_login_attempts
                ORDER BY occurred_at DESC LIMIT ?
            )
            """,
            (MAX_FAILED_LOGIN_EVENTS,),
        )
        db.execute(
            """
            DELETE FROM ip_location_cache
            WHERE ip_address NOT IN (
                SELECT ip_address FROM ip_location_cache
                ORDER BY looked_up_at DESC LIMIT ?
            )
            """,
            (MAX_LOCATION_CACHE_ENTRIES,),
        )

    def _location_for_ip(self, ip: str) -> str:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT location, looked_up_at FROM ip_location_cache WHERE ip_address = ?",
                (ip,),
            ).fetchone()
        if row:
            looked_up = datetime.fromisoformat(row["looked_up_at"])
            if _now() - looked_up < LOCATION_CACHE_LIFETIME:
                return str(row["location"])

        location = _approximate_location(ip)
        with self._lock, self._connect() as db:
            db.execute(
                """
                INSERT INTO ip_location_cache (ip_address, location, looked_up_at)
                VALUES (?, ?, ?)
                ON CONFLICT(ip_address) DO UPDATE SET
                    location = excluded.location,
                    looked_up_at = excluded.looked_up_at
                """,
                (ip, location, _iso(_now())),
            )
        return location

    def session_for_token(self, token: str | None, *, touch: bool = True) -> sqlite3.Row | None:
        if not token:
            return None
        token_hash = self._token_hash(token)
        with self._lock, self._connect() as db:
            self._prune(db)
            row = db.execute(
                "SELECT * FROM auth_sessions WHERE token_hash = ?", (token_hash,)
            ).fetchone()
            if not row:
                return None
            if touch:
                last_seen = datetime.fromisoformat(row["last_seen_at"])
                now = _now()
                if now - last_seen >= TOUCH_INTERVAL:
                    db.execute(
                        "UPDATE auth_sessions SET last_seen_at = ? WHERE id = ?",
                        (_iso(now), row["id"]),
                    )
                    row = db.execute(
                        "SELECT * FROM auth_sessions WHERE id = ?", (row["id"],)
                    ).fetchone()
            return row

    def token_from_request(self, request: Request) -> str | None:
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            return header[7:].strip() or None
        return request.cookies.get(COOKIE)

    def authenticated(self, request: Request) -> bool:
        if not self.required:
            return True
        return self.session_for_token(self.token_from_request(request)) is not None

    def list_sessions(self, current_token: str | None) -> list[dict]:
        current = self.session_for_token(current_token)
        current_id = current["id"] if current else None
        with self._lock, self._connect() as db:
            self._prune(db)
            rows = db.execute(
                "SELECT * FROM auth_sessions ORDER BY last_seen_at DESC"
            ).fetchall()
        return [
            {
                "id": row["id"],
                "current": row["id"] == current_id,
                "browser": row["browser"],
                "os": row["os_name"],
                "device_type": row["device_type"],
                "ip_address": row["ip_address"],
                "location": row["location"],
                "created_at": row["created_at"],
                "last_seen_at": row["last_seen_at"],
                "expires_at": row["expires_at"],
            }
            for row in rows
        ]

    def revoke_token(self, token: str | None) -> None:
        if not token:
            return
        with self._lock, self._connect() as db:
            db.execute(
                "DELETE FROM auth_sessions WHERE token_hash = ?",
                (self._token_hash(token),),
            )

    def revoke_session(self, session_id: str) -> bool:
        with self._lock, self._connect() as db:
            result = db.execute(
                "DELETE FROM auth_sessions WHERE id = ?", (session_id,)
            )
            return result.rowcount > 0

    def list_failed_logins(self) -> list[dict]:
        with self._lock, self._connect() as db:
            self._prune(db)
            rows = db.execute(
                """
                SELECT id, occurred_at, ip_address, location, browser, os_name, device_type
                FROM failed_login_attempts
                ORDER BY occurred_at DESC
                """
            ).fetchall()
        return [
            {
                "id": row["id"],
                "occurred_at": row["occurred_at"],
                "ip_address": row["ip_address"],
                "location": row["location"],
                "browser": row["browser"],
                "os": row["os_name"],
                "device_type": row["device_type"],
            }
            for row in rows
        ]

    def clear_failed_logins(self) -> None:
        with self._lock, self._connect() as db:
            db.execute("DELETE FROM failed_login_attempts")


class LoginBody(BaseModel):
    username: str
    password: str
    # Desktop/API clients that cannot use the __Host- cookie ask for the raw
    # token instead and send it back as an Authorization: Bearer header.
    create_token: bool = False
    client: str | None = None


def _clear_cookies(response: JSONResponse) -> None:
    response.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="strict")
    response.delete_cookie(LEGACY_COOKIE, path="/")


@router.get("/status")
async def status(request: Request):
    auth: SessionAuth = request.app.state.auth
    return {"auth_required": auth.required, "authenticated": auth.authenticated(request)}


@router.post("/login")
async def login(request: Request, body: LoginBody):
    auth: SessionAuth = request.app.state.auth
    if not auth.required:
        return {"ok": True}

    ip = _client_ip(request)
    retry_after = auth.retry_after(ip)
    if retry_after:
        return JSONResponse(
            status_code=429,
            headers={"Retry-After": str(retry_after)},
            content={"detail": "Too many attempts. Try again in a few minutes."},
        )
    if not auth.verify_credentials(body.username, body.password):
        await asyncio.to_thread(
            auth.record_failure, ip, request.headers.get("user-agent", "")
        )
        return JSONResponse(
            status_code=401, content={"detail": "Wrong username or password"}
        )

    auth.record_success(ip)
    token = await asyncio.to_thread(
        auth.create_session,
        ip,
        request.headers.get("user-agent", ""),
        body.client if body.create_token else None,
    )
    if body.create_token:
        return JSONResponse(content={"ok": True, "token": token})
    response = JSONResponse(content={"ok": True})
    response.set_cookie(
        COOKIE,
        token,
        httponly=True,
        samesite="strict",
        secure=True,
        max_age=int(SESSION_LIFETIME.total_seconds()),
        path="/",
    )
    response.delete_cookie(LEGACY_COOKIE, path="/")
    return response


@router.post("/logout")
async def logout(request: Request):
    auth: SessionAuth = request.app.state.auth
    auth.revoke_token(auth.token_from_request(request))
    response = JSONResponse(content={"ok": True})
    _clear_cookies(response)
    return response


@router.get("/sessions")
async def sessions(request: Request):
    auth: SessionAuth = request.app.state.auth
    token = auth.token_from_request(request)
    if auth.required and not auth.session_for_token(token):
        return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
    return {"sessions": auth.list_sessions(token)}


@router.delete("/sessions/{session_id}")
async def revoke_session(request: Request, session_id: str):
    auth: SessionAuth = request.app.state.auth
    token = auth.token_from_request(request)
    current = auth.session_for_token(token)
    if auth.required and not current:
        return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
    if not re.fullmatch(r"[0-9a-f]{24}", session_id):
        return JSONResponse(status_code=404, content={"detail": "Session not found"})
    revoked_current = bool(current and current["id"] == session_id)
    if not auth.revoke_session(session_id):
        return JSONResponse(status_code=404, content={"detail": "Session not found"})
    response = JSONResponse(content={"ok": True, "current": revoked_current})
    if revoked_current:
        _clear_cookies(response)
    return response


@router.get("/failed-logins")
async def failed_logins(request: Request):
    auth: SessionAuth = request.app.state.auth
    token = auth.token_from_request(request)
    if auth.required and not auth.session_for_token(token):
        return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
    return {"attempts": auth.list_failed_logins()}


@router.delete("/failed-logins")
async def clear_failed_logins(request: Request):
    auth: SessionAuth = request.app.state.auth
    token = auth.token_from_request(request)
    if auth.required and not auth.session_for_token(token):
        return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
    auth.clear_failed_logins()
    return {"ok": True}
