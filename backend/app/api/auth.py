"""Single-owner authentication with revocable, server-side sessions.

Authentication is disabled only when no password is configured. In production,
both OMNIA_AUTH_USERNAME and OMNIA_AUTH_PASSWORD are required to sign in.
"""

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import secrets
import sqlite3
import threading
import time
from collections import OrderedDict, deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request as URLRequest
from urllib.request import urlopen

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..config import get_settings

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

COOKIE = "__Host-omnia_session"
LEGACY_COOKIE = "omnia_session"
SESSION_LIFETIME = timedelta(days=30)
TOUCH_INTERVAL = timedelta(seconds=60)
MAX_FAILURES = 5
FAILURE_WINDOW_SECONDS = 10 * 60
LOCKOUT_SECONDS = 15 * 60
# Past this many failures in FAILURE_WINDOW_SECONDS from all sources combined,
# every login answer is slowed down (never refused).
GLOBAL_FAILURE_BUDGET = 30
SLOW_LOGIN_SECONDS = 2.0
FAILED_LOGIN_RETENTION = timedelta(days=90)
MAX_FAILED_LOGIN_EVENTS = 200
LOCATION_CACHE_LIFETIME = timedelta(hours=24)
MAX_LOCATION_CACHE_ENTRIES = 500
# Failed logins are geolocated after the fact, a few addresses at a time.
PENDING_LOCATION = "Location pending"
LOCATION_BATCH = 5
# Client builds that have called /status since boot, logged once each.
MAX_CLIENTS_SEEN = 100
_CLIENTS_SEEN: set[str] = set()
_CLIENT_LABEL_JUNK = re.compile(r"[^A-Za-z0-9._/+-]")


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _client_ip(request: Request) -> str:
    """Return Uvicorn's proxy-validated client address."""
    return request.client.host if request.client else "unknown"


