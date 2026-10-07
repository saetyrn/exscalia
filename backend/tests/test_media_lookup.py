"""Metadata providers (canned responses), cover ingestion, and key storage."""
import asyncio
import hashlib
import io
import json
import logging
import stat

import httpx
import pytest

from app import media_lookup
from app.vault.notes import NoteError


class FakeResponse:
    def __init__(
        self,
        payload=None,
        *,
        content=b"",
        content_type="application/json",
        status=200,
        headers=None,
    ):
        self._payload = payload
        self.content = content
        self.headers = {"content-type": content_type, **(headers or {})}
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
    """Routes URL fragments to canned responses; unmatched URLs fail loudly.

    A route may also be a list (consumed in order, one per request) or a
    callable taking the URL. Every request lands in `requests` as
    (method, url, kwargs); `calls` keeps just the URLs.
    """

    def __init__(self, routes):
        self.routes = routes
        self.calls = []
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def _respond(self, method, url, kwargs):
        self.calls.append(url)
        self.requests.append((method, url, kwargs))
        matches = [candidate for fragment, candidate in self.routes.items() if fragment in url]
        if not matches:
            raise AssertionError(f"unexpected URL: {url}")
        response = matches[0]
        if isinstance(response, list):
            response = response.pop(0)
        elif callable(response):
            response = response(url)
        if isinstance(response, Exception):
            raise response
        return response

    async def get(self, url, **kwargs):
        return self._respond("GET", url, kwargs)

    async def post(self, url, **kwargs):
        return self._respond("POST", url, kwargs)

    def stream(self, method, url, **kwargs):
        return FakeStream(self._respond(method, url, kwargs))


def use_client(monkeypatch, routes) -> FakeClient:
    """Swap in the fake client; every host resolves to a public address so
    image downloads stay offline (tests of private hosts patch it again)."""
    client = FakeClient(routes)
    monkeypatch.setattr(media_lookup, "_client", lambda: client)

    async def public(host):
        return ["93.184.216.34"]

    monkeypatch.setattr(media_lookup, "_resolve_addresses", public)
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


def commons_hits(*names):
    """A Commons search page of wide, ingestible originals, in rank order."""
    return FakeResponse(
        {
            "query": {
                "pages": {
                    str(rank): {
                        "index": rank,
                        "imageinfo": [
                            {
                                "size": 500_000,
                                "width": 3000,
                                "height": 2000,
                                "mime": "image/jpeg",
                                "url": f"https://img/{name}.jpg",
                            }
                        ],
                    }
                    for rank, name in enumerate(names, 1)
                }
            }
        }
    )


def test_book_backdrops_keep_the_queries_that_succeeded(monkeypatch, caplog):
    client = use_client(
        monkeypatch,
        {
            "commons.wikimedia.org": [
                commons_hits("a1", "a2"),
                keyed_failure(429, "https://commons.wikimedia.org/w/api.php"),
                commons_hits("c1", "c2"),
            ]
        },
    )

    urls = asyncio.run(
        media_lookup.book_backdrop_candidates(["factory floor", "launch pad", "desert road"])
    )

    assert len(client.calls) == 3
    assert urls == [
        "https://img/a1.jpg",
        "https://img/c1.jpg",
        "https://img/a2.jpg",
        "https://img/c2.jpg",
    ]
    assert "'launch pad': HTTP 429 from commons.wikimedia.org" in caplog.text


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


IGDB_GAME = FakeResponse(
    [
        {
            "id": 1030,
            "name": "Inside",
            "cover": {"image_id": "co1abc"},
            "involved_companies": [{"developer": True, "company": {"name": "Playdead"}}],
        }
    ]
)
IGDB_KEYS = {"igdb_id": "cid", "igdb_secret": "sec"}


def bearer_tokens(client):
    return [
        kw["headers"]["Authorization"]
        for _m, url, kw in client.requests
        if "api.igdb.com" in url
    ]


