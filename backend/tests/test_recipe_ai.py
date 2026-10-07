"""The Recipes AI step: imports into drafts, drawings of the dish, and the
Recipes model and effort settings. Offline, on the scripted FakeProvider."""
import asyncio
import io
import json
import logging
from types import SimpleNamespace

import pytest

from app import recipe_ai
from app.overseer.provider import FakeProvider, ProviderReply, build_provider
from app.overseer.runtime_config import effective_recipes_model, recipes_reasoning
from app.overseer.usage import UsageLedger
from app.recipe_ai import RecipeAIError, SourceFile
from app.recipe_models import Ingredient, RecipeIn, Step
from tests.conftest import make_client


def ai_settings(**overrides) -> SimpleNamespace:
    values = dict(
        ai_provider="anthropic",
        anthropic_api_key="sk-ant-x",
        openai_api_key=None,
        google_api_key=None,
        anthropic_model=None,
        openai_model=None,
        google_model=None,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture
def ai_state(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    settings = ai_settings()
    settings.data_dir = tmp_path
    settings.vault_dir = vault
    return SimpleNamespace(
        settings=settings,
        ai_config={"provider": "anthropic", "keys": {"anthropic": "sk-ant-x"}},
        usage=UsageLedger(vault),
    )


def scripted(monkeypatch, *texts, stop="end_turn"):
    """Every provider recipe_ai builds is this one FakeProvider; `built`
    holds the keyword arguments of the last build."""
    provider = FakeProvider(
        [
            ProviderReply(text=t, stop_reason=stop, usage={"input": 100, "output": 20})
            for t in texts
        ]
    )
    built: dict = {}

    def build(settings, **kwargs):
        built.update(kwargs)
        return provider

    monkeypatch.setattr(recipe_ai, "build_provider", build)
    return provider, built


def ai_recipe(**overrides) -> dict:
    value = {
        "title": "Shakshuka",
        "base_servings": 2,
        "prep_minutes": 10,
        "cook_minutes": 20,
        "ingredients": [
            {"id": "olive-oil", "amount": 2, "unit": "tbsp", "item": "olive oil", "prep": "",
             "scaling": "linear", "estimated": False, "group": None},
            {"id": "eggs", "amount": 4, "unit": "", "item": "eggs", "prep": "",
             "scaling": "whole", "estimated": False, "group": None},
            {"id": "feta", "amount": 50, "unit": "g", "item": "feta", "prep": "crumbled",
             "scaling": "linear", "estimated": True, "group": "To serve"},
        ],
        "steps": [
            {"text": "Warm {olive-oil} in a pan.", "timer_seconds": 120},
            {"text": "Crack in {eggs} and cover.", "timer_seconds": None},
            {"text": "Scatter {feta} over.", "timer_seconds": 0},
        ],
        "notes": "",
        "tags": ["breakfast", "eggs"],
        "source_kind": "text",
        "missing": ["The feta amount is a guess."],
    }
    value.update(overrides)
    return value


def image_bytes(fmt="PNG") -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (200, 80, 40)).save(buf, format=fmt)
    return buf.getvalue()


PDF = b"%PDF-1.4\n%fake\n"


def prompt_of(provider, call=0) -> str:
    content = provider.seen[call][0]["content"]
    return content if isinstance(content, str) else content[-1]["text"]


# -- extract ------------------------------------------------------------------


def test_text_import_produces_the_expected_draft(ai_state, monkeypatch):
    provider, built = scripted(monkeypatch, json.dumps(ai_recipe()))

    draft = asyncio.run(
        recipe_ai.extract(ai_state, text="Shakshuka\n2 tbsp olive oil\n4 eggs\nfeta")
    )

    assert draft.method == "ai"
    recipe = draft.recipe
    assert recipe.title == "Shakshuka" and recipe.base_servings == 2
    assert [i.id for i in recipe.ingredients] == ["olive-oil", "eggs", "feta"]
    assert recipe.ingredients[2].estimated is True
    assert recipe.ingredients[2].group == "To serve"
    assert recipe.ingredients[0].group is None
    assert [s.text for s in recipe.steps][0] == "Warm {olive-oil} in a pan."
    assert [s.timer_seconds for s in recipe.steps] == [120, None, None]
    assert recipe.source.type == "text" and recipe.source.url is None
    assert draft.missing == ["The feta amount is a guess."]
    # The Recipes model at the import effort, with the model's own ceiling.
    assert built["model"] == "claude-opus-5-5"
    assert built["reasoning"] == "medium"
    assert built["max_output"] is None
    # The source rides as fenced data under the typed-text hint, and the
    # system prompt carries the rules plus the JSON schema.
    prompt = prompt_of(provider)
    assert prompt.startswith("Convert this typed or pasted recipe text.")
    assert '<data-' in prompt and 'source="text"' in prompt and "4 eggs" in prompt
    assert "id: a short lowercase slug" in provider.systems[0]
    assert '"source_kind"' in provider.systems[0]
    assert provider.tool_counts == [0]
    # Interactive, but metered all the same.
    assert ai_state.usage.month_summary()["by_caller"]["recipes"]["calls"] == 1


def test_ingredient_ids_become_unique_slugs_and_steps_follow(ai_state, monkeypatch):
    reply = ai_recipe(
        ingredients=[
            {"id": "Soy Sauce", "amount": 1, "unit": "tbsp", "item": "soy sauce", "prep": "",
             "scaling": "linear", "estimated": False, "group": ""},
            {"id": "", "amount": 2, "unit": "", "item": "Spring Onions", "prep": "sliced",
             "scaling": "whole", "estimated": False, "group": None},
            {"id": "egg", "amount": 1, "unit": "", "item": "egg", "prep": "",
             "scaling": "whole", "estimated": False, "group": None},
            {"id": "egg", "amount": 1, "unit": "", "item": "egg yolk", "prep": "",
             "scaling": "whole", "estimated": False, "group": None},
        ],
        steps=[{"text": "Whisk {Soy Sauce} with {egg}.", "timer_seconds": None}],
    )
    scripted(monkeypatch, json.dumps(reply))

    draft = asyncio.run(recipe_ai.extract(ai_state, text="soy egg"))

    assert [i.id for i in draft.recipe.ingredients] == ["soy-sauce", "spring-onions", "egg", "egg-2"]
    assert draft.recipe.steps[0].text == "Whisk {soy-sauce} with {egg}."
    assert draft.recipe.ingredients[0].group is None


def test_web_page_text_keeps_its_url_and_long_text_is_cut(ai_state, monkeypatch):
    monkeypatch.setattr(recipe_ai, "MAX_SOURCE_CHARS", 50)
    provider, _built = scripted(monkeypatch, json.dumps(ai_recipe(source_kind="webpage")))

    draft = asyncio.run(
        recipe_ai.extract(ai_state, text="x" * 80, url="https://example.com/shakshuka")
    )

    assert draft.recipe.source.type == "website"
    assert draft.recipe.source.url == "https://example.com/shakshuka"
    assert draft.missing[-1] == "The source text was longer than 50 characters and was cut."
    prompt = prompt_of(provider)
    assert prompt.startswith("This is the text of a web page.")
    assert "Source URL: https://example.com/shakshuka" in prompt
    assert "x" * 50 in prompt and "x" * 51 not in prompt


def test_a_caller_hint_replaces_the_default(ai_state, monkeypatch):
    provider, _built = scripted(monkeypatch, json.dumps(ai_recipe()))
    asyncio.run(recipe_ai.extract(ai_state, text="soup", hint="Make it a soup."))
    assert prompt_of(provider).startswith("Make it a soup.")


def test_photo_import_sends_an_image_block_and_marks_a_photo(ai_state, monkeypatch):
    provider, _built = scripted(monkeypatch, json.dumps(ai_recipe(source_kind="text")))

    draft = asyncio.run(
        recipe_ai.extract(
            ai_state, files=[SourceFile(image_bytes("PNG"), "image/png", "card.png")]
        )
    )

    assert draft.recipe.source.type == "photo"
    content = provider.seen[0][0]["content"]
    assert [b["type"] for b in content] == ["image", "text"]
    assert content[0]["source"]["media_type"] == "image/png"
    assert content[-1]["text"].startswith("Read the recipe in this photo")


def test_a_dish_photo_stays_a_dish_photo(ai_state, monkeypatch):
    scripted(monkeypatch, json.dumps(ai_recipe(source_kind="photo_of_dish")))
    draft = asyncio.run(
        recipe_ai.extract(ai_state, files=[SourceFile(image_bytes("JPEG"), "image/jpeg", "dish.jpg")])
    )
    assert draft.recipe.source.type == "dish_photo"


def test_photos_and_pdfs_are_read_as_one_recipe(ai_state, monkeypatch):
    provider, _built = scripted(monkeypatch, json.dumps(ai_recipe()))

    asyncio.run(
        recipe_ai.extract(
            ai_state,
            text="serves 2",
            files=[
                SourceFile(PDF, "application/pdf", "page1.pdf"),
                # The browser's guess is wrong; the bytes say JPEG.
                SourceFile(image_bytes("JPEG"), "image/png", "page2.jpg"),
            ],
        )
    )

    content = provider.seen[0][0]["content"]
    assert [b["type"] for b in content] == ["document", "image", "text"]
    document = content[0]
    assert document["title"] == "page1.pdf" and "name" not in document
    assert document["source"]["media_type"] == "application/pdf"
    assert content[1]["source"]["media_type"] == "image/jpeg"
    prompt = content[-1]["text"]
    assert "these files" in prompt and "pages of the same recipe" in prompt
    assert "serves 2" in prompt


def test_a_fake_pdf_is_refused(ai_state, monkeypatch):
    provider, _built = scripted(monkeypatch, json.dumps(ai_recipe()))
    with pytest.raises(RecipeAIError, match="“x.pdf” is not a PDF."):
        asyncio.run(
            recipe_ai.extract(ai_state, files=[SourceFile(image_bytes(), "application/pdf", "x.pdf")])
        )
    assert provider.seen == []


def test_files_that_are_neither_photos_nor_pdfs_are_refused(ai_state, monkeypatch):
    scripted(monkeypatch, json.dumps(ai_recipe()))
    with pytest.raises(RecipeAIError, match="is not a photo or a PDF"):
        asyncio.run(
            recipe_ai.extract(ai_state, files=[SourceFile(b"<svg/>", "image/svg+xml", "a.svg")])
        )
    with pytest.raises(RecipeAIError, match="The files were empty."):
        asyncio.run(recipe_ai.extract(ai_state, files=[SourceFile(b"", "image/png", "a.png")]))
    with pytest.raises(RecipeAIError, match="There is nothing to read."):
        asyncio.run(recipe_ai.extract(ai_state, text="  ", url="https://example.com"))


def test_upload_size_limits(ai_state, monkeypatch):
    scripted(monkeypatch, json.dumps(ai_recipe()))
    monkeypatch.setattr(recipe_ai, "MAX_TOTAL_BYTES", len(PDF) + 4)
    with pytest.raises(RecipeAIError, match="larger than 0 MB together"):
        asyncio.run(
            recipe_ai.extract(
                ai_state,
                files=[SourceFile(PDF, "application/pdf", "a.pdf"), SourceFile(PDF, "", "b.pdf")],
            )
        )
    with pytest.raises(RecipeAIError, match="at most 6 files"):
        asyncio.run(
            recipe_ai.extract(ai_state, files=[SourceFile(PDF, "", f"{n}.pdf") for n in range(7)])
        )


def test_invalid_json_is_retried_once_with_the_error(ai_state, monkeypatch):
    provider, _built = scripted(
        monkeypatch,
        "Here you go: no JSON today.",
        "```json\n" + json.dumps(ai_recipe()) + "\n```",
    )

    draft = asyncio.run(recipe_ai.extract(ai_state, text="eggs"))

    assert draft.recipe.title == "Shakshuka"
    retry = provider.seen[1]
    assert [m["role"] for m in retry] == ["user", "assistant", "user"]
    assert retry[1]["content"] == [{"type": "text", "text": "Here you go: no JSON today."}]
    assert "did not match the schema" in retry[2]["content"]
    assert ai_state.usage.month_summary()["by_caller"]["recipes"]["calls"] == 2


def test_invalid_json_twice_raises(ai_state, monkeypatch):
    wrong_unit = ai_recipe()
    wrong_unit["ingredients"][0]["unit"] = "tablespoon"
    provider, _built = scripted(monkeypatch, "not json", json.dumps(wrong_unit), "unused")

    with pytest.raises(RecipeAIError, match="did not match the recipe format"):
        asyncio.run(recipe_ai.extract(ai_state, text="eggs"))

    assert len(provider.seen) == 2
    # The second attempt was told what failed the first time.
    assert "no JSON object" in provider.seen[1][2]["content"]


def test_a_refused_or_cut_off_answer_is_not_retried(ai_state, monkeypatch):
    provider, _built = scripted(monkeypatch, "", stop="refusal")
    with pytest.raises(RecipeAIError, match="declined to read this source"):
        asyncio.run(recipe_ai.extract(ai_state, text="eggs"))
    assert len(provider.seen) == 1

    provider, _built = scripted(monkeypatch, '{"title": "Shak', stop="max_tokens")
    with pytest.raises(RecipeAIError, match="cut off"):
        asyncio.run(recipe_ai.extract(ai_state, text="eggs"))
    assert len(provider.seen) == 1


class Failing(FakeProvider):
    def __init__(self, exc):
        super().__init__([])
        self.exc = exc

    async def chat(self, *args, **kwargs):
        raise self.exc


def api_error(name, message="", **attrs):
    """An exception shaped like an SDK's: its class name and status fields."""
    exc = type(name, (Exception,), {})(message)
    for key, value in attrs.items():
        setattr(exc, key, value)
    return exc


def test_provider_failures_become_one_short_sentence(ai_state, monkeypatch):
    cases = [
        (api_error("BadRequestError", "Your credit balance is too low", status_code=400),
         "The AI account is out of credit."),
        (api_error("RateLimitError", "insufficient_quota: billing", status_code=429),
         "The AI account is out of credit."),
        (api_error("AuthenticationError", "invalid x-api-key", status_code=401),
         "The AI key was rejected."),
        (api_error("ClientError", "API key not valid", code=400), "The AI key was rejected."),
        (api_error("RateLimitError", "slow down", status_code=429),
         "The AI is rate limited. Try again in a minute."),
        (api_error("InternalServerError", "Overloaded", status_code=529),
         "The AI service is busy. Try again shortly."),
        (api_error("ServerError", "UNAVAILABLE", code=503),
         "The AI service is busy. Try again shortly."),
        (api_error("APIConnectionError", "Connection error."), "Could not reach the AI service."),
        (api_error("BadRequestError", "messages: bad", status_code=400),
         "The AI request failed (400)."),
        (RuntimeError("OpenAI stream ended without a final response"), "The AI request failed."),
    ]
    for exc, sentence in cases:
        assert recipe_ai.explain(exc) == sentence, exc

    monkeypatch.setattr(
        recipe_ai, "build_provider", lambda settings, **kw: Failing(cases[5][0])
    )
    with pytest.raises(RecipeAIError) as raised:
        asyncio.run(recipe_ai.extract(ai_state, text="eggs"))
    assert str(raised.value) == "The AI service is busy. Try again shortly."


def test_a_provider_failure_is_logged_in_one_line(ai_state, monkeypatch, caplog):
    """A rate limit or an outage is the provider's answer, not a bug here:
    one log line and no traceback. A failure nothing recognises keeps it."""
    for exc, traced in (
        (api_error("RateLimitError", "slow down", status_code=429), False),
        (api_error("AuthenticationError", "invalid x-api-key", status_code=401), False),
        (TypeError("'NoneType' object is not iterable"), True),
    ):
        monkeypatch.setattr(
            recipe_ai, "build_provider", lambda settings, exc=exc, **kw: Failing(exc)
        )
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="app.recipe_ai"):
            with pytest.raises(RecipeAIError):
                asyncio.run(recipe_ai.extract(ai_state, text="eggs"))
        [record] = [r for r in caplog.records if r.name == "app.recipe_ai"]
        assert bool(record.exc_info) is traced, exc
        assert type(exc).__name__ in record.getMessage()


