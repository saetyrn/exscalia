"""The Media job: backdrop sourcing and the model's art-direction choice."""
import asyncio
import io
import json
from types import SimpleNamespace

import pytest

from app import media_items, media_lookup, media_theme
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
        return png_bytes((30, 60, 120)), "image/png", {"type": "image", "source": {}}

    async def fake_store(_state, data, ctype, hint, **kw):
        return {"path": f"attachments/{hint}.jpg", "url": f"/api/attachments/{hint}.jpg"}

    monkeypatch.setattr(media_lookup, "backdrop_candidates", fake_candidates)
    monkeypatch.setattr(media_theme, "_fetch_image", fake_fetch)
    monkeypatch.setattr(media_lookup, "store_image_bytes", fake_store)
    return state


class FakeArtDirector:
    model = "fake-model"

    def __init__(self, text):
        self.text = text
        self.prompts = []

    async def chat(self, system, messages, tools, on_text):
        self.prompts.append((system, messages))
        return SimpleNamespace(text=self.text, usage={"input": 10, "output": 5})


class MidJobEditor(FakeArtDirector):
    """The vision call is where the job parks for seconds; `edit` stands in
    for whatever the user (or OMNIA) writes to the note meanwhile."""

    def __init__(self, text, edit):
        super().__init__(text)
        self.edit = edit

    async def chat(self, system, messages, tools, on_text):
        self.edit()
        return await super().chat(system, messages, tools, on_text)


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


def test_enrich_keeps_edits_made_during_the_job(themed_state, monkeypatch):
    # "Log Severance, I'm on S01E05": the rating and log line land while the
    # job is still choosing art. Its write must patch them, not revert them.
    def rate_and_log():
        updated, _summary = media_items.apply_update(
            themed_state.store.read("media/Dune.md"),
            rating=9,
            entry={"kind": "progress", "value": "S01E05"},
            today="2026-09-22",
        )
        themed_state.store.write("media/Dune.md", updated)

    director = MidJobEditor(json.dumps({"backdrop": 2}), rate_and_log)
    monkeypatch.setattr(media_theme, "_build_media_provider", lambda _state: director)

    result = asyncio.run(
        media_theme.enrich_item(themed_state, "media/Dune.md", force=True)
    )

    meta, body = fm.parse(themed_state.store.read("media/Dune.md"))
    assert meta["rating"] == 9
    assert "S01E05" in body and "My review." in body
    assert meta["backdrop"] == "/api/attachments/Dune-backdrop.jpg"
    assert "theme" not in meta
    assert result["backdrop"] == "/api/attachments/Dune-backdrop.jpg"


def test_enrich_skips_when_the_note_vanishes(themed_state, monkeypatch):
    director = MidJobEditor(
        json.dumps({"backdrop": 2}),
        lambda: themed_state.store.delete("media/Dune.md"),
    )
    monkeypatch.setattr(media_theme, "_build_media_provider", lambda _state: director)

    result = asyncio.run(
        media_theme.enrich_item(themed_state, "media/Dune.md", force=True)
    )

    assert result["skipped"] == "note vanished during enrichment"
    assert not themed_state.store.exists("media/Dune.md")


def test_enrich_yields_to_a_backdrop_set_during_the_job(themed_state, monkeypatch):
    # A parallel run or a hand-picked backdrop got there first; an unforced
    # run leaves it alone.
    def pick_by_hand():
        meta, body = fm.parse(themed_state.store.read("media/Dune.md"))
        meta["backdrop"] = "/api/attachments/picked.jpg"
        themed_state.store.write("media/Dune.md", fm.serialize(meta, body))

    director = MidJobEditor(json.dumps({"backdrop": 2}), pick_by_hand)
    monkeypatch.setattr(media_theme, "_build_media_provider", lambda _state: director)

    result = asyncio.run(media_theme.enrich_item(themed_state, "media/Dune.md"))

    assert result["skipped"] == "already has a backdrop"
    meta, _body = fm.parse(themed_state.store.read("media/Dune.md"))
    assert meta["backdrop"] == "/api/attachments/picked.jpg"


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


def test_attribution_never_overwrites_a_hand_edit_made_during_the_job(
    themed_state, monkeypatch
):
    def retype_creator():
        meta, body = fm.parse(themed_state.store.read("media/Dune.md"))
        meta["creator"] = "Disney"
        themed_state.store.write("media/Dune.md", fm.serialize(meta, body))

    async def fake_attribution(source, keys):
        return {"director": "Denis Villeneuve", "studio": "Legendary"}

    monkeypatch.setattr(media_lookup, "movie_attribution", fake_attribution)
    director = MidJobEditor(
        json.dumps({"backdrop": 2, "credit": "Legendary"}), retype_creator
    )
    monkeypatch.setattr(media_theme, "_build_media_provider", lambda _state: director)

    asyncio.run(media_theme.enrich_item(themed_state, "media/Dune.md", force=True))

    meta, _body = fm.parse(themed_state.store.read("media/Dune.md"))
    # The snapshot still said "Denis Villeneuve"; only the fresh value counts.
    assert meta["creator"] == "Disney"
    assert meta["backdrop"] == "/api/attachments/Dune-backdrop.jpg"


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


# -- job resources ------------------------------------------------------------


