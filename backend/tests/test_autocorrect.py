"""The autocorrect scan: one model call in, anchored fixes out, and every
failure mode collapsing to an empty list instead of breaking typing."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from app import autocorrect
from app.api import autocorrect as autocorrect_api
from app.overseer.provider import FakeProvider, ProviderReply
from app.overseer.runtime_config import effective_autocorrect_model
from app.overseer.usage import UsageLedger
from app.proofread import anchor
from app.vault.notes import NoteError
from tests.test_media_api import make_client

NOTE = (
    "I recieve mail every day.\n"
    "The the cat sat on a apple.\n"
    "Their going home now.\n"
)

FIXES = [
    {"category": "misspelling", "before": "recieve", "after": "receive"},
    {"category": "doubled_word", "before": "The the", "after": "The"},
    {"category": "article", "before": "a apple", "after": "an apple"},
    {"category": "wrong_word", "before": "Their", "after": "They're"},
    # Not in the note — anchoring must drop it, not invent a position.
    {"category": "misspelling", "before": "elephant", "after": "relephant"},
]


# -- reply parsing ------------------------------------------------------------


def test_reply_parsing_survives_fences():
    fenced = 'Sure:\n```json\n{"fixes": []}\n```'
    assert autocorrect._parse_reply(fenced) == {"fixes": []}
    bare = '{"fixes": [{"category": "article"}]}'
    assert autocorrect._parse_reply(bare) == {"fixes": [{"category": "article"}]}
    assert autocorrect._parse_reply("the vibes are immaculate") is None
    assert autocorrect._parse_reply("[1, 2]") is None
    assert autocorrect._parse_reply("") is None


# -- anchoring with the autocorrect categories --------------------------------


def test_anchor_accepts_the_autocorrect_categories():
    staged, rejected = anchor(NOTE, FIXES[:4], categories=autocorrect.CATEGORIES)
    assert rejected == []
    assert [s["category"] for s in staged] == [
        "misspelling", "doubled_word", "article", "wrong_word",
    ]


def test_anchor_default_categories_are_unchanged():
    staged, rejected = anchor(NOTE, FIXES[:1])
    assert staged == []
    assert "category must be one of spelling, grammar, clarity" in rejected[0]


# -- the scan -----------------------------------------------------------------


def scripted(state, monkeypatch, text: str, usage=None) -> FakeProvider:
    provider = FakeProvider(
        [ProviderReply(text=text, usage=usage or {"input": 50, "output": 12})]
    )
    monkeypatch.setattr(
        autocorrect, "_build_autocorrect_provider", lambda _state: provider
    )
    return provider


def test_scan_anchors_every_category_and_drops_the_bogus_fix(state, monkeypatch):
    state.usage = UsageLedger(state.settings.vault_dir)
    provider = scripted(
        state, monkeypatch, f"```json\n{json.dumps({'fixes': FIXES})}\n```"
    )

    fixes = asyncio.run(autocorrect.scan(state, NOTE))

    assert provider.systems == [autocorrect.SYSTEM_PROMPT]
    assert len(fixes) == 4
    by_category = {f["category"]: f for f in fixes}
    for category, before in (
        ("misspelling", "recieve"),
        ("doubled_word", "The the"),
        ("article", "a apple"),
        ("wrong_word", "Their"),
    ):
        fix = by_category[category]
        assert NOTE[fix["offset"] : fix["end"]] == before
    assert set(fixes[0]) == {"id", "category", "before", "after", "offset", "end"}
    # The call was accounted for under its own caller.
    row = state.usage.month_summary()["by_caller"]["autocorrect"]
    assert row["calls"] == 1 and row["input"] == 50


def test_scan_survives_a_lying_model(state, monkeypatch):
    scripted(state, monkeypatch, "no json for you")
    assert asyncio.run(autocorrect.scan(state, NOTE)) == []
    scripted(state, monkeypatch, '{"fixes": "not a list"}')
    assert asyncio.run(autocorrect.scan(state, NOTE)) == []


def test_scan_caps_the_batch(state, monkeypatch):
    # Zero-padded so no word is a substring of another and every fix anchors.
    content = " ".join(f"wrd{i:02d}" for i in range(autocorrect.MAX_FIXES + 10))
    fixes = [
        {"category": "misspelling", "before": f"wrd{i:02d}", "after": f"word{i:02d}"}
        for i in range(autocorrect.MAX_FIXES + 10)
    ]
    scripted(state, monkeypatch, json.dumps({"fixes": fixes}))
    assert len(asyncio.run(autocorrect.scan(state, content))) == autocorrect.MAX_FIXES


# -- the endpoint -------------------------------------------------------------


def test_endpoint_reports_disabled_without_calling_anything(state):
    state.omnia_autocorrect = False
    client = make_client(state, autocorrect_api.router)
    resp = client.post("/api/autocorrect/Notes.md", json={"content": NOTE})
    assert resp.status_code == 200
    assert resp.json() == {"enabled": False, "suggestions": []}


def test_endpoint_happy_path(state, monkeypatch):
    state.omnia_autocorrect = True
    state.usage = UsageLedger(state.settings.vault_dir)
    scripted(state, monkeypatch, json.dumps({"fixes": FIXES[:1]}))
    client = make_client(state, autocorrect_api.router)

    resp = client.post("/api/autocorrect/Notes.md", json={"content": NOTE})

    assert resp.status_code == 200
    data = resp.json()
    assert data["enabled"] is True
    assert len(data["suggestions"]) == 1
    fix = data["suggestions"][0]
    assert fix["before"] == "recieve" and fix["after"] == "receive"
    assert NOTE[fix["offset"] : fix["end"]] == "recieve"


def test_endpoint_goes_quiet_when_the_budget_is_spent(state, monkeypatch):
    state.omnia_autocorrect = True
    state.ai_config = {"monthly_token_budget": 10}
    state.usage = UsageLedger(state.settings.vault_dir)
    state.usage.record("chat", {"input": 100, "output": 50})
    provider = scripted(state, monkeypatch, json.dumps({"fixes": FIXES[:1]}))
    client = make_client(state, autocorrect_api.router)

    resp = client.post("/api/autocorrect/Notes.md", json={"content": NOTE})

    assert resp.json() == {"enabled": True, "suggestions": []}
    assert provider.seen == []  # gated before the model was consulted


def test_endpoint_without_a_provider_returns_empty(state, monkeypatch):
    state.omnia_autocorrect = True
    monkeypatch.setattr(
        autocorrect, "_build_autocorrect_provider", lambda _state: None
    )
    client = make_client(state, autocorrect_api.router)
    resp = client.post("/api/autocorrect/Notes.md", json={"content": NOTE})
    assert resp.json() == {"enabled": True, "suggestions": []}


def test_endpoint_skips_oversized_content(state, monkeypatch):
    state.omnia_autocorrect = True
    provider = scripted(state, monkeypatch, json.dumps({"fixes": []}))
    client = make_client(state, autocorrect_api.router)
    huge = "a" * (autocorrect.MAX_CONTENT + 1)
    resp = client.post("/api/autocorrect/Notes.md", json={"content": huge})
    assert resp.json() == {"enabled": True, "suggestions": []}
    assert provider.seen == []


def test_endpoint_rejects_bad_paths(state):
    state.omnia_autocorrect = True
    request = SimpleNamespace(app=SimpleNamespace(state=state))
    with pytest.raises(NoteError) as excinfo:
        asyncio.run(
            autocorrect_api.scan_note(
                request, "../evil.md", autocorrect_api.AutocorrectBody(content="x")
            )
        )
    assert excinfo.value.status == 400
    # Non-markdown paths are refused over HTTP too.
    client = make_client(state, autocorrect_api.router)
    assert client.post("/api/autocorrect/notes.txt", json={"content": "x"}).status_code == 400


# -- model selection ----------------------------------------------------------


def ai_settings() -> SimpleNamespace:
    return SimpleNamespace(
        ai_provider="anthropic",
        anthropic_api_key="sk-ant-x",
        openai_api_key=None,
        google_api_key=None,
        anthropic_model=None,
        openai_model=None,
        google_model=None,
    )


def test_autocorrect_model_falls_back_through_background_to_chat():
    settings = ai_settings()
    config = {"provider": "anthropic", "keys": {"anthropic": "sk-ant-x"}}
    chat_default = effective_autocorrect_model(settings, config)
    assert chat_default == "claude-opus-4-8"

    config["background_models"] = {"anthropic": "claude-haiku-4-5"}
    assert effective_autocorrect_model(settings, config) == "claude-haiku-4-5"

    config["autocorrect_models"] = {"anthropic": "claude-sonnet-5"}
    assert effective_autocorrect_model(settings, config) == "claude-sonnet-5"

    # An invalid override falls through the chain, not onto the floor.
    config["autocorrect_models"] = {"anthropic": "not-a-real-model"}
    assert effective_autocorrect_model(settings, config) == "claude-haiku-4-5"


def test_scan_provider_uses_the_autocorrect_reasoning_override(monkeypatch):
    captured = {}

    def build(settings, **kwargs):
        captured.update(kwargs)
        return FakeProvider([])

    monkeypatch.setattr(autocorrect, "build_provider", build)
    config = {
        "provider": "anthropic",
        "keys": {"anthropic": "sk-ant-x"},
        "reasoning": "high",
        "reasoning_levels": {"autocorrect": "low"},
    }
    state = SimpleNamespace(overseer=None, ai_config=config, settings=ai_settings())
    autocorrect._build_autocorrect_provider(state)
    assert captured["reasoning"] == "low"


# -- settings round trip ------------------------------------------------------


def test_settings_round_trip(state):
    from app.api import meta as meta_api

    for name, value in vars(ai_settings()).items():
        setattr(state.settings, name, value)
    client = make_client(state, meta_api.router)

    toggled = client.put("/api/settings/omnia", json={"autocorrect": True})
    assert toggled.status_code == 200, toggled.text
    assert toggled.json()["autocorrect"] is True
    assert state.omnia_autocorrect is True
    config = json.loads(
        (state.settings.vault_dir / ".exscalia" / "config.json").read_text()
    )
    assert config["omnia_autocorrect"] is True

    picked = client.put(
        "/api/settings/omnia", json={"autocorrect_model": "claude-haiku-4-5"}
    )
    assert picked.json()["autocorrect_model"] == "claude-haiku-4-5"
    ai = json.loads((state.settings.data_dir / "ai.json").read_text())
    assert ai["autocorrect_models"]["anthropic"] == "claude-haiku-4-5"

    # Picking the chat model clears the override, so the scan follows the
    # background model from then on.
    client.put("/api/settings/omnia", json={"background_model": "claude-sonnet-5"})
    cleared = client.put(
        "/api/settings/omnia", json={"autocorrect_model": "claude-opus-4-8"}
    )
    assert cleared.json()["autocorrect_model"] == "claude-sonnet-5"
    ai = json.loads((state.settings.data_dir / "ai.json").read_text())
    assert "anthropic" not in ai.get("autocorrect_models", {})


def test_reasoning_override_round_trip(state):
    from app.api import meta as meta_api

    for name, value in vars(ai_settings()).items():
        setattr(state.settings, name, value)
    client = make_client(state, meta_api.router)

    set_low = client.put("/api/settings/omnia", json={"background_reasoning": "low"})
    assert set_low.status_code == 200, set_low.text
    assert set_low.json()["reasoning_levels"] == {"background": "low"}
    ai = json.loads((state.settings.data_dir / "ai.json").read_text())
    assert ai["reasoning_levels"]["background"] == "low"

    assert (
        client.put(
            "/api/settings/omnia", json={"media_reasoning": "turbo"}
        ).status_code
        == 400
    )

    # "inherit" clears the override back to the chat knob.
    cleared = client.put(
        "/api/settings/omnia", json={"background_reasoning": "inherit"}
    )
    assert cleared.json()["reasoning_levels"] == {}
    ai = json.loads((state.settings.data_dir / "ai.json").read_text())
    assert "background" not in ai.get("reasoning_levels", {})
