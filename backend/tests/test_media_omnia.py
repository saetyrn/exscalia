"""OMNIA's media tools: lookup passthrough, logging with cover ingest, updates."""
import asyncio
from types import SimpleNamespace

import pytest

from app import media_lookup
from app.overseer import tool_expansion as tx
from app.overseer.agent import Overseer
from app.overseer.provider import FakeProvider, ProviderReply
from app.vault import fm
from app.vault.notes import NoteError
from tests.test_overseer import tool_use_reply


@pytest.fixture
def omnia_state(acting_state):
    acting_state.timezone = "UTC"
    return acting_state


@pytest.fixture(autouse=True)
def fresh_offers(monkeypatch):
    monkeypatch.setattr(tx, "_OFFERED_IMAGE_URLS", {})


def stored_attachment(state, rel="attachments/x.jpg"):
    abs_path = state.store.resolve_attachment(rel)
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_bytes(b"jpeg")
    return {"path": rel, "url": f"/api/{rel}"}


def offer_cover(state, monkeypatch, url, thread="t"):
    """What the real flow does first: lookup_media returns the cover URL."""

    async def fake_search(media_type, query, keys):
        return {"candidates": [{"title": "x", "cover_url": url}]}

    monkeypatch.setattr(media_lookup, "search_media", fake_search)
    asyncio.run(tx.lookup_media(state, thread, {"media_type": "book", "query": "x"}))


def offer_gallery(state, monkeypatch, urls, thread="t"):
    """What the real flow does first: search_media_images shows a gallery."""

    async def fake_images(media_type, source, query, keys, *, kind="backdrop", limit=8):
        return [{"url": url, "origin": "commons"} for url in urls]

    monkeypatch.setattr(media_lookup, "image_candidates", fake_images)
    asyncio.run(tx.search_media_images(state, thread, {"query": "x"}))


def test_lookup_media_passes_through_search(omnia_state, monkeypatch):
    seen = {}

    async def fake_search(media_type, query, keys):
        seen.update(media_type=media_type, query=query, keys=keys)
        return {"candidates": [{"title": "Dune"}]}

    monkeypatch.setattr(media_lookup, "search_media", fake_search)
    result = asyncio.run(
        tx.lookup_media(omnia_state, "t", {"media_type": "book", "query": "dune"})
    )
    assert result == {"candidates": [{"title": "Dune"}]}
    assert seen["media_type"] == "book" and seen["query"] == "dune"


def test_log_media_creates_note_cover_and_undoable_action(omnia_state, monkeypatch):
    async def fake_ingest(state, url, hint="cover"):
        rel = "attachments/elon-musk-a-biography-abc123.jpg"
        abs_path = state.store.resolve_attachment(rel)
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_bytes(b"jpeg")
        return {"path": rel, "url": f"/api/{rel}"}

    monkeypatch.setattr(media_lookup, "ingest_cover", fake_ingest)
    offer_cover(omnia_state, monkeypatch, "https://covers.example/x.jpg")
    result = asyncio.run(
        tx.log_media(
            omnia_state,
            "t",
            {
                "media_type": "book",
                "title": "Elon Musk: A Biography",
                "creator": "Walter Isaacson",
                "year": 2023,
                "total": 688,
                "cover_url": "https://covers.example/x.jpg",
                "status": "active",
            },
        )
    )
    assert result["path"] == "media/Elon Musk A Biography.md"
    assert result["_action"]["navigate"] == "/note/media/Elon Musk A Biography.md"
    content = omnia_state.store.read(result["path"])
    assert "title: 'Elon Musk: A Biography'" in content or (
        'title: "Elon Musk: A Biography"' in content
    )
    assert "creator: Walter Isaacson" in content
    assert "cover: /api/attachments/elon-musk-a-biography-abc123.jpg" in content
    assert "— started" in content
    # The cover attachment is part of the action's paths, so Undo reverts it.
    assert "attachments/elon-musk-a-biography-abc123.jpg" in result["_action"]["paths"]

    # Logging the same title again refuses instead of duplicating.
    again = asyncio.run(
        tx.log_media(
            omnia_state,
            "t",
            {"media_type": "book", "title": "Elon Musk: A Biography"},
        )
    )
    assert "Already tracked" in again["error"]


