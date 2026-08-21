"""/api/media: grid queries, the timeline, and quick note-rewrite mutations."""
import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import media as media_api
from app.api import notes as notes_api
from app.vault.notes import NoteError

TZ = "America/New_York"


def make_client(state, *routers) -> TestClient:
    app = FastAPI()
    app.state = state
    app.state.timezone = TZ

    @app.exception_handler(NoteError)
    async def handler(_request, exc: NoteError):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=exc.status, content={"detail": exc.detail})

    for router in routers:
        app.include_router(router)
    return TestClient(app)


def create(client, title, media_type, **kw):
    resp = client.post(
        "/api/media", json={"title": title, "media_type": media_type, **kw}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_create_with_review_writes_the_body_above_the_log(state):
    from app import media_items

    client = make_client(state, media_api.router)
    data = create(client, "Reviewed Book", "book", status="finished", review="Loved it.", rating=8)
    content = state.store.read(data["path"])
    assert "Loved it." in content
    assert content.index("Loved it.") < content.index(media_items.LOG_HEADING)


def test_match_endpoint_detects_a_tracked_title(state):
    client = make_client(state, media_api.router)
    assert client.get("/api/media/match", params={"title": "Dune"}).json()["tracked"] is False

    create(client, "Dune", "book", status="finished")
    matched = client.get("/api/media/match", params={"title": "Dune"}).json()
    assert matched["tracked"] is True
    assert matched["item"]["title"] == "Dune"
    assert matched["plays"] >= 1  # the finished status seeded a finished log entry


def test_create_sanitizes_path_and_keeps_display_title(state):
    client = make_client(state, media_api.router)
    data = create(
        client,
        "Elon Musk: A Biography",
        "book",
        status="active",
        creator="Walter Isaacson",
        year=2023,
        total=688,
    )
    assert data["path"] == "media/Elon Musk A Biography.md"
    item = data["item"]
    assert item["title"] == "Elon Musk: A Biography"
    assert item["media_type"] == "book"
    assert item["status"] == "active"
    assert item["creator"] == "Walter Isaacson"
    assert item["year"] == 2023
    # An active item starts its log on day one.
    content = state.store.read(data["path"])
    assert "## Log" in content and "— started" in content

    # Same title again steps past the taken name.
    again = create(client, "Elon Musk: A Biography", "book")
    assert again["path"] == "media/Elon Musk A Biography 2.md"


def test_create_rejects_garbage(state):
    client = make_client(state, media_api.router)
    assert client.post(
        "/api/media", json={"title": "  ", "media_type": "book"}
    ).status_code == 400
    assert client.post(
        "/api/media", json={"title": "x", "media_type": "podcast"}
    ).status_code == 400
    assert client.post(
        "/api/media", json={"title": "x", "media_type": "book", "rating": 11}
    ).status_code == 400


def test_grid_filters_and_sorts(state):
    client = make_client(state, media_api.router)
    create(client, "Dune", "book", status="finished", rating=9, year=1965)
    create(client, "Alien", "movie", status="finished", rating=8, year=1979)
    create(client, "Severance", "tv", status="active", year=2022)

    all_items = client.get("/api/media").json()
    assert len(all_items) == 3

    books = client.get("/api/media?type=book").json()
    assert [i["title"] for i in books] == ["Dune"]

    active = client.get("/api/media?status=active").json()
    assert [i["title"] for i in active] == ["Severance"]

    by_rating = client.get("/api/media?sort=rating").json()
    # Unrated items sink below the lowest rating.
    assert [i["title"] for i in by_rating] == ["Dune", "Alien", "Severance"]

    by_title = client.get("/api/media?sort=title").json()
    assert [i["title"] for i in by_title] == ["Alien", "Dune", "Severance"]

    assert client.get("/api/media?type=podcast").status_code == 400
    assert client.get("/api/media?sort=chaos").status_code == 400
    assert client.get("/api/media?sort=year&dir=sideways").status_code == 400


def test_log_progress_rating_and_finish_flow(state):
    client = make_client(state, media_api.router)
    created = create(client, "Dune", "book", status="active", total=412)
    path = created["path"]

    logged = client.post(f"/api/media/{path}/log", json={"kind": "progress", "value": 120})
    assert logged.status_code == 200, logged.text
    assert "page 120" in logged.json()["content"]
    assert logged.json()["summary"] == "Logged page 120"
    assert logged.json()["item"]["progress"] == "page 120"

    listed = client.get("/api/media").json()
    assert listed[0]["progress"] == "page 120"

    rated = client.patch(f"/api/media/{path}", json={"rating": 9})
    assert rated.status_code == 200
    assert rated.json()["item"]["rating"] == 9

    finished = client.patch(f"/api/media/{path}", json={"status": "finished", "rating": 9})
    assert finished.status_code == 200
    item = finished.json()["item"]
    assert item["status"] == "finished"
    assert item["finished"]  # stamped today
    assert "finished, rated 9" in finished.json()["content"]

    timeline = client.get("/api/media/timeline").json()
    kinds = [e["kind"] for e in timeline]
    assert "started" in kinds and "progress" in kinds and "finished" in kinds
    assert all(e["title"] == "Dune" for e in timeline)


def test_stale_revision_is_rejected(state):
    client = make_client(state, media_api.router)
    path = create(client, "Dune", "book")["path"]
    stale = client.patch(
        f"/api/media/{path}", json={"rating": 5, "base_revision": "not-the-revision"}
    )
    assert stale.status_code == 409

    missing = client.patch("/api/media/media/Nope.md", json={"rating": 5})
    assert missing.status_code == 404

    plain = state.store.create("plain.md", None)
    ordinary = client.patch("/api/media/plain.md", json={"rating": 5})
    assert ordinary.status_code == 400


def test_delete_log_entry_removes_one_watch(state):
    client = make_client(state, media_api.router)
    path = create(client, "Akira", "movie", status="finished", rating=8)["path"]
    # Two watches now: the finish + a rewatch.
    client.post(f"/api/media/{path}/log", json={"kind": "watched"})

    events = client.get(f"/api/media/{path}").json()["events"]
    watches = [e for e in events if e["kind"] in ("watched", "finished")]
    assert len(watches) == 2

    # Delete the plain rewatch, matched by its date + text.
    watched = next(e for e in events if e["kind"] == "watched")
    deleted = client.post(
        f"/api/media/{path}/log/delete",
        json={"at": watched["at"], "text": watched["text"]},
    )
    assert deleted.status_code == 200, deleted.text
    assert "Removed" in deleted.json()["summary"]

    left = client.get(f"/api/media/{path}").json()["events"]
    assert [e["kind"] for e in left if e["kind"] in ("watched", "finished")] == ["finished"]

    # A no-longer-present entry is a clean 400, not a 500.
    missing = client.post(
        f"/api/media/{path}/log/delete", json={"at": watched["at"], "text": "watched"}
    )
    assert missing.status_code == 400


def test_alias_sets_display_name_without_touching_title(state):
    client = make_client(state, media_api.router)
    path = create(client, "The Lord of the Rings", "movie", status="finished", rating=9)["path"]

    aliased = client.patch(f"/api/media/{path}", json={"alias": "  LOTR 1  "})
    assert aliased.status_code == 200, aliased.text
    item = aliased.json()["item"]
    # Alias is stored (trimmed); the real title is left alone.
    assert item["alias"] == "LOTR 1"
    assert item["title"] == "The Lord of the Rings"
    assert "alias: LOTR 1" in aliased.json()["content"]

    # The diary/timeline surfaces the alias in place of the title.
    timeline = client.get("/api/media/timeline").json()
    assert timeline and all(e["title"] == "LOTR 1" for e in timeline)

    # An empty string clears it back to the real title.
    cleared = client.patch(f"/api/media/{path}", json={"alias": ""})
    assert cleared.json()["item"]["alias"] is None
    assert "alias:" not in cleared.json()["content"]
    assert client.get("/api/media/timeline").json()[0]["title"] == "The Lord of the Rings"


def test_media_folder_is_protected(state):
    client = make_client(state, media_api.router, notes_api.router)
    create(client, "Dune", "book")
    renamed = client.post(
        "/api/notes/folders/rename", json={"path": "media", "new_path": "stuff"}
    )
    assert renamed.status_code == 400
    deleted = client.delete("/api/notes/folders/media")
    assert deleted.status_code == 400


def test_settings_store_hints_not_keys(state):
    client = make_client(state, media_api.router)
    empty = client.get("/api/media/settings").json()
    assert empty == {
        "tmdb": {"set": False, "hint": None},
        "rawg": {"set": False, "hint": None},
        "igdb_id": {"set": False, "hint": None},
        "igdb_secret": {"set": False, "hint": None},
        "brave": {"set": False, "hint": None},
    }
    saved = client.put("/api/media/settings", json={"tmdb": "secret-key-1234"}).json()
    assert saved["tmdb"] == {"set": True, "hint": "…1234"}
    assert saved["rawg"]["set"] is False
    # None leaves a key untouched; empty string clears it.
    kept = client.put("/api/media/settings", json={"rawg": "abcd"}).json()
    assert kept["tmdb"]["set"] is True and kept["rawg"]["set"] is True
    cleared = client.put("/api/media/settings", json={"tmdb": ""}).json()
    assert cleared["tmdb"]["set"] is False and cleared["rawg"]["set"] is True
    # Keys live outside the vault.
    assert not (state.settings.vault_dir / "media.json").exists()
    assert (state.settings.data_dir / "media.json").is_file()


def test_lookup_route_validates_and_reports_missing_keys(state):
    client = make_client(state, media_api.router)
    assert client.get("/api/media/lookup?type=podcast&q=x").status_code == 400
    result = client.get("/api/media/lookup?type=movie&q=dune").json()
    assert result["candidates"] == []
    assert "TMDB API key not set" in result["error"]


def test_create_degrades_when_cover_ingest_fails(state, monkeypatch):
    from app import media_lookup
    from app.vault.notes import NoteError as NE

    async def failing_ingest(_state, _url, _hint="cover"):
        raise NE(502, "Cover download failed: no route")

    monkeypatch.setattr(media_lookup, "ingest_cover", failing_ingest)
    client = make_client(state, media_api.router)
    data = create(client, "Dune", "book", cover_url="https://example.com/cover.jpg")
    assert data["item"]["cover"] is None
    assert "Cover download failed" in data["cover_error"]


def test_review_editing_preserves_the_log(state):
    client = make_client(state, media_api.router)
    path = create(client, "Dune", "book", status="active")["path"]
    client.post(f"/api/media/{path}/log", json={"kind": "progress", "value": 120})

    saved = client.put(
        f"/api/media/{path}/review", json={"review": "A desert **classic**."}
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["summary"] == "Review updated"
    content = saved.json()["content"]
    assert "A desert **classic**." in content
    assert "## Log" in content and "page 120" in content
    assert content.index("classic") < content.index("## Log")

    # Clearing the review keeps the log intact.
    cleared = client.put(f"/api/media/{path}/review", json={"review": ""})
    body = cleared.json()["content"].split("---")[-1]
    assert body.strip().startswith("## Log")

    plain = state.store.create("plain2.md", None)
    assert client.put("/api/media/plain2.md/review", json={"review": "x"}).status_code == 400


def test_hub_payload_and_stats(state):
    client = make_client(state, media_api.router)
    create(client, "Planned Book", "book", status="planned")
    create(client, "Old Finish", "movie", status="finished", rating=6)
    active = create(client, "Reading Now", "book", status="active")
    client.post(
        f"/api/media/{active['path']}/log", json={"kind": "progress", "value": 120}
    )

    # Backdate one finish to last year so this-year math is real.
    import asyncio as _asyncio

    from app.index.db import content_hash as _hash
    from app.vault import fm as _fm

    content = state.store.read("media/Old Finish.md")
    meta, body = _fm.parse(content)
    meta["finished"] = "2001-05-05"
    state.store.write("media/Old Finish.md", _fm.serialize(meta, body))
    _asyncio.run(
        state.indexer.index_note(
            "media/Old Finish.md", state.store.read("media/Old Finish.md")
        )
    )
    create(client, "New Finish", "tv", status="finished", rating=9)
    # A finished book with a page count feeds the "pages read" stat this year.
    create(client, "Doorstop", "book", status="finished", rating=7, total=800)

    hub = client.get("/api/media/hub").json()
    assert hub["profile"]["name"] == "owner"  # no auth on the test state
    assert hub["profile"]["avatar"] == "" and hub["profile"]["location"] == ""
    assert hub["stats"] == {
        "watchlist": 1,
        # Pages = 800 (finished Doorstop) + 120 (in-progress Reading Now), both
        # periods. This year also counts the tv finish; the movie finished 2001.
        "this_year": {"pages": 920, "movies": 0, "shows": 1, "games": 0},
        # Lifetime adds the backdated movie finish.
        "lifetime": {"pages": 920, "movies": 1, "shows": 1, "games": 0},
    }
    assert [w["title"] for w in hub["watchlist"]] == ["Planned Book"]
    # Histogram: ratings 6, 9, and 7 among the fixtures.
    assert hub["ratings"]["total"] == 3
    assert (
        hub["ratings"]["counts"][5] == 1
        and hub["ratings"]["counts"][8] == 1
        and hub["ratings"]["counts"][6] == 1
    )
    # Recent activity dedupes to distinct items, newest first.
    recent_titles = [r["item"]["title"] for r in hub["recent"]]
    assert recent_titles[0] in ("New Finish", "Reading Now", "Doorstop")
    assert len(recent_titles) == len(set(recent_titles))
    assert hub["favorites"] == []
    assert all("title" in d and "at" in d for d in hub["diary"])


def test_profile_round_trip_and_validation(state):
    import json as _json

    client = make_client(state, media_api.router)
    a = create(client, "Fav A", "book")["path"]
    b = create(client, "Fav B", "movie")["path"]

    saved = client.put(
        "/api/media/profile",
        json={
            "name": "  daytona  ",
            "location": "LONDON",
            "favorites": [b, a],
        },
    )
    assert saved.status_code == 200, saved.text
    data = saved.json()
    assert data["profile"]["name"] == "daytona"
    assert data["profile"]["location"] == "LONDON"
    # Order is the curated order, not alphabetical or recency.
    assert [f["title"] for f in data["favorites"]] == ["Fav B", "Fav A"]

    # Persisted under media_profile in vault-internal config.
    config = _json.loads(
        (state.settings.vault_dir / ".exscalia" / "config.json").read_text()
    )
    assert config["media_profile"]["favorites"] == [b, a]

    # Partial update leaves other fields alone; empty string clears.
    kept = client.put("/api/media/profile", json={"location": ""}).json()
    assert kept["profile"]["name"] == "daytona"
    assert kept["profile"]["location"] == ""

    # Deleted favorites are filtered on read, not 500s.
    client.request("DELETE", f"/api/notes/{a}")  # not mounted here; simulate below
    state.store.resolve(a).unlink()
    import asyncio as _asyncio

    _asyncio.run(state.indexer.remove_note(a))
    hub = client.get("/api/media/hub").json()
    assert [f["title"] for f in hub["favorites"]] == ["Fav B"]

    # Validation: cap, unknown items, non-attachment avatars.
    assert client.put(
        "/api/media/profile", json={"favorites": [b] * 3 + ["media/x.md", "media/y.md"]}
    ).status_code == 400
    assert client.put(
        "/api/media/profile", json={"favorites": ["media/Nope.md"]}
    ).status_code == 400
    assert client.put(
        "/api/media/profile", json={"avatar": "https://evil.example/x.jpg"}
    ).status_code == 400
    ok = client.put(
        "/api/media/profile", json={"avatar": "/api/attachments/me-abcd.png"}
    )
    assert ok.status_code == 200
    assert ok.json()["profile"]["avatar"] == "/api/attachments/me-abcd.png"


def test_timeline_limit_is_clamped(state):
    client = make_client(state, media_api.router)
    path = create(client, "Dune", "book", status="active")["path"]
    for day in range(1, 4):
        client.post(
            f"/api/media/{path}/log",
            json={"kind": "progress", "value": day, "date": f"2026-08-0{day}"},
        )
    events = client.get("/api/media/timeline?limit=2").json()
    assert len(events) == 2
    # Newest first.
    assert events[0]["at"] >= events[1]["at"]