def test_enrich_stores_the_chosen_bytes_without_a_second_download(
    themed_state, monkeypatch
):
    fetched, stored = [], []

    async def distinct_fetch(url):
        fetched.append(url)
        return f"bytes of {url}".encode(), "image/jpeg", {"type": "image", "source": {}}

    async def fake_store(_state, data, ctype, hint, **kw):
        stored.append((data, ctype, hint, kw))
        return {"path": f"attachments/{hint}.jpg", "url": f"/api/attachments/{hint}.jpg"}

    async def no_download(*_args, **_kw):
        raise AssertionError("the chosen still was downloaded again")

    monkeypatch.setattr(media_theme, "_fetch_image", distinct_fetch)
    monkeypatch.setattr(media_lookup, "store_image_bytes", fake_store)
    monkeypatch.setattr(media_lookup, "ingest_cover", no_download)
    monkeypatch.setattr(media_lookup, "fetch_image_bytes", no_download)
    # Image 1 is the cover, so 3 is the second candidate.
    director = FakeArtDirector(json.dumps({"backdrop": 3}))
    monkeypatch.setattr(media_theme, "_build_media_provider", lambda _state: director)

    result = asyncio.run(media_theme.enrich_item(themed_state, "media/Dune.md"))

    assert result["backdrop"] == "/api/attachments/Dune-backdrop.jpg"
    assert fetched == ["https://img/1.jpg", "https://img/2.jpg"]
    assert stored == [
        (b"bytes of https://img/2.jpg", "image/jpeg", "Dune-backdrop", {"kind": "backdrop"})
    ]


def test_enrich_downloads_candidates_in_parallel(themed_state, monkeypatch):
    started = []

    async def main():
        both_started = asyncio.Event()

        async def gated_fetch(url):
            started.append(url)
            if len(started) == 2:
                both_started.set()
            # One at a time, the first download would wait here forever.
            await asyncio.wait_for(both_started.wait(), 2)
            return png_bytes(), "image/png", {"type": "image", "source": {}}

        monkeypatch.setattr(media_theme, "_fetch_image", gated_fetch)
        return await media_theme.enrich_item(themed_state, "media/Dune.md")

    monkeypatch.setattr(media_theme, "_build_media_provider", lambda _state: None)
    result = asyncio.run(main())

    assert started == ["https://img/1.jpg", "https://img/2.jpg"]
    assert result["backdrop"] == "/api/attachments/Dune-backdrop.jpg"


class ParkedArtDirector(FakeArtDirector):
    """Holds every vision call until `release` is set; `entered` counts them."""

    def __init__(self, text, release):
        super().__init__(text)
        self.release = release
        self.entered = 0

    async def chat(self, system, messages, tools, on_text):
        self.entered += 1
        await self.release.wait()
        return await super().chat(system, messages, tools, on_text)


def add_media_notes(state, *titles) -> list[str]:
    paths = []
    for title in titles:
        path = f"media/{title}.md"
        state.store.create(path, ITEM.replace("title: Dune", f"title: {title}"))
        paths.append(path)
    return paths


async def wait_for(condition, timeout=2.0):
    async def poll():
        while not condition():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(poll(), timeout)


def test_enrich_soon_runs_at_most_two_jobs_at_once(themed_state, monkeypatch):
    paths = ["media/Dune.md", *add_media_notes(themed_state, "Arrival", "Sicario")]

    async def main():
        director = ParkedArtDirector(json.dumps({"backdrop": 2}), asyncio.Event())
        monkeypatch.setattr(media_theme, "_build_media_provider", lambda _state: director)
        for path in paths:
            media_theme.enrich_soon(themed_state, path)
        await wait_for(lambda: director.entered == media_theme.ENRICH_CONCURRENCY)
        await asyncio.sleep(0.05)
        # The third job waits at the gate, not in the pipeline.
        assert director.entered == 2
        director.release.set()
        await asyncio.wait_for(asyncio.gather(*media_theme._TASKS), 2)
        return director.entered

    assert asyncio.run(main()) == 3
    assert not media_theme._TASKS
    for path in paths:
        meta, _body = fm.parse(themed_state.store.read(path))
        assert meta["backdrop"].endswith("-backdrop.jpg")


def test_shutdown_cancels_running_and_queued_jobs(themed_state, monkeypatch):
    monkeypatch.setattr(media_theme, "ENRICH_CONCURRENCY", 1)
    paths = ["media/Dune.md", *add_media_notes(themed_state, "Arrival")]

    async def main():
        director = ParkedArtDirector(json.dumps({"backdrop": 2}), asyncio.Event())
        monkeypatch.setattr(media_theme, "_build_media_provider", lambda _state: director)
        for path in paths:
            media_theme.enrich_soon(themed_state, path)
        await wait_for(lambda: director.entered == 1)
        tasks = set(media_theme._TASKS)
        assert len(tasks) == 2
        await asyncio.wait_for(media_theme.shutdown(), 2)
        assert all(task.cancelled() for task in tasks)
        return director.entered

    # One job was parked in the vision call, the other queued at the gate;
    # neither gets further and nothing escapes shutdown.
    assert asyncio.run(main()) == 1
    assert not media_theme._TASKS
    for path in paths:
        meta, _body = fm.parse(themed_state.store.read(path))
        assert "backdrop" not in meta