def test_igdb_recovers_from_a_revoked_token(monkeypatch):
    client = use_client(
        monkeypatch,
        {
            "id.twitch.tv/oauth2/token": [
                FakeResponse({"access_token": "old", "expires_in": 5000}),
                FakeResponse({"access_token": "new", "expires_in": 5000}),
            ],
            "api.igdb.com/v4/games": [
                keyed_failure(401, "https://api.igdb.com/v4/games", method="POST"),
                IGDB_GAME,
            ],
        },
    )
    media_lookup._IGDB_TOKENS.clear()

    (candidate,) = search("game", "inside", IGDB_KEYS)["candidates"]

    assert candidate["source"] == "igdb:1030"
    assert candidate["creator"] == "Playdead"
    assert sum("id.twitch.tv" in url for url in client.calls) == 2
    assert bearer_tokens(client) == ["Bearer old", "Bearer new"]
    assert [token for token, _exp in media_lookup._IGDB_TOKENS.values()] == ["new"]


def test_igdb_token_cache_misses_on_a_rotated_secret(monkeypatch):
    client = use_client(
        monkeypatch,
        {
            "id.twitch.tv/oauth2/token": [
                FakeResponse({"access_token": "tok1", "expires_in": 5000}),
                FakeResponse({"access_token": "tok2", "expires_in": 5000}),
            ],
            "api.igdb.com/v4/games": FakeResponse([{"artworks": [{"image_id": "ar1"}]}]),
        },
    )
    media_lookup._IGDB_TOKENS.clear()
    rotated = {"igdb_id": "cid", "igdb_secret": "sec2"}

    for keys in (IGDB_KEYS, rotated, rotated):
        assert asyncio.run(media_lookup.backdrop_candidates("game", "igdb:1030", keys))

    assert sum("id.twitch.tv" in url for url in client.calls) == 2
    assert bearer_tokens(client) == ["Bearer tok1", "Bearer tok2", "Bearer tok2"]


@pytest.mark.parametrize(("status", "igdb_calls"), [(500, 1), (401, 2)])
def test_igdb_retries_only_one_401_then_falls_through(monkeypatch, status, igdb_calls):
    client = use_client(
        monkeypatch,
        {
            "id.twitch.tv/oauth2/token": FakeResponse({"access_token": "tok", "expires_in": 5000}),
            "api.igdb.com/v4/games": keyed_failure(
                status, "https://api.igdb.com/v4/games", method="POST"
            ),
            "storesearch": STEAM_SEARCH,
            "appdetails": STEAM_DETAILS,
        },
    )
    media_lookup._IGDB_TOKENS.clear()

    result = search("game", "portal 2", IGDB_KEYS)

    assert result["candidates"][0]["source"] == "steam:620"
    assert sum("api.igdb.com" in url for url in client.calls) == igdb_calls
    assert sum("id.twitch.tv" in url for url in client.calls) == igdb_calls


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
    # A short file under that name is a write killed part-way: the next
    # download of the same bytes repairs it instead of trusting it.
    abs_path.write_bytes(JPEG[:5])
    asyncio.run(
        media_lookup.ingest_cover(state, "https://example.com/cover", "Dune Part Two")
    )
    assert abs_path.read_bytes() == JPEG
    assert [p.name for p in abs_path.parent.iterdir()] == [abs_path.name]


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


def ingest_error(state, url) -> NoteError:
    with pytest.raises(NoteError) as err:
        asyncio.run(media_lookup.ingest_cover(state, url))
    return err.value


def test_ingest_cover_checks_every_redirect_hop(state, monkeypatch):
    client = use_client(
        monkeypatch,
        {
            "cdn.example/moved": FakeResponse(
                status=302, headers={"location": "http://127.0.0.1:8000/internal.png"}
            ),
            "cdn.example/relative": FakeResponse(
                status=301, headers={"location": "/final.jpg"}
            ),
            "cdn.example/final.jpg": FakeResponse(content=JPEG, content_type="image/jpeg"),
            "127.0.0.1": FakeResponse(content=JPEG, content_type="image/jpeg"),
        },
    )
    # An https URL that bounces to plain http on loopback is refused at the
    # hop; the internal URL is never requested.
    assert ingest_error(state, "https://cdn.example/moved").status == 400
    assert not any("127.0.0.1" in url for url in client.calls)
    # Redirects are followed by hand, one checked hop at a time.
    assert all(
        kwargs.get("follow_redirects") is False
        for method, _url, kwargs in client.requests
        if method == "GET"
    )
    result = asyncio.run(media_lookup.ingest_cover(state, "https://cdn.example/relative"))
    assert result["path"].endswith(".jpg")
    assert client.calls[-1] == "https://cdn.example/final.jpg"


