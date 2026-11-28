"""Music: signed stream links for players that cannot send the login.

An <audio> element sends cookies but no Authorization header. The web app
signs in with a cookie, so its streams just work; the desktop app signs in
with a Bearer token, so its audio element would be refused. It asks for a
token instead and puts it in the stream URL. One token covers every song's
stream and download for a day (MUSIC.md section 3): a short expiry would
break playback mid-song, since every seek is a new request on the same URL.

The key is derived from the session secret, so rotating that (which signs
everyone out) also voids every link.
"""
from __future__ import annotations

import hashlib
import hmac
import time

TTL_SECONDS = 24 * 60 * 60
PREFIXES = ("/api/music/stream/", "/api/music/download/")


def _key(settings) -> bytes:
    secret = str(getattr(settings, "session_secret", "") or "").encode()
    return hmac.new(secret, b"exscalia-music-stream", hashlib.sha256).digest()


def make_token(settings, now: float | None = None) -> dict:
    expires = int((time.time() if now is None else now) + TTL_SECONDS)
    sig = hmac.new(_key(settings), str(expires).encode(), hashlib.sha256).hexdigest()[:40]
    return {"token": f"{expires}.{sig}", "expires": expires}


def check_token(settings, token: str | None, now: float | None = None) -> bool:
    if not token or "." not in token:
        return False
    expires, _, sig = token.partition(".")
    if not expires.isdigit() or int(expires) < (time.time() if now is None else now):
        return False
    want = hmac.new(_key(settings), expires.encode(), hashlib.sha256).hexdigest()[:40]
    return hmac.compare_digest(want, sig)


def signed_request(settings, method: str, path: str, token: str | None) -> bool:
    return method in ("GET", "HEAD") and path.startswith(PREFIXES) and check_token(settings, token)
