"""The Media job: backdrop sourcing and the model's art-direction choice."""
import asyncio
import io
import json
from types import SimpleNamespace

import pytest

from app import media_lookup, media_theme
from app.overseer.runtime_config import effective_media_model
from app.vault import fm
from tests.test_media_lookup import FakeResponse, use_client


def png_bytes(color=(180, 40, 40)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (60, 90), color).save(buf, format="PNG")
    return buf.getvalue()


# -- backdrop candidates ------------------------------------------------------


def test_tmdb_backdrops_sorted_and_sized(monkeypatch):
    use_client(
        monkeypatch,
        {
            "movie/693134/images": FakeResponse(
                {
                    "backdrops": [
                        {"file_path": "/b.jpg", "vote_count": 2},
                        {"file_path": "/a.jpg", "vote_count": 9},
                    ]
                }
            )
        },
    )
    urls = asyncio.run(
        media_lookup.backdrop_candidates("movie", "tmdb:movie:693134", {"tmdb": "k"})
    )
    assert urls == [
        "https://image.tmdb.org/t/p/w1280/a.jpg",
        "https://image.tmdb.org/t/p/w1280/b.jpg",
    ]


def test_rawg_screenshots_and_the_quiet_paths(monkeypatch):
    use_client(
        monkeypatch,
        {
            "games/3498/screenshots": FakeResponse(
                {"results": [{"image": "https://media.rawg.io/s1.jpg"}]}
            )
        },
    )
    assert asyncio.run(
        media_lookup.backdrop_candidates("game", "rawg:3498", {"rawg": "k"})
    ) == ["https://media.rawg.io/s1.jpg"]
    # Books, missing keys, and malformed sources are all just "no candidates".
    assert asyncio.run(media_lookup.backdrop_candidates("book", "openlibrary:X", {})) == []
    assert asyncio.run(media_lookup.backdrop_candidates("movie", "tmdb:movie:1", {})) == []
    assert asyncio.run(media_lookup.backdrop_candidates("game", "garbage", {"rawg": "k"})) == []


# -- parsing and validation ---------------------------------------------------


def test_reply_parsing_survives_fences():
    text = 'Here you go:\n```json\n{"backdrop": 2}\n```'
    assert media_theme._parse_reply(text) == {"backdrop": 2}
    assert media_theme._parse_reply("no json here") is None


def test_book_plan_is_clean_and_bounded():
    concept, queries = media_theme._book_plan(
        {
            "concept": "  A life shaped by ambitious industrial systems.  ",
            "queries": [
                " Elon   Musk Tesla factory ",
                "Elon Musk Tesla factory",
                4,
                "SpaceX Starbase Elon Musk",
                "Tesla Fremont assembly line",
                "ignored fourth query",
            ],
        }
    )
    assert concept == "A life shaped by ambitious industrial systems."
    assert queries == [
        "Elon Musk Tesla factory",
        "SpaceX Starbase Elon Musk",
        "Tesla Fremont assembly line",
    ]


def test_biography_queries_require_the_principal_person():
    context = {
        "subjects": ["Biography", "Businesspeople"],
        "people": ["Elon Musk"],
    }
    assert media_theme._ground_book_queries(
        context,
        ["Tesla Fremont factory", "Elon Musk SpaceX Starbase", "Mars rocket launch"],
    ) == [
        "Elon Musk Tesla Fremont factory",
        "Elon Musk SpaceX Starbase",
        "Elon Musk Mars rocket launch",
    ]
    assert media_theme._ground_book_queries(
        {"subjects": ["Fantasy fiction"], "people": ["Bilbo Baggins"]},
        ["New Zealand mountain landscape"],
    ) == ["New Zealand mountain landscape"]


def test_media_model_falls_back_to_chat_model():
    settings = SimpleNamespace(
        anthropic_api_key="sk-ant-x",
        openai_api_key=None,
        google_api_key=None,
        anthropic_model=None,
        openai_model=None,
        google_model=None,
    )
    config = {"provider": "anthropic", "keys": {"anthropic": "sk-ant-x"}}
    chat_default = effective_media_model(settings, config)
    assert chat_default
    config["media_models"] = {"anthropic": "not-a-real-model"}
    assert effective_media_model(settings, config) == chat_default


def test_media_provider_uses_the_media_reasoning_override(monkeypatch):
    from app.overseer.provider import FakeProvider

    captured = {}

    def build(settings, **kwargs):
        captured.update(kwargs)
        return FakeProvider([])

    monkeypatch.setattr(media_theme, "build_provider", build)
    config = {
        "provider": "anthropic",
        "keys": {"anthropic": "sk-ant-x"},
        "reasoning": "high",
        "reasoning_levels": {"media": "low"},
    }
    settings = SimpleNamespace(
        anthropic_api_key="sk-ant-x",
        openai_api_key=None,
        google_api_key=None,
        anthropic_model=None,
        openai_model=None,
        google_model=None,
    )
    state = SimpleNamespace(overseer=None, ai_config=config, settings=settings)
    media_theme._build_media_provider(state)
    assert captured["reasoning"] == "low"