def test_imports_are_not_paused_by_the_budget(ai_state, monkeypatch):
    # The user is waiting on an import, as on a chat answer.
    ai_state.ai_config["monthly_token_budget"] = 100
    ai_state.usage.record("chat", {"input": 500, "output": 500})
    scripted(monkeypatch, json.dumps(ai_recipe()))
    assert asyncio.run(recipe_ai.extract(ai_state, text="eggs")).recipe.title == "Shakshuka"


# -- no provider ----------------------------------------------------------------


def test_without_a_provider_nothing_runs(ai_state, monkeypatch):
    ai_state.settings.anthropic_api_key = None
    ai_state.ai_config = {"provider": "anthropic"}
    provider, _built = scripted(monkeypatch, json.dumps(ai_recipe()))

    assert recipe_ai.available(ai_state) is False
    with pytest.raises(RecipeAIError, match="No AI provider is configured."):
        asyncio.run(recipe_ai.extract(ai_state, text="eggs"))
    with pytest.raises(RecipeAIError, match="No AI provider is configured."):
        asyncio.run(recipe_ai.extract(ai_state, files=[SourceFile(PDF, "application/pdf", "x.pdf")]))
    with pytest.raises(RecipeAIError, match="No AI provider is configured."):
        asyncio.run(recipe_ai.draw(ai_state, RecipeIn(title="Soup")))
    assert provider.seen == []


