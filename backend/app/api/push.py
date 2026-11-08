"""Web Push subscription management + a test send.

All routes sit under /api/, so the global require_auth middleware
(`main.py`) already gates them for the single owner — no per-route auth."""

from fastapi import APIRouter, Request
from pydantic import BaseModel

from .. import push_service

router = APIRouter(prefix="/api/push", tags=["push"])


class PushKeys(BaseModel):
    p256dh: str
    auth: str


class SubscribeBody(BaseModel):
    endpoint: str
    keys: PushKeys


class UnsubscribeBody(BaseModel):
    endpoint: str


@router.get("/public-key")
async def public_key(request: Request):
    """The applicationServerKey the browser needs to subscribe."""
    vapid = getattr(request.app.state, "vapid", None)
    return {"key": vapid.public_key if vapid else None}


@router.post("/subscribe")
async def subscribe(body: SubscribeBody, request: Request):
    request.app.state.auth.add_push_subscription(
        body.endpoint,
        body.keys.p256dh,
        body.keys.auth,
        request.headers.get("user-agent", ""),
    )
    return {"ok": True}


@router.post("/unsubscribe")
async def unsubscribe(body: UnsubscribeBody, request: Request):
    request.app.state.auth.remove_push_subscription(body.endpoint)
    return {"ok": True}


@router.post("/test")
async def test(request: Request):
    """Send a sample notification to every subscription, so the Settings UI can
    confirm the round trip works end to end."""
    result = await push_service.deliver(
        request.app.state,
        [
            {
                "title": "EXSCALIA",
                "body": "Notifications are working.",
                "url": "/omnia",
                "source": "omnia",
                "tag": "push-test",
            }
        ],
    )
    return result