# -- enrichment ---------------------------------------------------------------


ITEM = (
    "---\ntitle: Dune\nmedia: movie\nmedia_status: active\n"
    "creator: Denis Villeneuve\nyear: 2021\ncover: /api/attachments/dune-abcd.jpg\n"
    "source: tmdb:movie:438631\n---\n\nMy review.\n"
)


@pytest.fixture
def themed_state(state, monkeypatch):
    cover = state.store.resolve_attachment("attachments/dune-abcd.jpg")
    cover.parent.mkdir(parents=True, exist_ok=True)
    cover.write_bytes(png_bytes())
    state.store.create("media/Dune.md", ITEM)
    asyncio.run(state.indexer.index_note("media/Dune.md", state.store.read("media/Dune.md")))

    async def fake_candidates(media_type, source, keys):
        return ["https://img/1.jpg", "https://img/2.jpg"]

    async def fake_fetch(url):
        return png_bytes((30, 60, 120)), {"type": "image", "source": {}}

    async def fake_ingest(_state, url, hint="cover"):
        return {"path": f"attachments/{hint}.jpg", "url": f"/api/attachments/{hint}.jpg"}

    monkeypatch.setattr(media_lookup, "backdrop_candidates", fake_candidates)
    monkeypatch.setattr(media_theme, "_fetch_image", fake_fetch)
    monkeypatch.setattr(media_lookup, "ingest_cover", fake_ingest)
    return state


class FakeArtDirector:
    model = "fake-model"

    def __init__(self, text):
        self.text = text
        self.prompts = []

    async def chat(self, system, messages, tools, on_text):
        self.prompts.append((system, messages))
        return SimpleNamespace(text=self.text, usage={"input": 10, "output": 5})


def test_book_research_uses_catalog_context(themed_state, monkeypatch):
    director = FakeArtDirector(
        json.dumps(
            {
                "concept": "An industrial biography about ambition and technology.",
                "queries": ["Elon Musk Tesla factory", "SpaceX Starbase launch site"],
            }
        )
    )

    async def fake_context(source, title, creator):
        assert source == "openlibrary:OL123W"
        assert title == "Elon Musk"
        assert creator == "Walter Isaacson"
        return {
            "description": "A biography of Elon Musk.",
            "subjects": ["Entrepreneurs", "Technology"],
            "people": ["Elon Musk"],
        }

    monkeypatch.setattr(media_lookup, "book_context", fake_context)
    meta = {
        "title": "Elon Musk",
        "creator": "Walter Isaacson",
        "year": 2023,
        "source": "openlibrary:OL123W",
    }
    concept, queries = asyncio.run(
        media_theme._plan_book_backdrop(themed_state, director, meta, "Elon Musk")
    )

    assert concept.startswith("An industrial biography")
    assert queries[0] == "Elon Musk Tesla factory"
    system, messages = director.prompts[0]
    prompt = messages[0]["content"][-1]["text"]
    assert system == media_theme.BOOK_RESEARCH_SYSTEM
    assert "A biography of Elon Musk" in prompt
    assert "People: Elon Musk" in prompt


def test_enrich_applies_model_choice(themed_state, monkeypatch):
    director = FakeArtDirector(json.dumps({"backdrop": 3}))
    monkeypatch.setattr(media_theme, "_build_media_provider", lambda _state: director)

    result = asyncio.run(media_theme.enrich_item(themed_state, "media/Dune.md"))
    # Image 1 is the cover, so 3 means the second candidate.
    assert result["backdrop"] == "/api/attachments/Dune-backdrop.jpg"
    assert "theme" not in result
    meta, _body = fm.parse(themed_state.store.read("media/Dune.md"))
    assert meta["backdrop"] == "/api/attachments/Dune-backdrop.jpg"
    assert "theme" not in meta
    # The prompt names the work and frames the image list.
    system, messages = director.prompts[0]
    text_block = messages[0]["content"][-1]["text"]
    assert '"Dune" (2021)' in text_block and "directed by Denis Villeneuve" in text_block
    assert "Images 2–3 are candidate backdrop stills" in text_block

    # Second run skips unless forced.
    again = asyncio.run(media_theme.enrich_item(themed_state, "media/Dune.md"))
    assert again["skipped"] == "already has a backdrop"
    forced = asyncio.run(
        media_theme.enrich_item(themed_state, "media/Dune.md", force=True)
    )
    assert forced["backdrop"] == "/api/attachments/Dune-backdrop.jpg"


