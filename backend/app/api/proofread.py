"""Read and dismiss staged proofreading suggestions.

There is deliberately no accept endpoint. Accepting a suggestion is an ordinary
edit in the editor, so it rides the existing autosave and lands in CodeMirror's
undo history like anything else typed — one write path instead of two, and
nothing special to undo.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..index.db import content_hash
from ..vault.notes import NoteError

router = APIRouter(prefix="/api", tags=["proofread"])


def _public(state, path: str) -> dict:
    record = state.proofread.get(path)
    suggestions = [s for s in record.get("suggestions") or [] if s.get("status") == "open"]
    # The revision the pass was made against, so the client can tell the user
    # when the note has moved on underneath a stale review.
    return {
        "path": path,
        "revision": record.get("revision"),
        "current_revision": _revision(state, path),
        "suggestions": suggestions,
    }


def _revision(state, path: str) -> str | None:
    try:
        return content_hash(state.store.read(path))
    except NoteError:
        return None


@router.get("/proofread/{path:path}")
async def get_proofread(request: Request, path: str):
    state = request.app.state
    state.store.resolve(path)  # reject traversal and non-markdown paths
    return _public(state, path)


@router.post("/proofread/{path:path}/dismiss/{suggestion_id}")
async def dismiss_suggestion(request: Request, path: str, suggestion_id: str):
    state = request.app.state
    state.store.resolve(path)
    if not state.proofread.dismiss(path, suggestion_id):
        raise NoteError(404, "That suggestion is no longer open")
    return _public(state, path)


@router.delete("/proofread/{path:path}")
async def clear_proofread(request: Request, path: str):
    state = request.app.state
    state.store.resolve(path)
    state.proofread.clear(path)
    return {"path": path, "revision": None, "current_revision": None, "suggestions": []}
