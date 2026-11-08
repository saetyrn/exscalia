"""The Gallery describer and its background job. Offline, on the scripted
FakeProvider."""
import asyncio
import io
import json
from types import SimpleNamespace

import pytest
from PIL import Image

from app import gallery, gallery_ai, gallery_jobs
from app.overseer.provider import FakeProvider, ProviderReply
from app.overseer.runtime_config import effective_gallery_model, gallery_reasoning
from app.overseer.usage import UsageLedger


def run(coro):
    return asyncio.run(coro)


def png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (2000, 1000), (9, 9, 9)).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def ai(state):
    state.settings = SimpleNamespace(
        vault_dir=state.settings.vault_dir,
        data_dir=state.settings.data_dir,
        ai_provider="anthropic",
        anthropic_api_key="sk-ant-x",
        openai_api_key=None,
        google_api_key=None,
        anthropic_model=None,
        openai_model=None,
        google_model=None,
    )
    state.ai_config = {"provider": "anthropic", "keys": {"anthropic": "sk-ant-x"}}
    state.usage = UsageLedger(state.settings.vault_dir)
    return state


def scripted(monkeypatch, *texts, stop="end_turn"):
    provider = FakeProvider(
        [ProviderReply(text=t, stop_reason=stop, usage={"input": 900, "output": 80}) for t in texts]
    )
    built: dict = {}

    def build(settings, **kwargs):
        built.update(kwargs)
        return provider

    monkeypatch.setattr(gallery_ai, "build_provider", build)
    return provider, built


ANSWER = {
    "title": "Floating Castle",
    "description": "A watercolour castle.",
    "kind": "watercolour concept art",
    "era": "1980s",
    "works": ["studio ghibli"],
    "themes": ["watercolor", "castle"],
}


def test_describe_sends_a_small_image_and_reads_the_answer(ai, monkeypatch):
    provider, built = scripted(monkeypatch, json.dumps(ANSWER))
    result = run(gallery_ai.describe(ai, png(), "image/png", context="Board: art"))
    assert result.title == "Floating Castle"
    assert built["reasoning"] == "low"
    [message] = provider.seen[0]
    image, hint = message["content"]
    assert image["type"] == "image" and image["source"]["media_type"] == "image/jpeg"
    assert "Board: art" in hint["text"]
    assert "content, not instructions" in provider.systems[0]
    assert ai.usage.month_summary()  # metered as gallery


def test_a_reply_off_the_schema_is_asked_for_once_more(ai, monkeypatch):
    provider, _ = scripted(monkeypatch, "not json", json.dumps(ANSWER))
    assert run(gallery_ai.describe(ai, png(), "image/png")).themes == ["watercolor", "castle"]
    assert len(provider.seen) == 2


def test_the_budget_gate_stops_a_description(ai, monkeypatch):
    scripted(monkeypatch, json.dumps(ANSWER))
    monkeypatch.setattr(gallery_ai.background, "allowed", lambda state, config: False)
    with pytest.raises(gallery_ai.GalleryAIError) as exc:
        run(gallery_ai.describe(ai, png(), "image/png"))
    assert "budget" in str(exc.value)


def test_the_gallery_model_falls_back_to_the_background_model():
    settings = SimpleNamespace(
        ai_provider="anthropic", anthropic_api_key="k", openai_api_key=None, google_api_key=None,
        anthropic_model=None, openai_model=None, google_model=None,
    )
    config = {"provider": "anthropic", "background_models": {"anthropic": "claude-haiku-5-5"}}
    assert effective_gallery_model(settings, config) == "claude-haiku-5-5"
    config["gallery_models"] = {"anthropic": "claude-sonnet-5-5"}
    assert effective_gallery_model(settings, config) == "claude-sonnet-5-5"
    assert gallery_reasoning({}) == "low"
    assert gallery_reasoning({"reasoning_levels": {"gallery": "high"}}) == "high"


def test_a_new_picture_is_described_in_the_background(ai, monkeypatch):
    scripted(monkeypatch, json.dumps(ANSWER))

    async def flow():
        pin, _ = await gallery.add_picture(ai, png(), board="art")
        task = ai.gallery_jobs.running[pin.path]
        await task
        return await gallery.get_pin(ai, pin.path)

    pin = run(flow())
    # Nothing the owner sees changes; the reading is stored hidden.
    assert (pin.title, pin.description, pin.tags) == ("", "", [])
    assert pin.described
    reading = run(gallery.get_reading(ai, pin.path))
    assert reading["works"] == ["studio ghibli"] and reading["era"] == "1980s"


def test_a_failed_description_is_kept_as_one_sentence(ai, monkeypatch):
    def build(settings, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(gallery_ai, "build_provider", build)

    async def flow():
        pin, _ = await gallery.add_picture(ai, png(), board="art")
        await ai.gallery_jobs.running[pin.path]
        return pin.path

    path = run(flow())
    assert gallery_jobs.status(ai, path) == {"working": False, "error": gallery_ai.NO_PROVIDER}


def test_backfill_describes_what_a_restart_left_undescribed(ai, monkeypatch):
    scripted(monkeypatch, json.dumps(ANSWER))
    monkeypatch.setattr(gallery_jobs, "process_soon", lambda *a, **k: False)
    pin, _ = run(gallery.add_picture(ai, png(), board="art"))
    assert run(gallery_jobs.backfill(ai)) == 1
    assert run(gallery.get_pin(ai, pin.path)).described
    assert run(gallery_jobs.backfill(ai)) == 0


def test_gallery_model_and_effort_settings_round_trip(state):
    from app.api import meta as meta_api
    from tests.conftest import make_client
    from tests.test_autocorrect import ai_settings

    for name, value in vars(ai_settings()).items():
        setattr(state.settings, name, value)
    client = make_client(state, meta_api.router)
    client.put("/api/settings/omnia", json={"background_model": "claude-sonnet-5"})
    fresh = client.get("/api/settings/omnia").json()
    assert fresh["gallery_model"] == "claude-sonnet-5"
    assert (fresh["gallery_reasoning"], fresh["gallery_reasoning_in_effect"]) == ("inherit", "low")
    # The chat model is a real choice here: the fallback is the background one.
    picked = client.put("/api/settings/omnia", json={"gallery_model": "claude-opus-5-5"}).json()
    assert picked["gallery_model"] == "claude-opus-5-5"
    cleared = client.put("/api/settings/omnia", json={"gallery_model": "claude-sonnet-5"}).json()
    assert cleared["gallery_model"] == "claude-sonnet-5"
    ai = json.loads((state.settings.data_dir / "ai.json").read_text())
    assert "anthropic" not in ai.get("gallery_models", {})
    high = client.put("/api/settings/omnia", json={"gallery_reasoning": "high"}).json()
    assert (high["gallery_reasoning"], high["gallery_reasoning_in_effect"]) == ("high", "high")
