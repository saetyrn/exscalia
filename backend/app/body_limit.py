"""Request-body caps, enforced before a route ever sees the body.

Two layers, both answering 413:

``BodyLimitMiddleware`` (outermost, before CORS) caps every route family by
path prefix. FastAPI buffers a JSON body whole before validation, and Starlette
spools a multipart file part to disk before the route's own size check runs,
so without a cap an unauthenticated POST /api/auth/login could hold hundreds of
MB in RAM (the container restarts at its 2 GB limit) and an oversized upload
filled the disk before its 413. It refuses a declared Content-Length over the
cap without calling the app and counts a chunked body as it arrives. The
Caddyfile sets the same caps at the edge; this holds them for a backend
reached without Caddy.

``reject_oversized_bodies`` (inside require_auth) knows the Paper routes'
own per-part caps and refuses a declared length over them, so a caller
without a session still gets 401 first and an oversized Paper upload never
reaches Starlette's temp file. A chunked body declares no length and passes
through to the outer layer.
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .api.paper import MAX_SELECTION_IMAGE_BYTES
from .paper_store import (
    MAX_ASSET_BYTES,
    MAX_DRAWING_BYTES,
    MAX_RENDER_BYTES,
    MAX_TEXT_BYTES,
)

MB = 1024 * 1024
# Multipart framing plus the small form fields that ride along with the parts.
SLACK_BYTES = 1 * MB
# Every other Paper write is a small JSON body (a title, a page order).
MAX_JSON_BODY_BYTES = 2 * MB

PAPER_PREFIX = "/api/paper"


def limit_for(method: str, path: str) -> int | None:
    """The most a request may declare, or None when this middleware does not care.

    Computed per call rather than at import so a test can shrink the caps.
    """
    if method not in ("POST", "PUT", "PATCH"):
        return None
    if path != PAPER_PREFIX and not path.startswith(PAPER_PREFIX + "/"):
        return None
    if method == "PUT" and path.startswith("/api/paper/pages/"):
        # page.json and text.json share the text cap.
        return MAX_DRAWING_BYTES + MAX_RENDER_BYTES + 2 * MAX_TEXT_BYTES + SLACK_BYTES
    if method == "POST" and path.startswith("/api/paper/assets/"):
        return MAX_ASSET_BYTES + SLACK_BYTES
    if method == "POST" and path == "/api/paper/selection-action":
        return MAX_SELECTION_IMAGE_BYTES + SLACK_BYTES
    return MAX_JSON_BODY_BYTES


async def reject_oversized_bodies(request: Request, call_next):
    """413 on a declared length over the route's limit; never touches the body."""
    limit = limit_for(request.method, request.url.path)
    if limit is not None:
        try:
            declared = int(request.headers.get("content-length", ""))
        except ValueError:
            declared = None
        if declared is not None and declared > limit:
            return JSONResponse(
                status_code=413,
                content={"detail": f"Request is larger than {limit // MB} MB"},
            )
    return await call_next(request)


# -- the outer, prefix-keyed cap -------------------------------------------


def _describe(limit: int) -> str:
    size = f"{limit // MB} MB" if limit >= MB else f"{limit // 1024} KB"
    return f"Request body is larger than {size}"


def _refusal(limit: int) -> JSONResponse:
    return JSONResponse(status_code=413, content={"detail": _describe(limit)})


def _declared_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers", ()):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


class BodyLimitMiddleware:
    """Answer 413 when a request body exceeds its route family's cap.

    `limits` maps path prefixes to byte caps; the longest matching prefix
    wins and anything else gets `default`. A Content-Length over the cap is
    refused without calling the app. A chunked body is counted as it arrives
    and `receive` raises once it passes the cap. The app does not always
    report that as a 413: behind an @app.middleware("http") layer the error
    reaches FastAPI wrapped in an anyio ExceptionGroup, which it answers as a
    400. So once the cap trips, whatever the app sends or raises is replaced
    by the 413 here."""

    def __init__(
        self, app: ASGIApp, limits: list[tuple[str, int]], default: int
    ) -> None:
        self.app = app
        self.limits = sorted(limits, key=lambda item: len(item[0]), reverse=True)
        self.default = default

    def limit_for(self, path: str) -> int:
        for prefix, limit in self.limits:
            if path.startswith(prefix):
                return limit
        return self.default

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self.limit_for(scope["path"])
        declared = _declared_length(scope)
        if declared is not None and declared > limit:
            await _refusal(limit)(scope, receive, send)
            return

        received = 0
        exceeded = False
        started = False

        async def limited_receive() -> Message:
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    exceeded = True
                    raise HTTPException(status_code=413, detail=_describe(limit))
            return message

        async def refuse() -> None:
            nonlocal started
            if not started:
                started = True
                await _refusal(limit)(scope, receive, send)

        async def guarded_send(message: Message) -> None:
            nonlocal started
            if exceeded:
                if message["type"] == "http.response.start":
                    await refuse()
                return
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except Exception:
            if not exceeded:
                raise
            await refuse()