def test_enrich_falls_back_without_a_model(themed_state, monkeypatch):
    monkeypatch.setattr(media_theme, "_build_media_provider", lambda _state: None)
    result = asyncio.run(media_theme.enrich_item(themed_state, "media/Dune.md"))
    # No art director to veto, so the top-voted candidate becomes the backdrop.
    assert result["backdrop"] == "/api/attachments/Dune-backdrop.jpg"


def test_enrich_survives_a_lying_model(themed_state, monkeypatch):
    director = FakeArtDirector("the vibes are immaculate, no json for you")
    monkeypatch.setattr(media_theme, "_build_media_provider", lambda _state: director)
    result = asyncio.run(media_theme.enrich_item(themed_state, "media/Dune.md"))
    # The best-ranked still is the floor rather than failing the item.
    assert result["backdrop"] == "/api/attachments/Dune-backdrop.jpg"


def test_enrich_uses_ranked_still_when_model_omits_choice(themed_state, monkeypatch):
    director = FakeArtDirector(json.dumps({"backdrop": None}))
    monkeypatch.setattr(media_theme, "_build_media_provider", lambda _state: director)

    result = asyncio.run(media_theme.enrich_item(themed_state, "media/Dune.md"))

    assert result["backdrop"] == "/api/attachments/Dune-backdrop.jpg"


def test_attribution_prefers_the_models_pick(themed_state, monkeypatch):
    async def fake_attribution(source, keys):
        assert source == "tmdb:movie:438631"
        return {"director": "Denis Villeneuve", "studio": "Legendary"}

    monkeypatch.setattr(media_lookup, "movie_attribution", fake_attribution)
    director = FakeArtDirector(json.dumps({"backdrop": 2, "credit": "legendary"}))
    monkeypatch.setattr(media_theme, "_build_media_provider", lambda _state: director)

    asyncio.run(media_theme.enrich_item(themed_state, "media/Dune.md"))

    meta, _body = fm.parse(themed_state.store.read("media/Dune.md"))
    # Case-insensitive match, canonical casing written; the mechanical
    # director fill is replaceable.
    assert meta["creator"] == "Legendary"
    _system, messages = director.prompts[0]
    prompt = messages[0]["content"][-1]["text"]
    assert "director is" in prompt and "lead studio is" in prompt
    assert "Denis Villeneuve" in prompt and "Legendary" in prompt


def test_attribution_never_overwrites_a_hand_edit(themed_state, monkeypatch):
    content = themed_state.store.read("media/Dune.md")
    meta, body = fm.parse(content)
    meta["creator"] = "Disney"  # the user's own words
    themed_state.store.write("media/Dune.md", fm.serialize(meta, body))

    async def fake_attribution(source, keys):
        return {"director": "Denis Villeneuve", "studio": "Legendary"}

    monkeypatch.setattr(media_lookup, "movie_attribution", fake_attribution)
    director = FakeArtDirector(json.dumps({"backdrop": 2, "credit": "Denis Villeneuve"}))
    monkeypatch.setattr(media_theme, "_build_media_provider", lambda _state: director)

    asyncio.run(media_theme.enrich_item(themed_state, "media/Dune.md", force=True))

    meta, _body = fm.parse(themed_state.store.read("media/Dune.md"))
    assert meta["creator"] == "Disney"


def test_attribution_ignores_invented_names(themed_state, monkeypatch):
    async def fake_attribution(source, keys):
        return {"director": "Denis Villeneuve", "studio": "Legendary"}

    monkeypatch.setattr(media_lookup, "movie_attribution", fake_attribution)
    director = FakeArtDirector(json.dumps({"backdrop": 2, "credit": "Warner Bros"}))
    monkeypatch.setattr(media_theme, "_build_media_provider", lambda _state: director)

    asyncio.run(media_theme.enrich_item(themed_state, "media/Dune.md"))

    meta, _body = fm.parse(themed_state.store.read("media/Dune.md"))
    assert meta["creator"] == "Denis Villeneuve"


def test_enrich_retires_a_stale_palette(themed_state, monkeypatch):
    # Items themed before the standard look carry a theme: block; a forced
    # re-run must remove it along with writing the backdrop.
    content = themed_state.store.read("media/Dune.md")
    meta, body = fm.parse(content)
    meta["theme"] = {"bg": "#101216"}
    themed_state.store.write("media/Dune.md", fm.serialize(meta, body))
    monkeypatch.setattr(media_theme, "_build_media_provider", lambda _state: None)

    asyncio.run(media_theme.enrich_item(themed_state, "media/Dune.md", force=True))

    meta, _body = fm.parse(themed_state.store.read("media/Dune.md"))
    assert "theme" not in meta
    assert meta["backdrop"] == "/api/attachments/Dune-backdrop.jpg"