def test_available_follows_the_configured_key(ai_state):
    assert recipe_ai.available(ai_state) is True
    ai_state.settings.anthropic_api_key = None
    ai_state.ai_config = {"provider": "openai", "keys": {"openai": "sk-x"}}
    assert recipe_ai.available(ai_state) is True


# -- clean_art (ported from the recipe app) ---------------------------------------

PAL = {"y": "#e8c26a", "g": "#7fbf6a"}


def test_clean_art_fixes_size_charset_and_aligns_colours():
    rows = ["```", "", "    ~~~~ ''\t", "    ( y )é", "    " + "x" * 80] + ["    |"] * 30 + ["```"]
    colors = ["```", "", "    yyyy .g", "    .y.y.y", "    " + "y" * 80] + ["    y"] * 30 + ["```"]
    d = recipe_ai.clean_art(rows, colors, {**PAL, "Q": "#000000", "z": "not-a-colour"})
    art, keys = d.art.split("\n"), d.art_colors.split("\n")
    assert art[0] == "~~~~ ''" and keys[0] == "yyyy..g"  # shared indent removed, tab trimmed, space uncoloured
    assert len(art) <= recipe_ai.ART_ROWS and max(map(len, art)) <= recipe_ai.ART_COLS
    assert all(len(a) == len(k) for a, k in zip(art, keys))
    assert all(32 <= ord(ch) <= 126 for line in art for ch in line)
    assert set(d.art_palette) == {"y", "g"}  # bad keys and colours dropped
    # spaces never carry a colour, unknown keys fall back to default ink
    for a, k in zip(art, keys):
        assert all(kk == "." for ch, kk in zip(a, k) if ch == " ")


