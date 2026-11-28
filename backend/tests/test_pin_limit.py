"""Pinned notes are capped at three — the sidebar section stays a small,
deliberate set. Enforced server-side so the note page, the sidebar menu, and
OMNIA's set_note_pin all hit the same wall."""
import asyncio

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api import notes as notes_module
from app.event_store import EventStore
from app.overseer.tool_expansion import set_note_pin
from app.vault.notes import NoteError


def make_client(state):
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.state = state
    app.include_router(notes_module.router)

    @app.exception_handler(NoteError)
    async def note_err(_request: Request, exc: NoteError):
        return JSONResponse(status_code=exc.status, content={"detail": exc.detail})

    return TestClient(app)


def test_fourth_pin_is_refused_and_unpin_frees_a_slot(state):
    client = make_client(state)
    for name in ("A", "B", "C", "D"):
        client.post("/api/notes", json={"path": f"{name}.md", "content": name})

    for name in ("A", "B", "C"):
        assert client.post(
            f"/api/notes/{name}.md/pin", json={"pinned": True}
        ).status_code == 200

    refused = client.post("/api/notes/D.md/pin", json={"pinned": True})
    assert refused.status_code == 400
    assert "unpin one first" in refused.json()["detail"]

    # Re-pinning an already-pinned note is not a new pin.
    assert client.post("/api/notes/A.md/pin", json={"pinned": True}).status_code == 200

    # Unpinning always works and frees the slot.
    assert client.post("/api/notes/A.md/pin", json={"pinned": False}).status_code == 200
    assert client.post("/api/notes/D.md/pin", json={"pinned": True}).status_code == 200


def test_omnia_pin_tool_respects_the_same_cap(acting_state):
    acting_state.timezone = "UTC"
    acting_state.events = EventStore(acting_state.settings.vault_dir)
    client = make_client(acting_state)
    for name in ("A", "B", "C", "D"):
        client.post("/api/notes", json={"path": f"{name}.md", "content": name})
        if name != "D":
            client.post(f"/api/notes/{name}.md/pin", json={"pinned": True})

    with pytest.raises(NoteError, match="unpin one first"):
        asyncio.run(set_note_pin(acting_state, "t", {"path": "D.md", "pinned": True}))

    # Unpin via the tool still works, then the freed slot is usable.
    asyncio.run(set_note_pin(acting_state, "t", {"path": "B.md", "pinned": False}))
    out = asyncio.run(set_note_pin(acting_state, "t", {"path": "D.md", "pinned": True}))
    assert out["pinned"] is True
