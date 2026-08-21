"""Metadata providers (canned responses), cover ingestion, and key storage."""
import asyncio
import json
import stat

import httpx
import pytest

from app import media_lookup
from app.vault.notes import NoteError


class FakeResponse:
    def __init__(self, payload=None, *, content=b"", content_type="application/json", status=200):
        self._payload = payload
        self.content = content
        self.headers = {"content-type": content_type}
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)

    def json(self):
        return self._payload

    async def aiter_bytes(self):
        for i in range(0, len(self.content), 4096):
            yield self.content[i : i + 4096]


class FakeStream:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *args):
        return False


class FakeClient:
    """Routes URL fragments to canned responses; unmatched URLs fail loudly."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def _match(self, url):
        for fragment, response in self.routes.items():
            if fragment in url:
                return response
        raise AssertionError(f"unexpected URL: {url}")

    async def get(self, url, params=None, headers=None):
        self.calls.append(url)
        response = self._match(url)
        if isinstance(response, Exception):
            raise response
        return response

    async def post(self, url, params=None, content=None, headers=None):
        self.calls.append(url)
        response = self._match(url)
        if isinstance(response, Exception):
            raise response
        return response

    def stream(self, method, url):
        response = self._match(url)
        if isinstance(response, Exception):
            raise response
        return FakeStream(response)


def use_client(monkeypatch, routes) -> FakeClient:
    client = FakeClient(routes)
    monkeypatch.setattr(media_lookup, "_client", lambda: client)
    return client


def search(media_type, query, keys=None):
    return asyncio.run(media_lookup.search_media(media_type, query, keys or {}))


def test_openlibrary_candidates_are_normalized(monkeypatch):
    use_client(
        monkeypatch,
        {
            "openlibrary.org/search.json": FakeResponse(
                {
                    "docs": [
                        {
                            "key": "/works/OL123W",
                            "title": "Elon Musk",
                            "author_name": ["Walter Isaacson"],
                            "first_publish_year": 2023,
                            "cover_i": 14625765,
                            "number_of_pages_median": 688,
                        },
                        {"title": "Coverless", "author_name": []},
                    ]
                }
            )
        },
    )
    result = search("book", "elon musk")
    assert "error" not in result
    top, bare = result["candidates"]
    assert top == {
        "title": "Elon Musk",
        "creator": "Walter Isaacson",
        "year": 2023,
        "cover_url": "https://covers.openlibrary.org/b/id/14625765-L.jpg",
        "total": 688,
        "source": "openlibrary:OL123W",
    }
    assert bare["cover_url"] is None and bare["creator"] is None


def test_tmdb_movie_gets_director_from_credits(monkeypatch):
    use_client(
        monkeypatch,
        {
            "search/movie": FakeResponse(
                {
                    "results": [
                        {
                            "id": 693134,
                            "title": "Dune: Part Two",
                            "release_date": "2024-02-27",
                            "poster_path": "/dune2.jpg",
                        }
                    ]
                }
            ),
            "movie/693134": FakeResponse(
                {
                    "credits": {"crew": [{"name": "Denis Villeneuve", "job": "Director"}]},
                    "production_companies": [{"name": "Legendary Pictures"}],
                }
            ),
        },
    )
    (candidate,) = search("movie", "dune part two", {"tmdb": "k"})["candidates"]
    assert candidate["creator"] == "Denis Villeneuve"
    assert candidate["studio"] == "Legendary Pictures"
    assert candidate["year"] == 2024
    assert candidate["cover_url"] == "https://image.tmdb.org/t/p/w500/dune2.jpg"
    assert candidate["source"] == "tmdb:movie:693134"


def test_tmdb_tv_gets_creator_and_episode_count(monkeypatch):
    use_client(
        monkeypatch,
        {
            "search/tv": FakeResponse(
                {
                    "results": [
                        {"id": 95396, "name": "Severance", "first_air_date": "2022-02-18"}
                    ]
                }
            ),
            "tv/95396": FakeResponse(
                {"created_by": [{"name": "Dan Erickson"}], "number_of_episodes": 19}
            ),
        },
    )
    (candidate,) = search("tv", "severance", {"tmdb": "k"})["candidates"]
    assert candidate["creator"] == "Dan Erickson"
    assert candidate["total"] == 19


def test_detail_failure_keeps_the_search_hit(monkeypatch):
    use_client(
        monkeypatch,
        {
            "search/movie": FakeResponse(
                {"results": [{"id": 1, "title": "Obscure", "release_date": "1999-01-01"}]}
            ),
            "movie/1": httpx.ConnectError("no route"),
        },
    )
    (candidate,) = search("movie", "obscure", {"tmdb": "k"})["candidates"]
    assert candidate["title"] == "Obscure"
    assert candidate["creator"] is None


def test_movie_attribution_by_source(monkeypatch):
    use_client(
        monkeypatch,
        {
            "movie/438631": FakeResponse(
                {
                    "credits": {"crew": [{"name": "Denis Villeneuve", "job": "Director"}]},
                    "production_companies": [{"name": "Legendary Pictures"}],
                }
            )
        },
    )
    result = asyncio.run(
        media_lookup.movie_attribution("tmdb:movie:438631", {"tmdb": "k"})
    )
    assert result == {"director": "Denis Villeneuve", "studio": "Legendary Pictures"}
    # No key, wrong source shape, or network trouble → quiet empty dict.
    assert asyncio.run(media_lookup.movie_attribution("tmdb:movie:438631", {})) == {}
    assert asyncio.run(media_lookup.movie_attribution("rawg:5", {"tmdb": "k"})) == {}
    use_client(monkeypatch, {"movie/438631": httpx.ConnectError("down")})
    assert asyncio.run(
        media_lookup.movie_attribution("tmdb:movie:438631", {"tmdb": "k"})
    ) == {}


def test_rawg_candidates(monkeypatch):
    use_client(
        monkeypatch,
        {
            "api.rawg.io/api/games": FakeResponse(
                {
                    "results": [
                        {
                            "id": 3498,
                            "name": "GTA V",
                            "released": "2013-09-17",
                            "background_image": "https://media.rawg.io/gta.jpg",
                        }
                    ]
                }
            )
        },
    )
    (candidate,) = search("game", "gta", {"rawg": "k"})["candidates"]
    assert candidate == {
        "title": "GTA V",
        "creator": None,
        "year": 2013,
        "cover_url": "https://media.rawg.io/gta.jpg",
        "total": None,
        "source": "rawg:3498",
    }


def test_book_context_and_landscape_backdrops(monkeypatch):
    client = use_client(
        monkeypatch,
        {
            "works/OL123W.json": FakeResponse(
                {
                    "description": {"value": "A biography of a technology entrepreneur."},
                    "subjects": ["Biography", "Technology"],
                    "subject_people": ["Elon Musk"],
                    "subject_places": ["United States"],
                }
            ),
            "commons.wikimedia.org": FakeResponse(
                {
                    "query": {
                        "pages": {
                            "1": {
                                "index": 2,
                                "imageinfo": [
                                    {
                                        "size": 200_000,
                                        "width": 800,
                                        "height": 1200,
                                        "mime": "image/jpeg",
                                        "thumburl": "https://img/portrait.jpg",
                                    }
                                ],
                            },
                            "2": {
                                "index": 1,
                                "imageinfo": [
                                    {
                                        "size": 747_058,
                                        "width": 3008,
                                        "height": 2000,
                                        "mime": "image/jpeg",
                                        "url": "https://img/factory-original.jpg?tracking=1",
                                        "thumburl": "https://img/factory.jpg",
                                    }
                                ],
                            },
                            "3": {
                                "index": 3,
                                "imageinfo": [
                                    {
                                        "size": 2_000,
                                        "width": 2400,
                                        "height": 1200,
                                        "mime": "image/svg+xml",
                                        "thumburl": "https://img/logo.svg",
                                    }
                                ],
                            },
                        }
                    }
                }
            ),
        },
    )

    context = asyncio.run(media_lookup.book_context("openlibrary:OL123W"))
    urls = asyncio.run(media_lookup.book_backdrop_candidates(["Elon Musk Tesla factory"]))

    assert context["description"].startswith("A biography")
    assert context["people"] == ["Elon Musk"]
    assert urls == ["https://img/factory-original.jpg"]
    assert any("commons.wikimedia.org" in url for url in client.calls)


def test_book_context_can_resolve_a_manual_entry(monkeypatch):
    client = use_client(
        monkeypatch,
        {
            "search.json": FakeResponse({"docs": [{"key": "/works/OL123W"}]}),
            "works/OL123W.json": FakeResponse(
                {"description": "A researched biography.", "subjects": ["Biography"]}
            ),
        },
    )

    context = asyncio.run(media_lookup.book_context(None, "Elon Musk", "Walter Isaacson"))

    assert context["description"] == "A researched biography."
    assert any("search.json" in url for url in client.calls)


def test_missing_keys_and_failures_never_raise(monkeypatch):
    assert "TMDB API key not set" in search("movie", "dune")["error"]
    assert search("book", "  ")["error"] == "Empty query"
    assert "Unknown media type" in search("podcast", "x")["error"]

    use_client(monkeypatch, {"openlibrary.org": httpx.ConnectError("offline")})
    result = search("book", "dune")
    assert result["candidates"] == [] and "Lookup failed" in result["error"]


STEAM_SEARCH = FakeResponse(
    {"items": [{"id": 620, "name": "Portal 2", "tiny_image": "x"}]}
)
STEAM_DETAILS = FakeResponse(
    {
        "620": {
            "data": {
                "developers": ["Valve"],
                "release_date": {"date": "18 Apr, 2011"},
                "screenshots": [
                    {"path_full": "https://cdn.steam/620/s1.jpg"},
                    {"path_full": "https://cdn.steam/620/s2.jpg"},
                ],
            }
        }
    }
)


def test_games_work_keyless_through_steam(monkeypatch):
    use_client(
        monkeypatch,
        {"storesearch": STEAM_SEARCH, "appdetails": STEAM_DETAILS},
    )
    (candidate,) = search("game", "portal 2")["candidates"]
    assert candidate["title"] == "Portal 2"
    assert candidate["creator"] == "Valve"
    assert candidate["year"] == 2011
    assert candidate["cover_url"].endswith("/620/library_600x900_2x.jpg")
    assert candidate["source"] == "steam:620"


def test_game_chain_falls_through_a_rawg_outage(monkeypatch):
    client = use_client(
        monkeypatch,
        {
            "api.rawg.io": httpx.ConnectError("rawg is down again"),
            "storesearch": STEAM_SEARCH,
            "appdetails": STEAM_DETAILS,
        },
    )
    result = search("game", "portal 2", {"rawg": "k"})
    assert result["candidates"][0]["source"] == "steam:620"
    assert any("rawg.io" in url for url in client.calls)


def test_igdb_is_preferred_and_parses(monkeypatch):
    use_client(
        monkeypatch,
        {
            "id.twitch.tv/oauth2/token": FakeResponse(
                {"access_token": "tok", "expires_in": 5000}
            ),
            "api.igdb.com/v4/games": FakeResponse(
                [
                    {
                        "id": 1030,
                        "name": "Inside",
                        "first_release_date": 1467158400,
                        "cover": {"image_id": "co1abc"},
                        "involved_companies": [
                            {"developer": False, "company": {"name": "Publisher"}},
                            {"developer": True, "company": {"name": "Playdead"}},
                        ],
                    }
                ]
            ),
        },
    )
    media_lookup._IGDB_TOKENS.clear()
    (candidate,) = search(
        "game", "inside", {"igdb_id": "cid", "igdb_secret": "sec"}
    )["candidates"]
    assert candidate == {
        "title": "Inside",
        "creator": "Playdead",
        "year": 2016,
        "cover_url": f"{media_lookup.IGDB_IMG}/t_cover_big_2x/co1abc.jpg",
        "total": None,
        "source": "igdb:1030",
    }


def test_igdb_and_steam_backdrops(monkeypatch):
    use_client(
        monkeypatch,
        {
            "id.twitch.tv/oauth2/token": FakeResponse(
                {"access_token": "tok", "expires_in": 5000}
            ),
            "api.igdb.com/v4/games": FakeResponse(
                [
                    {
                        "artworks": [{"image_id": "ar1"}],
                        "screenshots": [{"image_id": "sc1"}, {"image_id": "sc2"}],
                    }
                ]
            ),
        },
    )
    media_lookup._IGDB_TOKENS.clear()
    urls = asyncio.run(
        media_lookup.backdrop_candidates(
            "game", "igdb:1030", {"igdb_id": "cid", "igdb_secret": "sec"}
        )
    )
    # Artworks (promo art) lead, screenshots follow.
    assert urls == [
        f"{media_lookup.IGDB_IMG}/t_1080p/ar1.jpg",
        f"{media_lookup.IGDB_IMG}/t_1080p/sc1.jpg",
        f"{media_lookup.IGDB_IMG}/t_1080p/sc2.jpg",
    ]

    use_client(monkeypatch, {"appdetails": STEAM_DETAILS})
    steam = asyncio.run(media_lookup.backdrop_candidates("game", "steam:620", {}))
    assert steam == ["https://cdn.steam/620/s1.jpg", "https://cdn.steam/620/s2.jpg"]


JPEG = b"\xff\xd8\xff" + b"x" * 100


def test_ingest_cover_writes_a_content_hashed_attachment(state, monkeypatch):
    use_client(
        monkeypatch,
        {"example.com/cover": FakeResponse(content=JPEG, content_type="image/jpeg")},
    )
    result = asyncio.run(
        media_lookup.ingest_cover(state, "https://example.com/cover", "Dune Part Two")
    )
    assert result["url"].startswith("/api/attachments/dune-part-two-")
    assert result["url"].endswith(".jpg")
    abs_path = state.store.resolve_attachment(result["path"])
    assert abs_path.read_bytes() == JPEG
    # Same bytes again dedupes to the same name.
    again = asyncio.run(
        media_lookup.ingest_cover(state, "https://example.com/cover", "Dune Part Two")
    )
    assert again["path"] == result["path"]


def test_ingest_cover_rejects_bad_input(state, monkeypatch):
    with pytest.raises(NoteError) as err:
        asyncio.run(media_lookup.ingest_cover(state, "http://example.com/c"))
    assert err.value.status == 400

    use_client(
        monkeypatch,
        {
            "example.com/page": FakeResponse(content=b"<html>", content_type="text/html"),
            "example.com/huge": FakeResponse(
                content=b"x" * (media_lookup.COVER_MAX_BYTES + 1),
                content_type="image/png",
            ),
            "example.com/gone": httpx.ConnectError("no route"),
        },
    )
    with pytest.raises(NoteError) as err:
        asyncio.run(media_lookup.ingest_cover(state, "https://example.com/page"))
    assert err.value.status == 400
    with pytest.raises(NoteError) as err:
        asyncio.run(media_lookup.ingest_cover(state, "https://example.com/huge"))
    assert err.value.status == 413
    with pytest.raises(NoteError) as err:
        asyncio.run(media_lookup.ingest_cover(state, "https://example.com/gone"))
    assert err.value.status == 502


COMMONS_PAGE = {
    "query": {
        "pages": {
            "1": {
                "index": 1,
                "imageinfo": [
                    {
                        "mime": "image/jpeg",
                        "width": 1600,
                        "height": 900,
                        "size": 500_000,
                        "url": "https://upload.wikimedia.org/wide.jpg",
                        "thumburl": "https://upload.wikimedia.org/wide-thumb.jpg",
                    }
                ],
            },
            "2": {
                "index": 2,
                "imageinfo": [
                    {
                        "mime": "image/jpeg",
                        "width": 800,
                        "height": 1200,
                        "size": 400_000,
                        "url": "https://upload.wikimedia.org/tall.jpg",
                        "thumburl": "https://upload.wikimedia.org/tall-thumb.jpg",
                    }
                ],
            },
        }
    }
}


def gallery(media_type, source, query, keys=None, **kw):
    return asyncio.run(
        media_lookup.image_candidates(media_type, source, query, keys or {}, **kw)
    )


def test_image_candidates_backdrops_vs_posters(monkeypatch):
    use_client(
        monkeypatch,
        {
            "movie/438631/images": FakeResponse(
                {
                    "backdrops": [
                        {"file_path": "/b1.jpg", "vote_count": 9, "width": 1920, "height": 1080},
                        {"file_path": "/b2.jpg", "vote_count": 2, "width": 1920, "height": 1080},
                    ],
                    "posters": [
                        {"file_path": "/p1.jpg", "vote_count": 5, "width": 800, "height": 1200}
                    ],
                }
            )
        },
    )
    backs = gallery("movie", "tmdb:movie:438631", "", {"tmdb": "k"}, kind="backdrop")
    assert [b["url"] for b in backs] == [
        "https://image.tmdb.org/t/p/w1280/b1.jpg",
        "https://image.tmdb.org/t/p/w1280/b2.jpg",
    ]
    assert backs[0]["thumb"] == "https://image.tmdb.org/t/p/w500/b1.jpg"

    covers = gallery("movie", "tmdb:movie:438631", "", {"tmdb": "k"}, kind="cover")
    assert [c["url"] for c in covers] == ["https://image.tmdb.org/t/p/w780/p1.jpg"]


def test_image_candidates_commons_aspect_by_kind(monkeypatch):
    use_client(monkeypatch, {"commons.wikimedia.org": FakeResponse(COMMONS_PAGE)})
    # Backdrops: landscape only.
    backs = gallery("book", None, "dune desert", kind="backdrop")
    assert [b["url"] for b in backs] == ["https://upload.wikimedia.org/wide.jpg"]
    assert backs[0]["thumb"] == "https://upload.wikimedia.org/wide-thumb.jpg"
    # Covers: portrait allowed.
    covers = gallery("book", None, "dune cover", kind="cover")
    assert {c["url"] for c in covers} == {
        "https://upload.wikimedia.org/wide.jpg",
        "https://upload.wikimedia.org/tall.jpg",
    }


def test_image_candidates_brave_tier_and_limit(monkeypatch):
    use_client(
        monkeypatch,
        {
            "commons.wikimedia.org": FakeResponse({"query": {"pages": {}}}),
            "api.search.brave.com": FakeResponse(
                {
                    "results": [
                        {
                            "properties": {"url": "https://img.example/a.jpg", "width": 1000},
                            "thumbnail": {"src": "https://thumb.example/a.jpg"},
                        },
                        {"properties": {"url": "http://insecure.example/b.jpg"}},
                        {
                            "properties": {"url": "https://img.example/a.jpg"},
                            "thumbnail": {"src": "https://thumb.example/dupe.jpg"},
                        },
                        {
                            "properties": {"url": "https://img.example/c.jpg"},
                            "thumbnail": {},
                        },
                    ]
                }
            ),
        },
    )
    out = gallery("game", None, "hollow knight art", {"brave": "bk"}, limit=2)
    assert [o["url"] for o in out] == [
        "https://img.example/a.jpg",
        "https://img.example/c.jpg",
    ]
    assert out[0]["origin"] == "brave"
    # Thumb falls back to the full URL when Brave has none.
    assert out[1]["thumb"] == "https://img.example/c.jpg"

    # Without the key the Brave tier is skipped silently.
    assert gallery("game", None, "hollow knight art") == []


def test_keys_round_trip_outside_the_vault(tmp_path):
    assert media_lookup.load_media_keys(tmp_path) == {}
    media_lookup.save_media_keys(tmp_path, {"tmdb": "secret-1234"})
    path = tmp_path / "media.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert media_lookup.load_media_keys(tmp_path) == {"tmdb": "secret-1234"}
    path.write_text("not json", encoding="utf-8")
    assert media_lookup.load_media_keys(tmp_path) == {}
