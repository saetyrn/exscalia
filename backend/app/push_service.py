"""Web Push (VAPID) delivery — the last mile that reaches the owner off-device.

OMNIA already computes due reminders and its own nudges on a 60s server tick
(see `alerts.py` / `main.py`), but the only delivery was a foreground poll +
toast. This module lets the same alerts fan out to the browser's push service,
so a reminder lands on the lock screen even when the app is closed.

Design notes:
  * VAPID keys are auto-generated on first boot and persisted OUTSIDE the vault
    (data_dir/push_keys.json, chmod 600), mirroring how ai.json holds secrets.
    The key must never rotate — every existing subscription is bound to it.
  * `pywebpush` is synchronous (it uses `requests`), so every send goes through
    a worker thread; a dead endpoint (404/410) prunes the subscription.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

log = logging.getLogger(__name__)

KEYS_FILE = "push_keys.json"
# Keep a reminder deliverable for a while if the device is briefly offline, but
# not so long that a stale "in 15 min" lands hours late.
DEFAULT_TTL = 60 * 60


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


def load_or_create_vapid(settings) -> VapidKeys:
    """Return the VAPID keypair, generating and persisting one on first boot.

    Env vars win when both are set (an operator pinning keys); otherwise the
    keypair lives in data_dir/push_keys.json so the single owner never has to
    touch it and it survives redeploys via the data volume."""
    subject = settings.vapid_subject
    if settings.vapid_public_key and settings.vapid_private_key:
        return VapidKeys(settings.vapid_public_key, settings.vapid_private_key, subject)

    path = Path(settings.data_dir) / KEYS_FILE
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("public_key") and data.get("private_key"):
                return VapidKeys(data["public_key"], data["private_key"], subject)
        except Exception:
            log.exception("failed reading %s — regenerating", path)

    public_key, private_key = _generate_keypair()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"public_key": public_key, "private_key": private_key}, indent=2),
        encoding="utf-8",
    )
    os.chmod(path, 0o600)  # a private key lives here
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
                status = await asyncio.to_thread(send_push, info, payload, vapid)
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