def test_clean_art_without_colours_is_plain():
    d = recipe_ai.clean_art("\n      ___\n     (   )\n")
    assert d.art == " ___\n(   )" and d.art_colors == "" and d.art_palette == {}


# -- draw -----------------------------------------------------------------------

PANCAKES = RecipeIn(
    title="Pancakes",
    tags=["breakfast"],
    ingredients=[
        Ingredient(id="flour", amount=1, unit="cup", item="flour"),
        Ingredient(id="butter", amount=1, unit="tbsp", item="butter", prep="melted", group="Batter"),
    ],
    steps=[Step(text="Mix."), Step(text="Stack and pour syrup over.")],
)


def ai_art(**overrides) -> dict:
    value = {
        "palette": [
            {"key": "Y", "color": "#e8c26a ", "name": "pancake gold"},
            {"key": "b", "color": "#8a5a2b", "name": "syrup"},
        ],
        "rows": ["", "   ______", "  (======)", "  (______)", ""],
        "colors": ["", "   bbbbbb", "  yyyyyyyy", "  yyyyyyyy", ""],
    }
    value.update(overrides)
    return value


def test_draw_returns_a_clean_drawing(ai_state, monkeypatch):
    provider, built = scripted(monkeypatch, json.dumps(ai_art()))

    drawing = asyncio.run(recipe_ai.draw(ai_state, PANCAKES))

    assert drawing.art == " ______\n(======)\n(______)"
    assert drawing.art_colors == ".bbbbbb\nyyyyyyyy\nyyyyyyyy"
    assert drawing.art_palette == {"y": "#e8c26a", "b": "#8a5a2b"}
    prompt = prompt_of(provider)
    assert prompt.startswith("Dish: Pancakes\nTags: breakfast")
    assert "- butter (melted) [Batter]" in prompt
    assert "How it is finished and served: Mix. / Stack and pour syrup over." in prompt
    assert "You are an ASCII artist." in provider.systems[0]
    # Low effort by default, on the model's own output ceiling.
    assert built["reasoning"] == "low" and built["max_output"] is None
    assert ai_state.usage.month_summary()["by_caller"]["recipes"]["calls"] == 1