def _throttle_key(ip: str) -> str:
    """Bucket for the login throttle: one IPv4 address or one IPv6 /64.

    An IPv6 host usually holds a whole /64, so counting per address would let
    it rotate past the lockout. Anything unparsable is its own bucket.
    """
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped:
            return str(address.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(address)


def _credential_bytes(value: str) -> bytes:
    # compare_digest raises on non-ASCII str; surrogatepass keeps a lone
    # surrogate from a JSON body comparable instead of a 500.
    return value.encode("utf-8", "surrogatepass")


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
        # Throttle state per _throttle_key, kept in last-update order so stale
        # entries sit at the front and are swept cheaply.
        self._failures: OrderedDict[str, list[float]] = OrderedDict()
        self._blocked_until: OrderedDict[str, float] = OrderedDict()
        # Newest failures from all sources; one more than the budget is enough
        # to tell whether the budget is exceeded.
        self._recent_failures: deque[float] = deque(maxlen=GLOBAL_FAILURE_BUDGET + 1)
        self._location_task: asyncio.Task | None = None
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
            # The throttle bucket, so a lockout outlives a restart. Rows from
            # before the column existed stay NULL and never count.
            columns = {
                row["name"]
                for row in db.execute("PRAGMA table_info(failed_login_attempts)")
            }
            if "throttle_key" not in columns:
                db.execute(
                    "ALTER TABLE failed_login_attempts ADD COLUMN throttle_key TEXT"
                )
            db.execute(
                "CREATE INDEX IF NOT EXISTS failed_login_throttle "
                "ON failed_login_attempts(throttle_key, occurred_at)"
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
            # Sessions belong to the credentials they were issued under, so
            # changing the password signs every device out.
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS auth_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            fingerprint = hmac.new(
                self.secret,
                _credential_bytes(f"{self.username}:{self.password or ''}"),
                hashlib.sha256,
            ).hexdigest()
            stored = db.execute(
                "SELECT value FROM auth_meta WHERE key = 'credentials'"
            ).fetchone()
            if stored is None:
                # Nothing to compare against yet (first boot with this table):
                # keep the sessions rather than sign everyone out.
                db.execute(
                    "INSERT INTO auth_meta (key, value) VALUES ('credentials', ?)",
                    (fingerprint,),
                )
            elif not hmac.compare_digest(stored["value"], fingerprint):
                revoked = db.execute("DELETE FROM auth_sessions").rowcount
                db.execute(
                    "UPDATE auth_meta SET value = ? WHERE key = 'credentials'",
                    (fingerprint,),
                )
                log.info("credentials changed; signed out %d session(s)", revoked)
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
        username_ok = hmac.compare_digest(
            _credential_bytes(username.strip()), _credential_bytes(self.username)
        )
        password_ok = hmac.compare_digest(
            _credential_bytes(password), _credential_bytes(self.password or "")
        )
        return username_ok and password_ok

    def retry_after(self, ip: str) -> int:
        key = _throttle_key(ip)
        with self._lock:
            remaining = self._blocked_until.get(key, 0) - time.monotonic()
            if remaining <= 0:
                remaining = self._persisted_lockout(key)
            return max(0, int(remaining + 0.999))

    def _persisted_lockout(self, key: str) -> float:
        """Seconds left on a lockout recorded in the failed-login log.

        A locked-out bucket logs no further failures, so its newest row is the
        one that tripped the lockout. Because LOCKOUT_SECONDS exceeds the
        window, rows from before an earlier lockout never count again.
        """
        with self._lock, self._connect() as db:
            latest = db.execute(
                "SELECT MAX(occurred_at) FROM failed_login_attempts "
                "WHERE throttle_key = ?",
                (key,),
            ).fetchone()[0]
            if latest is None:
                return 0.0
            tripped = datetime.fromisoformat(latest)
            remaining = (
                tripped + timedelta(seconds=LOCKOUT_SECONDS) - _now()
            ).total_seconds()
            if remaining <= 0:
                return 0.0
            count = db.execute(
                "SELECT COUNT(*) FROM failed_login_attempts "
                "WHERE throttle_key = ? AND occurred_at > ?",
                (key, _iso(tripped - timedelta(seconds=FAILURE_WINDOW_SECONDS))),
            ).fetchone()[0]
        return remaining if count >= MAX_FAILURES else 0.0

    def count_failure(self, ip: str) -> None:
        """Bump the in-memory throttle for a failed attempt.

        Synchronous on purpose: the login route calls it before its first
        await, so a concurrent burst cannot get more than MAX_FAILURES guesses
        past retry_after.
        """
        key = _throttle_key(ip)
        with self._lock:
            now = time.monotonic()
            self._sweep(now)
            self._recent_failures.append(now)
            recent = [
                attempt
                for attempt in self._failures.pop(key, [])
                if now - attempt < FAILURE_WINDOW_SECONDS
            ]
            recent.append(now)
            if len(recent) >= MAX_FAILURES:
                self._blocked_until.pop(key, None)
                self._blocked_until[key] = now + LOCKOUT_SECONDS
            else:
                self._failures[key] = recent

    def _sweep(self, now: float) -> None:
        # Both maps are in last-update order (every write re-inserts its key),
        # so the stale entries are exactly the ones at the front.
        while self._failures:
            attempts = next(iter(self._failures.values()))
            if now - attempts[-1] < FAILURE_WINDOW_SECONDS:
                break
            self._failures.popitem(last=False)
        while self._blocked_until:
            if next(iter(self._blocked_until.values())) > now:
                break
            self._blocked_until.popitem(last=False)

    def log_failure(self, ip: str, user_agent: str = "") -> None:
        """Add a failed attempt to the owner's log, which also persists the
        throttle. The location is filled in later by resolve_pending_locations,
        so a failure never waits on the lookup service."""
        browser, os_name, device_type = _device_details(user_agent)
        with self._lock, self._connect() as db:
            self._prune(db)
            db.execute(
                """
                INSERT INTO failed_login_attempts (
                    occurred_at, ip_address, location, browser, os_name,
                    device_type, throttle_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _iso(_now()),
                    ip,
                    PENDING_LOCATION,
                    browser,
                    os_name,
                    device_type,
                    _throttle_key(ip),
                ),
            )

    def record_failure(self, ip: str, user_agent: str = "") -> None:
        self.count_failure(ip)
        self.log_failure(ip, user_agent)

    def record_success(self, ip: str) -> None:
        key = _throttle_key(ip)
        with self._lock, self._connect() as db:
            self._failures.pop(key, None)
            self._blocked_until.pop(key, None)
            # The rows stay in the owner's log; they just stop counting.
            db.execute(
                "UPDATE failed_login_attempts SET throttle_key = NULL "
                "WHERE throttle_key = ?",
                (key,),
            )

    def global_delay(self) -> float:
        """Seconds to hold every login answer while failures from all sources
        exceed the budget. A delay rather than a lock, so a stranger sharing
        the owner's address cannot keep the owner out."""
        with self._lock:
            recent = self._recent_failures
            if (
                len(recent) > GLOBAL_FAILURE_BUDGET
                and time.monotonic() - recent[0] < FAILURE_WINDOW_SECONDS
            ):
                return SLOW_LOGIN_SECONDS
        return 0.0

    def resolve_pending_locations(self, limit: int = LOCATION_BATCH) -> int:
        """Geolocate up to `limit` addresses whose failed logins are pending.

        Returns how many addresses were resolved.
        """
        with self._lock, self._connect() as db:
            rows = db.execute(
                """
                SELECT ip_address, MAX(id) AS newest
                FROM failed_login_attempts
                WHERE location = ?
                GROUP BY ip_address
                ORDER BY newest DESC
                LIMIT ?
                """,
                (PENDING_LOCATION, limit),
            ).fetchall()
        for row in rows:
            location = self._location_for_ip(row["ip_address"])
            with self._lock, self._connect() as db:
                db.execute(
                    "UPDATE failed_login_attempts SET location = ? "
                    "WHERE ip_address = ? AND location = ?",
                    (location, row["ip_address"], PENDING_LOCATION),
                )
        return len(rows)

    def locate_failures_later(self) -> None:
        """Start a background geolocation pass unless one is running.

        Call from the event loop. One pass at a time keeps a flood of failures
        to one worker thread and one outbound lookup at once.
        """
        if self._location_task is None or self._location_task.done():
            self._location_task = asyncio.create_task(
                asyncio.to_thread(self.resolve_pending_locations)
            )

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


async def _slow_down(auth: SessionAuth) -> None:
    delay = auth.global_delay()
    if delay:
        await asyncio.sleep(delay)


def _note_client(client: str | None) -> None:
    """Log each client build ("desktop/0.1.30") the first time it calls in.

    The route is public, so the label is reduced to a version-like string and
    the set stops growing once full.
    """
    label = _CLIENT_LABEL_JUNK.sub("", client or "")[:64]
    if not label or label in _CLIENTS_SEEN or len(_CLIENTS_SEEN) >= MAX_CLIENTS_SEEN:
        return
    _CLIENTS_SEEN.add(label)
    log.info("client version seen: %s", label)


@router.get("/status")
async def status(request: Request, client: str | None = None):
    auth: SessionAuth = request.app.state.auth
    # Lifespan sets app.state.settings; bare test apps fall back to the env.
    settings = getattr(request.app.state, "settings", None) or get_settings()
    _note_client(client)
    # Old clients ignore the version keys.
    return {
        "auth_required": auth.required,
        "authenticated": auth.authenticated(request),
        "api_version": settings.api_version,
        "server_build": settings.git_sha,
        "min_client": {"desktop": settings.min_desktop_version},
    }


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
        # Counted before the first await, so concurrent guesses see the lockout.
        auth.count_failure(ip)
        await asyncio.to_thread(
            auth.log_failure, ip, request.headers.get("user-agent", "")
        )
        auth.locate_failures_later()
        await _slow_down(auth)
        return JSONResponse(
            status_code=401, content={"detail": "Wrong username or password"}
        )

    auth.record_success(ip)
    await _slow_down(auth)
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
    # Never waits on the lookup service: rows still pending show as such and
    # are filled in by the background pass for the next look.
    auth.locate_failures_later()
    return {"attempts": auth.list_failed_logins()}


@router.delete("/failed-logins")
async def clear_failed_logins(request: Request):
    auth: SessionAuth = request.app.state.auth
    token = auth.token_from_request(request)
    if auth.required and not auth.session_for_token(token):
        return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
    auth.clear_failed_logins()
    return {"ok": True}