def test_ingest_cover_refuses_private_hosts(state, monkeypatch):
    client = use_client(
        monkeypatch,
        {"": FakeResponse(content=JPEG, content_type="image/jpeg")},
    )
    for literal in (
        "https://127.0.0.1/c.jpg",
        "https://169.254.169.254/latest/meta-data",
        "https://[::1]/c.jpg",
    ):
        err = ingest_error(state, literal)
        assert (err.status, err.detail) == (400, "Image host is not public")

    async def lan(host):
        return ["93.184.216.34", "10.0.0.1"]

    monkeypatch.setattr(media_lookup, "_resolve_addresses", lan)
    err = ingest_error(state, "https://sneaky.example/c.jpg")
    assert (err.status, err.detail) == (400, "Image host is not public")
    assert client.calls == []

    async def unresolvable(host):
        raise OSError("nodename nor servname provided")

    monkeypatch.setattr(media_lookup, "_resolve_addresses", unresolvable)
    assert ingest_error(state, "https://nowhere.example/c.jpg").status == 502
    assert client.calls == []


def test_ingest_cover_redirects_through_real_httpx(state, monkeypatch):
    """The same rules on a real AsyncClient (offline transport, real resolver
    for localhost), so the fake's shape cannot hide an httpx mismatch."""
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.path == "/moved":
            return httpx.Response(302, headers={"Location": "/cover.png"})
        if request.url.path == "/downgrade":
            return httpx.Response(302, headers={"Location": "http://cdn.example/x.png"})
        return httpx.Response(200, headers={"Content-Type": "image/png"}, content=b"png")

    def client():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)

    async def public(host):
        return ["93.184.216.34"]

    real_resolver = media_lookup._resolve_addresses
    monkeypatch.setattr(media_lookup, "_client", client)
    monkeypatch.setattr(media_lookup, "_resolve_addresses", public)
    result = asyncio.run(media_lookup.ingest_cover(state, "https://cdn.example/moved"))
    assert result["path"].endswith(".png")
    assert seen == ["https://cdn.example/moved", "https://cdn.example/cover.png"]

    seen.clear()
    assert ingest_error(state, "https://cdn.example/downgrade").status == 400
    assert seen == ["https://cdn.example/downgrade"]

    monkeypatch.setattr(media_lookup, "_resolve_addresses", real_resolver)
    seen.clear()
    err = ingest_error(state, "https://localhost/c.png")
    assert (err.status, err.detail) == (400, "Image host is not public")
    assert seen == []