def test_draw_retries_once_then_reports_an_empty_drawing(ai_state, monkeypatch):
    provider, _built = scripted(
        monkeypatch, '{"palette": []}', json.dumps(ai_art(rows=["", "   "], colors=[]))
    )
    with pytest.raises(RecipeAIError, match="The drawing came back empty."):
        asyncio.run(recipe_ai.draw(ai_state, PANCAKES))
    assert len(provider.seen) == 2


def test_draw_refuses_when_the_budget_gate_says_no(ai_state, monkeypatch):
    ai_state.ai_config["monthly_token_budget"] = 100
    ai_state.usage.record("chat", {"input": 500, "output": 500})
    provider, built = scripted(monkeypatch, json.dumps(ai_art()))

    with pytest.raises(RecipeAIError, match="budget has paused drawings"):
        asyncio.run(recipe_ai.draw(ai_state, PANCAKES))

    assert provider.seen == [] and built == {}
    assert "recipes" not in ai_state.usage.month_summary()["by_caller"]


def test_a_cut_off_drawing_says_so(ai_state, monkeypatch):
    scripted(monkeypatch, '{"palette": [', stop="max_tokens")
    with pytest.raises(RecipeAIError, match="ran out of room"):
        asyncio.run(recipe_ai.draw(ai_state, PANCAKES))