def test_log_media_degrades_when_cover_fails(omnia_state, monkeypatch):
    async def failing_ingest(state, url, hint="cover"):
        raise NoteError(502, "Cover download failed: no route")

    monkeypatch.setattr(media_lookup, "ingest_cover", failing_ingest)
    offer_cover(omnia_state, monkeypatch, "https://covers.example/x.jpg")
    result = asyncio.run(
        tx.log_media(
            omnia_state,
            "t",
            {
                "media_type": "movie",
                "title": "Dune",
                "cover_url": "https://covers.example/x.jpg",
            },
        )
    )
    assert result["path"] == "media/Dune.md"
    assert "Cover download failed" in result["cover_error"]
    assert "cover:" not in omnia_state.store.read(result["path"])


def test_image_gallery_lift_shows_chat_and_numbers_urls(state, monkeypatch):
    async def fake_images(media_type, source, query, keys, *, kind="backdrop", limit=8):
        assert query == "dune desert"
        return [
            {"url": "https://img/a.jpg", "thumb": "https://img/a-t.jpg", "origin": "tmdb"},
            {"url": "https://img/b.jpg", "thumb": "https://img/b-t.jpg", "origin": "commons"},
        ]

    monkeypatch.setattr(media_lookup, "image_candidates", fake_images)
    provider = FakeProvider(
        [
            tool_use_reply(
                "search_media_images", {"query": "dune desert", "kind": "backdrop"}
            ),
            ProviderReply(text="The gallery is in the chat.", stop_reason="end_turn"),
        ]
    )
    overseer = Overseer(provider, state)
    result = asyncio.run(overseer.chat("show me new backdrops for dune"))

    assert len(result["galleries"]) == 1
    gallery = result["galleries"][0]
    assert gallery["kind"] == "backdrop"
    assert [image["url"] for image in gallery["images"]] == [
        "https://img/a.jpg",
        "https://img/b.jpg",
    ]
    assert [image["thumb"] for image in gallery["images"]] == [
        "https://img/a-t.jpg",
        "https://img/b-t.jpg",
    ]
    # The model got a numbered URL list, never the gallery blob.
    tool_results = [
        m
        for m in provider.seen[-1]
        if m["role"] == "user" and isinstance(m["content"], list)
    ]
    content = tool_results[-1]["content"][0]["content"]
    assert "_gallery" not in content
    assert "1: https://img/a.jpg" in content
    assert "2: https://img/b.jpg" in content


def test_set_media_art_ingests_and_writes_frontmatter(omnia_state, monkeypatch):
    created = asyncio.run(
        tx.log_media(omnia_state, "t", {"media_type": "movie", "title": "Dune"})
    )

    async def fake_ingest(state, url, hint="cover", *, kind="cover"):
        assert url == "https://img/pick.jpg"
        # A backdrop is stored at hero size, not capped like a cover.
        assert kind == "backdrop"
        rel = f"attachments/{hint}-feed.jpg"
        abs_path = state.store.resolve_attachment(rel)
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_bytes(b"jpeg")
        return {"path": rel, "url": f"/api/{rel}"}

    monkeypatch.setattr(media_lookup, "ingest_cover", fake_ingest)
    offer_gallery(omnia_state, monkeypatch, ["https://img/other.jpg", "https://img/pick.jpg"])
    result = asyncio.run(
        tx.set_media_art(
            omnia_state,
            "t",
            {"path": created["path"], "kind": "backdrop", "url": "https://img/pick.jpg"},
        )
    )
    assert result["backdrop"] == "/api/attachments/Dune-backdrop-feed.jpg"
    content = omnia_state.store.read(created["path"])
    assert "backdrop: /api/attachments/Dune-backdrop-feed.jpg" in content
    # The binary is inside the undoable action.
    assert "attachments/Dune-backdrop-feed.jpg" in result["_action"]["paths"]

    with pytest.raises(NoteError):
        asyncio.run(
            tx.set_media_art(
                omnia_state,
                "t",
                {"path": created["path"], "kind": "poster", "url": "https://img/x.jpg"},
            )
        )