def test_ingest_cover_refuses_stacked_content_encodings(state, monkeypatch):
    """httpx decodes every layer of a chunk in one call, before any size cap
    runs: a few KB of gzip inside gzip is gigabytes in memory. A recipe
    page's image URL is the page author's choice, so this was a remote OOM."""
    import gzip

    bomb = gzip.compress(gzip.compress(b"\0" * (64 * 1024 * 1024), 9), 9)
    assert len(bomb) < 100_000

    async def streamed():  # as off the network: nothing decodes it but the reader
        yield bomb

    def handler(request):
        return httpx.Response(
            200,
            headers={"Content-Type": "image/png", "Content-Encoding": "gzip, gzip"},
            content=streamed(),
        )

    async def public(host):
        return ["93.184.216.34"]

    decoded = []
    real_decode = httpx._decoders.GZipDecoder.decode
    monkeypatch.setattr(
        httpx._decoders.GZipDecoder,
        "decode",
        lambda self, data: decoded.append(1) or real_decode(self, data),
    )
    monkeypatch.setattr(
        media_lookup, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(media_lookup, "_resolve_addresses", public)
    err = ingest_error(state, "https://cdn.example/bomb.png")
    assert (err.status, err.detail) == (502, "Cover download failed: encoding")
    assert decoded == []


def test_ingest_cover_answers_a_redirect_to_a_host_that_is_not_idna(state, monkeypatch):
    """httpx reads a redirect's host as IDNA before it answers; the
    UnicodeError from "xn--localhost" escaped as a 500."""

    def handler(request):
        return httpx.Response(302, headers={"Location": "https://xn--localhost/c.png"})

    async def public(host):
        return ["93.184.216.34"]

    monkeypatch.setattr(
        media_lookup, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr(media_lookup, "_resolve_addresses", public)
    assert ingest_error(state, "https://cdn.example/c.png").status == 502
    assert ingest_error(state, "https://xn--localhost/c.png").status == 400


def test_ingest_cover_caps_redirects(state, monkeypatch):
    hops = iter(range(10))
    use_client(
        monkeypatch,
        {
            "cdn.example": lambda url: FakeResponse(
                status=302, headers={"location": f"https://cdn.example/{next(hops)}"}
            )
        },
    )
    err = ingest_error(state, "https://cdn.example/start")
    assert (err.status, err.detail) == (502, "Too many redirects")
    # The first request plus three followed redirects, then it stops.
    assert next(hops) == media_lookup.COVER_MAX_REDIRECTS + 1


def test_ingest_cover_has_one_deadline(state, monkeypatch):
    class Trickle(FakeResponse):
        async def aiter_bytes(self):
            for _ in range(50):
                await asyncio.sleep(0.01)
                yield b"x"

    use_client(monkeypatch, {"cdn.example": Trickle(content_type="image/png")})
    monkeypatch.setattr(media_lookup, "COVER_TIMEOUT", 0.05)
    assert ingest_error(state, "https://cdn.example/slow.png").status == 504


def image_bytes(size, fmt) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, (90, 120, 150)).save(buf, format=fmt)
    return buf.getvalue()


def stored_image(state, result):
    from PIL import Image

    data = state.store.resolve_attachment(result["path"]).read_bytes()
    with Image.open(io.BytesIO(data)) as image:
        return data, image.format, image.size


def store(state, data, ctype, hint="Dune-backdrop", **kw):
    return asyncio.run(media_lookup.store_image_bytes(state, data, ctype, hint, **kw))


def test_stored_art_is_downscaled_by_kind(state):
    # A Commons original as a backdrop: re-encoded to the hero's size.
    result = store(state, image_bytes((3000, 1200), "PNG"), "image/png", kind="backdrop")
    data, fmt, size = stored_image(state, result)
    assert result["path"].endswith(".jpg") and fmt == "JPEG"
    assert size == (2400, 960)
    # The name hashes the bytes actually written.
    assert result["path"].endswith(f"-{hashlib.sha256(data).hexdigest()[:16]}.jpg")

    # Covers cap lower.
    result = store(state, image_bytes((1500, 2250), "JPEG"), "image/jpeg", "Dune")
    _data, fmt, size = stored_image(state, result)
    assert fmt == "JPEG" and max(size) == 1000 and size[0] <= 1000


def test_stored_art_keeps_small_and_animated_images_as_they_are(state):
    small = image_bytes((900, 600), "PNG")
    result = store(state, small, "image/png", kind="backdrop")
    data, fmt, size = stored_image(state, result)
    assert result["path"].endswith(".png") and data == small

    gif = image_bytes((3000, 1200), "GIF")
    result = store(state, gif, "image/gif", kind="backdrop")
    assert result["path"].endswith(".gif")
    assert state.store.resolve_attachment(result["path"]).read_bytes() == gif

    for data, ctype in ((b"", "image/png"), (small, "text/html")):
        with pytest.raises(NoteError) as err:
            store(state, data, ctype)
        assert err.value.status == 400


def test_ingest_cover_stores_by_kind(state, monkeypatch):
    wide = image_bytes((3000, 1200), "PNG")
    use_client(monkeypatch, {"cdn.example": FakeResponse(content=wide, content_type="image/png")})
    backdrop = asyncio.run(
        media_lookup.ingest_cover(
            state, "https://cdn.example/w.png", "Dune-backdrop", kind="backdrop"
        )
    )
    cover = asyncio.run(media_lookup.ingest_cover(state, "https://cdn.example/w.png", "Dune"))
    assert stored_image(state, backdrop)[2] == (2400, 960)
    assert stored_image(state, cover)[2] == (1000, 400)


def keyed_failure(status, url, method="GET"):
    return httpx.Response(status, request=httpx.Request(method, url))


def test_provider_keys_never_reach_errors_or_logs(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger="app.media_lookup")
    tmdb_404 = keyed_failure(404, "https://api.themoviedb.org/3/movie/999?api_key=SENTINEL123")
    use_client(monkeypatch, {"api.themoviedb.org": tmdb_404})
    errors = [
        search("movie", "dune", {"tmdb": "SENTINEL123"})["error"],
        asyncio.run(
            media_lookup.media_details("movie", "tmdb:movie:999", None, {"tmdb": "SENTINEL123"})
        )["error"],
        asyncio.run(
            media_lookup.similar_media("movie", "tmdb:movie:999", {"tmdb": "SENTINEL123"})
        )["error"],
    ]
    asyncio.run(media_lookup.movie_attribution("tmdb:movie:999", {"tmdb": "SENTINEL123"}))
    asyncio.run(
        media_lookup.backdrop_candidates("movie", "tmdb:movie:999", {"tmdb": "SENTINEL123"})
    )
    assert errors[0] == "Lookup failed: HTTP 404 from api.themoviedb.org"
    assert errors[1] == "Details lookup failed: HTTP 404 from api.themoviedb.org"
    assert errors[2] == "Recommendation lookup failed: HTTP 404 from api.themoviedb.org"

    # A failed Twitch exchange must not echo the IGDB client secret either.
    media_lookup._IGDB_TOKENS.clear()
    use_client(
        monkeypatch,
        {
            "id.twitch.tv": keyed_failure(
                400,
                "https://id.twitch.tv/oauth2/token?client_secret=SENTINEL123",
                method="POST",
            ),
            "storesearch": FakeResponse({"items": []}),
        },
    )
    game = search("game", "inside", {"igdb_id": "cid", "igdb_secret": "SENTINEL123"})
    assert game["error"] == "IGDB: HTTP 400 from id.twitch.tv; Steam: no matches"
    errors.append(game["error"])

    assert not any("SENTINEL123" in error for error in errors)
    assert "SENTINEL123" not in caplog.text
    assert "api.themoviedb.org" in caplog.text


def test_twitch_credentials_go_in_the_form_body(monkeypatch):
    media_lookup._IGDB_TOKENS.clear()
    client = use_client(
        monkeypatch,
        {
            "id.twitch.tv/oauth2/token": FakeResponse({"access_token": "tok"}),
            "api.igdb.com/v4/games": FakeResponse([]),
            "storesearch": FakeResponse({"items": []}),
        },
    )
    search("game", "inside", {"igdb_id": "cid", "igdb_secret": "sec"})
    (token_call,) = [kw for _m, url, kw in client.requests if "id.twitch.tv" in url]
    assert token_call["data"]["client_secret"] == "sec"
    assert "params" not in token_call


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


def test_art_that_declares_too_many_pixels_is_refused_before_it_is_decoded():
    """A small file can declare a huge canvas (a recipe page's image URL is
    the author's to choose): it is neither decoded to shrink nor stored."""
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("1", (8000, 8000)).save(buffer, format="PNG")  # 64 MP in a few KB
    bomb = buffer.getvalue()
    assert len(bomb) < 100_000
    with pytest.raises(NoteError) as refused:
        media_lookup._shrink_for_storage(bomb, "image/png", 1600)
    assert refused.value.status == 400

    buffer = io.BytesIO()
    Image.new("RGB", (3000, 2000), "white").save(buffer, format="PNG")
    data, ctype = media_lookup._shrink_for_storage(buffer.getvalue(), "image/png", 1600)
    assert ctype == "image/jpeg"
    with Image.open(io.BytesIO(data)) as shrunk:
        assert max(shrunk.size) == 1600