def test_higher_effort_drawings_get_the_whole_output_ceiling(ai_state, monkeypatch):
    ai_state.ai_config["reasoning_levels"] = {"recipes": "max"}
    _provider, built = scripted(monkeypatch, json.dumps(ai_art()))
    asyncio.run(recipe_ai.draw(ai_state, PANCAKES))
    assert built["reasoning"] == "max" and built["max_output"] == 128000

    ceiling = recipe_ai.art_output_ceiling
    assert ceiling("anthropic", "claude-opus-5-5", "low") is None
    assert ceiling("anthropic", "claude-sonnet-5-5", "medium") == 128000
    # "default" lets the provider pick, and Opus 5.5's own default is medium.
    assert ceiling("anthropic", "claude-opus-5-5", "default") == 128000
    assert ceiling("anthropic", "claude-haiku-4-5", "max") == 64000
    assert ceiling("openai", "gpt-6.1-sol", "high") == 64000
    assert ceiling("google", "gemini-3.6-flash", "max") is None

    # Imports keep the model's own ceiling at any effort.
    _provider, built = scripted(monkeypatch, json.dumps(ai_recipe()))
    asyncio.run(recipe_ai.extract(ai_state, text="eggs"))
    assert built["reasoning"] == "max" and built["max_output"] is None


# -- the Recipes model and effort -------------------------------------------------


def test_recipes_model_falls_back_to_the_chat_model():
    settings = ai_settings()
    config = {"provider": "anthropic", "keys": {"anthropic": "sk-ant-x"}}
    assert effective_recipes_model(settings, config) == "claude-opus-5-5"
    config["recipes_models"] = {"anthropic": "claude-sonnet-5-5"}
    assert effective_recipes_model(settings, config) == "claude-sonnet-5-5"
    config["recipes_models"] = {"anthropic": "not-a-real-model"}
    assert effective_recipes_model(settings, config) == "claude-opus-5-5"
    # Per provider: the Anthropic pick says nothing about OpenAI.
    config.update(provider="openai", recipes_models={"anthropic": "claude-sonnet-5-5"})
    assert effective_recipes_model(ai_settings(openai_api_key="sk-x"), config) == "gpt-6.1-sol"