def test_unoffered_image_urls_never_reach_the_network(omnia_state, monkeypatch):
    fetched = []

    async def fake_ingest(state, url, hint="cover", **_kw):
        fetched.append(url)
        return {"path": "attachments/x.jpg", "url": "/api/attachments/x.jpg"}

    monkeypatch.setattr(media_lookup, "ingest_cover", fake_ingest)
    # Offered in another conversation only: still not this thread's to use.
    offer_gallery(omnia_state, monkeypatch, ["https://img/pick.jpg"], thread="other")
    planted = "https://attacker.example/c.png?d=user+lives+at+42+Elm+St"

    logged = asyncio.run(
        tx.log_media(
            omnia_state,
            "t",
            {"media_type": "book", "title": "Dune", "cover_url": planted},
        )
    )
    assert logged["path"] == "media/Dune.md"
    assert logged["cover_error"] == (
        "cover_url was not returned by lookup_media in this conversation"
        " — item created without a cover"
    )
    assert "cover:" not in omnia_state.store.read(logged["path"])

    for url in (planted, "https://img/pick.jpg"):
        with pytest.raises(NoteError) as err:
            asyncio.run(
                tx.set_media_art(
                    omnia_state,
                    "t",
                    {"path": logged["path"], "kind": "backdrop", "url": url},
                )
            )
        assert err.value.status == 400
        assert "search again" in err.value.detail
    assert fetched == []


def test_persisted_galleries_still_count_after_a_restart(omnia_state, monkeypatch):
    created = asyncio.run(
        tx.log_media(omnia_state, "t", {"media_type": "movie", "title": "Dune"})
    )

    async def fake_ingest(state, url, hint="cover", **_kw):
        return stored_attachment(state)

    monkeypatch.setattr(media_lookup, "ingest_cover", fake_ingest)
    thread = {
        "messages": [
            {"role": "user", "text": "new backdrop"},
            {
                "role": "assistant",
                "galleries": [{"kind": "backdrop", "images": [{"url": "https://img/a.jpg"}]}],
            },
        ]
    }
    omnia_state.threads = SimpleNamespace(get=lambda thread_id: thread)
    result = asyncio.run(
        tx.set_media_art(
            omnia_state,
            "t",
            {"path": created["path"], "kind": "backdrop", "url": "https://img/a.jpg"},
        )
    )
    assert result["backdrop"] == "/api/attachments/x.jpg"
    with pytest.raises(NoteError):
        asyncio.run(
            tx.set_media_art(
                omnia_state,
                "t",
                {"path": created["path"], "kind": "backdrop", "url": "https://img/b.jpg"},
            )
        )


def test_offered_urls_are_bounded(monkeypatch):
    monkeypatch.setattr(tx, "OFFERED_URLS_PER_THREAD", 3)
    monkeypatch.setattr(tx, "OFFERED_URL_THREADS", 2)
    tx._offer_image_urls("a", ["https://1", "https://2", "https://3"])
    tx._offer_image_urls("a", ["https://4", "https://2"])
    assert tx._OFFERED_IMAGE_URLS["a"] == ["https://3", "https://4", "https://2"]
    tx._offer_image_urls("b", ["https://5"])
    tx._offer_image_urls("a", ["https://6"])
    tx._offer_image_urls("c", ["https://7"])
    # "b" was offered least recently, so it goes first.
    assert list(tx._OFFERED_IMAGE_URLS) == ["a", "c"]


def test_set_media_art_keeps_edits_made_during_the_download(omnia_state, monkeypatch):
    created = asyncio.run(
        tx.log_media(omnia_state, "t", {"media_type": "movie", "title": "Dune"})
    )
    path = created["path"]

    async def slow_ingest(state, url, hint="cover", **_kw):
        # The user rates the item while the image is still downloading.
        meta, body = fm.parse(state.store.read(path))
        meta["rating"] = 9
        state.store.write(path, fm.serialize(meta, body))
        return stored_attachment(state)

    monkeypatch.setattr(media_lookup, "ingest_cover", slow_ingest)
    offer_gallery(omnia_state, monkeypatch, ["https://img/pick.jpg"])
    asyncio.run(
        tx.set_media_art(
            omnia_state,
            "t",
            {"path": path, "kind": "backdrop", "url": "https://img/pick.jpg"},
        )
    )
    content = omnia_state.store.read(path)
    assert "rating: 9" in content
    assert "backdrop: /api/attachments/x.jpg" in content


