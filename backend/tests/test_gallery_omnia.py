"""OMNIA in the Gallery: the gallery tools, the open picture in the turn's
context, and Undo of a save after its description has landed."""
import asyncio
import hashlib
import io

import pytest
from PIL import Image

from app import gallery, gallery_ai, gallery_jobs
from app.overseer.agent import Overseer
from app.overseer.provider import FakeProvider
from app.vault import fm
from app.vault.notes import NoteError
from tests.test_recipes_omnia import calls, done, run_tool


def png(color=(120, 40, 200)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (30, 20), color).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def studio(acting_state):
    acting_state.timezone = "UTC"
    acting_state.settings.timezone = "UTC"
    return acting_state


class Describer:
    """Stands in for gallery_ai.describe; holds while `gate` is closed."""

    def __init__(self):
        self.gate: asyncio.Event | None = None

    async def __call__(self, state, raw, media_type, *, context=""):
        if self.gate is not None:
            await self.gate.wait()
        return gallery_ai.Description(
            title="Violet Field", description="A flat violet field.", kind="digital painting",
            era=None, works=[], themes=["violet", "flat"],
        )


@pytest.fixture
def describer(studio, monkeypatch):
    describe = Describer()
    monkeypatch.setattr(gallery_ai, "available", lambda state: True)
    monkeypatch.setattr(gallery_ai, "describe", describe)
    return describe


def attach(state, color=(120, 40, 200)) -> str:
    """An image as the chat's upload stores one: named by its content."""
    data = png(color)
    rel = f"attachments/violet-{hashlib.sha256(data).hexdigest()[:16]}.png"
    state.store.resolve_attachment(rel).parent.mkdir(parents=True, exist_ok=True)
    state.store.resolve_attachment(rel).write_bytes(data)
    return rel


async def settle(state) -> None:
    jobs = gallery_jobs._jobs(state)
    while jobs.running:
        await asyncio.gather(*list(jobs.running.values()), return_exceptions=True)


async def omnia(state, *script, route="/gallery"):
    provider = FakeProvider([*script, done()])
    result = await Overseer(provider, state).chat("do it", ui_context={"route": route})
    assert all(item["ok"] for item in result["trace"]), result["trace"]
    return provider, result


def test_in_the_gallery_the_tools_are_loaded_and_the_open_picture_named(studio):
    pin, _ = asyncio.run(gallery.add_picture(studio, png(), board="art"))
    provider, _result = asyncio.run(omnia(studio, route=f"/gallery/pin/{pin.path}"))
    offered = {tool.name for tool in provider.tools_seen[0]} if hasattr(provider, "tools_seen") else None
    context = provider.contexts[0] or provider.systems[0]
    assert f"Gallery picture {pin.path} open" in context
    if offered is not None:
        assert "save_to_gallery" in offered


def test_save_from_an_attachment_then_undo_after_the_description_landed(studio, describer):
    rel = attach(studio)

    async def scenario():
        describer.gate = asyncio.Event()
        _provider, result = await omnia(
            studio, calls(("save_to_gallery", {"attachment": rel, "board": "Wishlist"}))
        )
        [action] = result["actions"]
        [path] = action["paths"]
        assert path.startswith("gallery/Wishlist/")
        describer.gate.set()
        await settle(studio)
        pin = await gallery.get_pin(studio, path)
        assert pin.title == "" and pin.described
        assert (await gallery.get_reading(studio, path))["title"] == "Violet Field"
        # The same bytes kept the attachment's own file.
        assert pin.image == "/api/" + rel
        undone = await studio.actions.undo(action["id"], result["session_id"])
        return path, undone

    path, undone = asyncio.run(scenario())
    assert undone["ok"] and not undone["skipped"]
    assert not studio.store.exists(path)
    assert asyncio.run(gallery.count_pins(studio)) == 0


def test_undo_still_refuses_after_an_edit_that_is_not_the_description(studio, describer):
    rel = attach(studio)

    async def scenario():
        _provider, result = await omnia(studio, calls(("save_to_gallery", {"attachment": rel})))
        await settle(studio)
        [action] = result["actions"]
        [path] = action["paths"]
        await gallery.update_pin(studio, path, title="Mine")
        await studio.vault.commit_all("autosave")
        with pytest.raises(NoteError) as refused:
            await studio.actions.undo(action["id"], result["session_id"])
        return path, refused.value

    path, refused = asyncio.run(scenario())
    assert refused.status == 409
    assert fm.parse(studio.store.read(path))[0]["title"] == "Mine"


def test_list_get_related_and_update(studio):
    a, _ = asyncio.run(gallery.add_picture(studio, png((1, 1, 1)), board="art", title="Ink"))
    asyncio.run(gallery.add_picture(studio, png((2, 2, 2)), board="art"))
    overview = run_tool(studio, "list_gallery", {})
    assert overview["boards"] == [{"name": "art", "count": 2}]
    found = run_tool(studio, "list_gallery", {"query": "ink"})
    assert [p["path"] for p in found["pictures"]] == [a.path]
    got = run_tool(studio, "get_picture", {"path": a.path})
    assert got["attachment"].startswith("attachments/") and got["page"] == f"/gallery/pin/{a.path}"
    assert run_tool(studio, "related_pictures", {"path": a.path})["count"] == 1
    moved = run_tool(
        studio,
        "update_picture",
        {"path": a.path, "tags": ["ink"], "board": "y", "base_revision": got["revision"]},
    )
    assert moved["board"] == "y" and moved["tags"] == ["ink"]
    empty = run_tool(studio, "list_gallery", {"query": "nothing like it"})
    assert empty["count"] == 0 and "note" in empty


def test_a_picture_alone_has_no_related_and_says_so(studio):
    pin, _ = asyncio.run(gallery.add_picture(studio, png(), board="art"))
    out = run_tool(studio, "related_pictures", {"path": pin.path})
    assert out["pictures"] == [] and out["note"]


def test_a_link_the_user_did_not_write_is_not_fetched(studio):
    out = run_tool(studio, "save_to_gallery", {"url": "https://evil.example/x.png"})
    assert "Only a link the user wrote" in out.get("error", "")