def test_recipes_effort_is_medium_for_imports_and_low_for_drawings_unless_set():
    # Unlike the other roles it does not inherit the chat knob.
    for config in ({}, {"reasoning": "max"}, {"reasoning_levels": {"recipes": "turbo"}}):
        assert recipes_reasoning(config, "import") == "medium"
        assert recipes_reasoning(config, "drawing") == "low"
    # A set level applies to both.
    for level in ("default", "low", "high"):
        config = {"reasoning_levels": {"recipes": level}}
        assert recipes_reasoning(config, "import") == level
        assert recipes_reasoning(config, "drawing") == level


def test_unset_effort_reads_imports_at_medium_and_draws_at_low(ai_state, monkeypatch):
    _provider, built = scripted(monkeypatch, json.dumps(ai_recipe()), json.dumps(ai_art()))
    asyncio.run(recipe_ai.extract(ai_state, text="eggs"))
    assert built["reasoning"] == "medium" and built["max_output"] is None
    asyncio.run(recipe_ai.draw(ai_state, PANCAKES))
    assert built["reasoning"] == "low" and built["max_output"] is None

    ai_state.ai_config["reasoning_levels"] = {"recipes": "low"}
    _provider, built = scripted(monkeypatch, json.dumps(ai_recipe()))
    asyncio.run(recipe_ai.extract(ai_state, text="eggs"))
    assert built["reasoning"] == "low"


def test_errors_carry_the_status_the_routes_answer(ai_state, monkeypatch):
    scripted(monkeypatch, json.dumps(ai_recipe()))
    cases = [
        ([SourceFile(image_bytes(), "application/pdf", "x.pdf")], 400),
        ([SourceFile(b"<svg/>", "image/svg+xml", "a.svg")], 400),
        ([SourceFile(b"", "image/png", "a.png")], 400),
        ([SourceFile(PDF, "", f"{n}.pdf") for n in range(7)], 400),
    ]
    for files, status in cases:
        with pytest.raises(RecipeAIError) as raised:
            asyncio.run(recipe_ai.extract(ai_state, files=files))
        assert raised.value.status == status, raised.value
    monkeypatch.setattr(recipe_ai, "MAX_IMAGE_BYTES", 10)
    with pytest.raises(RecipeAIError) as raised:
        asyncio.run(recipe_ai.extract(ai_state, files=[SourceFile(image_bytes(), "", "big.png")]))
    assert raised.value.status == 413

    monkeypatch.setattr(
        recipe_ai, "build_provider", lambda settings, **kw: Failing(api_error("RateLimitError", status_code=429))
    )
    with pytest.raises(RecipeAIError) as raised:
        asyncio.run(recipe_ai.extract(ai_state, text="eggs"))
    assert raised.value.status == 502

    ai_state.settings.anthropic_api_key = None
    ai_state.ai_config = {"provider": "anthropic"}
    with pytest.raises(RecipeAIError) as raised:
        asyncio.run(recipe_ai.extract(ai_state, text="eggs"))
    assert raised.value.status == 503