def test_set_media_review_replaces_body_keeps_log(omnia_state):
    created = asyncio.run(
        tx.log_media(
            omnia_state, "t", {"media_type": "book", "title": "Dune", "status": "active"}
        )
    )
    result = asyncio.run(
        tx.set_media_review(
            omnia_state,
            "t",
            {
                "path": created["path"],
                "base_revision": created["revision"],
                "review": "A desert epic that earns its length.",
            },
        )
    )
    content = omnia_state.store.read(created["path"])
    assert "A desert epic that earns its length." in content
    assert "## Log" in content and "— started" in content
    assert result["_action"]["summary"] == 'Wrote the review of "Dune"'


def test_media_stats_summarizes_the_library(omnia_state):
    asyncio.run(
        tx.log_media(
            omnia_state,
            "t",
            {"media_type": "book", "title": "A", "status": "finished", "rating": 8},
        )
    )
    asyncio.run(
        tx.log_media(
            omnia_state,
            "t",
            {"media_type": "movie", "title": "B", "status": "planned"},
        )
    )
    stats = asyncio.run(tx.media_stats(omnia_state, "t", {}))
    assert stats["by_type"]["book"]["finished"] == 1
    assert stats["by_type"]["movie"]["planned"] == 1
    assert stats["rating_histogram_1_to_10"][7] == 1
    assert stats["average_rating_by_type"]["book"] == 8
    assert stats["top_rated"][0]["title"] == "A"
    assert stats["finished_by_month"][0]["finished"] == 1


def test_details_and_similar_pass_through(omnia_state, monkeypatch):
    async def fake_details(media_type, source, query, keys):
        return {"title": "Dune", "overview": "Sand.", "genres": ["Sci-Fi"]}

    async def fake_similar(media_type, source, keys):
        return {"candidates": [{"title": "Arrival"}]}

    monkeypatch.setattr(media_lookup, "media_details", fake_details)
    monkeypatch.setattr(media_lookup, "similar_media", fake_similar)
    details = asyncio.run(
        tx.media_details(omnia_state, "t", {"media_type": "movie", "query": "dune"})
    )
    assert details["genres"] == ["Sci-Fi"]
    similar = asyncio.run(
        tx.similar_media(
            omnia_state, "t", {"media_type": "movie", "source": "tmdb:movie:1"}
        )
    )
    assert similar["candidates"][0]["title"] == "Arrival"


def test_update_media_guards_revision_and_applies(omnia_state):
    created = asyncio.run(
        tx.log_media(
            omnia_state,
            "t",
            {"media_type": "book", "title": "Dune", "status": "active"},
        )
    )
    path, revision = created["path"], created["revision"]

    with pytest.raises(NoteError):
        asyncio.run(
            tx.update_media(omnia_state, "t", {"path": path, "base_revision": None, "rating": 8})
        )
    with pytest.raises(NoteError):
        asyncio.run(
            tx.update_media(
                omnia_state, "t", {"path": path, "base_revision": "stale", "rating": 8}
            )
        )

    result = asyncio.run(
        tx.update_media(
            omnia_state,
            "t",
            {
                "path": path,
                "base_revision": revision,
                "entry": {"kind": "progress", "value": "120"},
            },
        )
    )
    assert result["summary"] == "Logged page 120"
    assert result["_action"]["summary"] == 'Logged page 120 — "Dune"'
    assert "- " in omnia_state.store.read(path)

    finished = asyncio.run(
        tx.update_media(
            omnia_state,
            "t",
            {
                "path": path,
                "base_revision": result["revision"],
                "status": "finished",
                "rating": 9,
            },
        )
    )
    content = omnia_state.store.read(path)
    assert "finished, rated 9" in content
    assert "rating: 9" in content
