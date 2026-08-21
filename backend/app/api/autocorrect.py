"""Autocorrect scans for the editor.

A separate router from proofread on purpose: proofreading is model-free
staged-review state, while this is its inverse — one model call per request
and zero persistence. The path is validated for traversal only; the note
file is never read or written, because the client's editor buffer is the
truth while the user is typing.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

from .. import autocorrect

router = APIRouter(prefix="/api", tags=["autocorrect"])


class AutocorrectBody(BaseModel):
    content: str


@router.post("/autocorrect/{path:path}")
async def scan_note(request: Request, path: str, body: AutocorrectBody):
    state = request.app.state
    state.store.resolve(path)  # reject traversal and non-markdown paths
    if not getattr(state, "omnia_autocorrect", False):
        return {"enabled": False, "suggestions": []}
    return {"enabled": True, "suggestions": await autocorrect.scan(state, body.content)}
