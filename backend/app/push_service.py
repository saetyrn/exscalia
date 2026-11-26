"""Web Push (VAPID) delivery — the last mile that reaches the owner off-device.

OMNIA already computes due reminders and its own nudges on a 60s server tick
(see `alerts.py` / `main.py`), but the only delivery was a foreground poll +
toast. This module lets the same alerts fan out to the browser's push service,
so a reminder lands on the lock screen even when the app is closed.

Design notes:
  * VAPID keys are auto-generated on first boot and persisted OUTSIDE the vault
    (data_dir/push_keys.json, chmod 600), mirroring how ai.json holds secrets.
    The key must never rotate — every existing subscription is bound to it —
    so a corrupt key file is set aside and push stays off until it is
    restored, one that cannot be read leaves push off for that boot, and a
    new key is minted only when there was none.
  * `pywebpush` is synchronous (it uses `requests`), so every send goes through
    a worker thread; a dead endpoint (404/410) prunes the subscription.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from .vault.internal import atomic_secret_json, quarantine_pending, set_aside

log = logging.getLogger(__name__)

KEYS_FILE = "push_keys.json"
# Keep a reminder deliverable for a while if the device is briefly offline, but
# not so long that a stale "in 15 min" lands hours late.
DEFAULT_TTL = 60 * 60
# pywebpush passes its timeout to requests explicitly, and its default is None:
# one half-open connection to a push service blocked the send forever, and the
# reminder ticker awaiting it with it.
SEND_TIMEOUT = 10.0
# deliver()'s own bound on each send's worker thread, in case requests' per-read
# timeout never trips (a server that trickles bytes). Past SEND_TIMEOUT so
# requests normally reports the failure first.
DELIVER_TIMEOUT = 15.0


@dataclass
class VapidKeys:
    """The single VAPID keypair, base64url-encoded.

    `public_key` is the browser's applicationServerKey; `private_key` is the raw
    VAPID private key that `pywebpush` signs with."""

    public_key: str
    private_key: str
    subject: str


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _generate_keypair() -> tuple[str, str]:
    """A fresh P-256 keypair as (public_b64url, private_b64url)."""
    key = ec.generate_private_key(ec.SECP256R1())
    public_raw = key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )  # 65 bytes, uncompressed point — the applicationServerKey shape
    private_raw = key.private_numbers().private_value.to_bytes(32, "big")
    return _b64url(public_raw), _b64url(private_raw)


def load_or_create_vapid(settings) -> VapidKeys | None:
    """Return the VAPID keypair, generating and persisting one on first boot.

    Env vars win when both are set (an operator pinning keys); otherwise the
    keypair lives in data_dir/push_keys.json so the single owner never has to
    touch it and it survives redeploys via the data volume.

    None means push is disabled: the key file is corrupt (set aside; later
    boots stay off until it is restored) or cannot be read right now
    (permissions, I/O; this boot only, the file stays put). Minting a fresh
    key over it would silently kill every subscription and destroy the only
    copy of the old key."""
    subject = settings.vapid_subject
    if settings.vapid_public_key and settings.vapid_private_key:
        return VapidKeys(settings.vapid_public_key, settings.vapid_private_key, subject)

    path = Path(settings.data_dir) / KEYS_FILE
    if path.exists():
        try:
            raw = path.read_bytes()
        except OSError as exc:
            log.error("%s cannot be read (%s); push disabled for this boot", path, exc)
            return None
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError:
            data = None
        if (
            isinstance(data, dict)
            and isinstance(data.get("public_key"), str)
            and isinstance(data.get("private_key"), str)
            and data["public_key"]
            and data["private_key"]
        ):
            return VapidKeys(data["public_key"], data["private_key"], subject)
        moved = set_aside(path, "unreadable")
        log.error(
            "%s is unreadable; set aside as %s — push disabled until the key "
            "file is restored",
            path,
            moved.name,
        )
        return None

    pending = quarantine_pending(path)
    if pending is not None:
        # A later boot after the set-aside above: still not "first boot".
        log.error(
            "%s was set aside as %s — push disabled until the key file is restored",
            path,
            pending.name,
        )
        return None

    public_key, private_key = _generate_keypair()
    # A private key lives here: 0600 from creation, never a torn file.
    atomic_secret_json(path, {"public_key": public_key, "private_key": private_key})
    log.info("generated VAPID keypair (public key %s…)", public_key[:12])
    return VapidKeys(public_key, private_key, subject)


def _reminder_body(alert: dict[str, Any]) -> str:
    if alert.get("overdue"):
        return "Overdue"
    minutes = alert.get("minutes_before")
    if minutes:
        return f"in {minutes} min"
    return "now"


def build_payload(alert: dict[str, Any]) -> dict[str, Any]:
    """Normalise an alert (reminder OR notify_user OR test) into a push payload
    the service worker can render: title, body, a deep-link url, and a tag that
    coalesces repeats."""
    source = alert.get("source")
    url = alert.get("url") or ("/omnia" if source == "omnia" else "/calendar")
    return {
        "title": alert.get("title") or "EXSCALIA",
        "body": alert.get("body") or _reminder_body(alert),
        "url": url,
        "tag": alert.get("tag") or alert.get("key"),
    }


def send_push(subscription: dict[str, Any], payload: dict[str, Any], vapid: VapidKeys) -> int:
    """Send one payload to one subscription. Returns the HTTP status (0 on a
    transport error). 404/410 mean the endpoint is gone and should be pruned.

    Imported lazily so the app still boots if the optional dep is absent."""
    from pywebpush import WebPushException, webpush

    try:
        resp = webpush(
            subscription_info=subscription,
            data=json.dumps(payload),
            vapid_private_key=vapid.private_key,
            vapid_claims={"sub": vapid.subject},
            ttl=DEFAULT_TTL,
            timeout=SEND_TIMEOUT,
        )
        return resp.status_code
    except WebPushException as exc:
        response = getattr(exc, "response", None)
        if response is not None:
            return response.status_code
        log.warning("web push failed with no response: %s", exc)
        return 0


async def deliver(state, alerts: list[dict[str, Any]]) -> dict[str, int]:
    """Fan the given alerts out to every stored subscription, off the event loop.

    Prunes subscriptions whose endpoint has expired (404/410) and refreshes
    `last_ok_at` on success. One failing endpoint never stalls the rest."""
    vapid: VapidKeys | None = getattr(state, "vapid", None)
    auth = getattr(state, "auth", None)
    if not vapid or not auth or not alerts:
        return {"sent": 0, "pruned": 0}

    subs = auth.list_push_subscriptions()
    if not subs:
        return {"sent": 0, "pruned": 0}

    payloads = [build_payload(a) for a in alerts]
    sent = 0
    pruned = 0
    for sub in subs:
        info = {
            "endpoint": sub["endpoint"],
            "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
        }
        sub_ok = False
        for payload in payloads:
            try:
                status = await asyncio.wait_for(
                    asyncio.to_thread(send_push, info, payload, vapid),
                    DELIVER_TIMEOUT,
                )
            except asyncio.TimeoutError:
                # The worker thread finishes on its own once requests times out.
                log.warning("push send timed out for one endpoint")
                continue
            except Exception:
                log.warning("push send crashed for one endpoint", exc_info=True)
                continue
            if status in (404, 410):
                auth.remove_push_subscription(sub["endpoint"])
                pruned += 1
                sub_ok = False
                break  # endpoint is gone; skip its remaining payloads
            if 200 <= status < 300:
                sent += 1
                sub_ok = True
        if sub_ok:
            auth.mark_push_ok(sub["endpoint"])
    return {"sent": sent, "pruned": pruned}