def test_recipes_settings_round_trip(state):
    from app.api import meta as meta_api

    for name, value in vars(ai_settings()).items():
        setattr(state.settings, name, value)
    client = make_client(state, meta_api.router)

    before = client.get("/api/settings/omnia").json()
    assert before["recipes_model"] == "claude-opus-5-5"
    assert before["recipes_reasoning"] == "inherit"
    assert before["recipes_reasoning_in_effect"] == {"import": "medium", "drawing": "low"}

    picked = client.put(
        "/api/settings/omnia",
        json={"recipes_model": "claude-sonnet-5-5", "recipes_reasoning": "medium"},
    )
    assert picked.status_code == 200, picked.text
    assert picked.json()["recipes_model"] == "claude-sonnet-5-5"
    assert picked.json()["recipes_reasoning"] == "medium"
    assert picked.json()["recipes_reasoning_in_effect"] == {"import": "medium", "drawing": "medium"}
    assert picked.json()["reasoning_levels"] == {"recipes": "medium"}
    ai = json.loads((state.settings.data_dir / "ai.json").read_text())
    assert ai["recipes_models"] == {"anthropic": "claude-sonnet-5-5"}
    assert ai["reasoning_levels"] == {"recipes": "medium"}

    assert client.put("/api/settings/omnia", json={"recipes_model": "gpt-6-luna"}).status_code == 400
    assert client.put("/api/settings/omnia", json={"recipes_reasoning": "turbo"}).status_code == 400

    # The chat model clears the model override; "inherit" clears the effort
    # back to its own defaults.
    cleared = client.put(
        "/api/settings/omnia",
        json={"recipes_model": "claude-opus-5-5", "recipes_reasoning": "inherit"},
    ).json()
    assert cleared["recipes_model"] == "claude-opus-5-5"
    assert cleared["recipes_reasoning"] == "inherit"
    assert cleared["recipes_reasoning_in_effect"] == {"import": "medium", "drawing": "low"}
    ai = json.loads((state.settings.data_dir / "ai.json").read_text())
    assert "anthropic" not in ai["recipes_models"]
    assert "recipes" not in ai["reasoning_levels"]


# -- provider.py: the per-instance output ceiling ----------------------------------


def test_build_provider_passes_an_output_ceiling():
    settings = ai_settings()
    default = build_provider(settings, provider_id="anthropic", api_key="sk-ant-x", model="claude-opus-5-5")
    assert default.max_output is None and default.output_ceiling() == 32000
    raised = build_provider(
        settings, provider_id="anthropic", api_key="sk-ant-x", model="claude-opus-5-5", max_output=128000
    )
    assert raised.output_ceiling() == 128000
    assert FakeProvider([]).output_ceiling() == 8192


def test_anthropic_requests_use_the_ceiling_for_tokens_and_thinking():
    from app.overseer.provider import AnthropicProvider

    sent = {}

    async def create(**kwargs):
        sent.update(kwargs)
        block = SimpleNamespace(
            type="text", text="ok", model_dump=lambda exclude_none=True: {"type": "text", "text": "ok"}
        )
        return SimpleNamespace(content=[block], stop_reason="end_turn", usage=None)

    provider = AnthropicProvider("sk-ant-x", "claude-haiku-4-5", "max", max_output=64000)
    provider.client = SimpleNamespace(messages=SimpleNamespace(create=create))
    reply = asyncio.run(provider.chat("sys", [{"role": "user", "content": "hi"}], []))
    assert reply.text == "ok"
    assert sent["max_tokens"] == 64000
    # Haiku's thinking budget is sized under the raised ceiling, not 16000.
    assert sent["thinking"] == {"type": "enabled", "budget_tokens": 32000}


def test_openai_and_gemini_requests_use_the_ceiling():
    from app.overseer.provider import GeminiProvider, OpenAIProvider

    sent = {}

    async def create(**kwargs):
        sent.update(kwargs)
        part = SimpleNamespace(type="output_text", text="ok")
        item = SimpleNamespace(type="message", content=[part])
        return SimpleNamespace(output=[item], incomplete_details=None, usage=None)

    openai = OpenAIProvider("test-key", "gpt-6.1-sol", "high", max_output=64000)
    openai.client = SimpleNamespace(responses=SimpleNamespace(create=create))
    asyncio.run(openai.chat("sys", [{"role": "user", "content": "hi"}], []))
    assert sent["max_output_tokens"] == 64000

    async def generate_content(**kwargs):
        sent.update(kwargs)
        part = SimpleNamespace(text="ok", function_call=None, thought_signature=None)
        candidate = SimpleNamespace(content=SimpleNamespace(parts=[part]), finish_reason=None)
        return SimpleNamespace(candidates=[candidate], usage_metadata=None)

    gemini = GeminiProvider("AQ.test", "gemini-3.6-flash", "max", max_output=40000)
    gemini.client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)))
    asyncio.run(gemini.chat("sys", [{"role": "user", "content": "hi"}], []))
    assert sent["config"]["max_output_tokens"] == 40000
    # Without an override the table still decides.
    assert GeminiProvider("AQ.test", "gemini-3.6-flash").output_ceiling() == 32000
