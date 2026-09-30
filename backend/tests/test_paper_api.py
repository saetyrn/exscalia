"""/api/paper: the wire contract the iPad client is built against.

These tests pin the things a native client cannot work around — how a page is
uploaded, what a 409 body carries, which URLs may be cached forever, and what
the change feed says when a cursor is no longer usable.
"""

import asyncio
import hashlib
import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.api import paper as paper_api
from app.api import overseer as overseer_api
from app.overseer.threads import ThreadStore
from app.vault.notes import NoteError

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32
IMMUTABLE = "private, max-age=31536000, immutable"
NO_CACHE = "private, no-cache"


def etag_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


def make_client(state) -> TestClient:
    app = FastAPI()
    app.state = state

    @app.exception_handler(NoteError)
    async def handler(_request, exc: NoteError):
        return JSONResponse(status_code=exc.status, content={"detail": exc.detail})

    app.include_router(paper_api.router)
    return TestClient(app)


def new_notebook(client, title="Ideas", **body) -> dict:
    response = client.post("/api/paper/notebooks", json={"title": title, **body})
    assert response.status_code == 200, response.text
    return response.json()


def put_page(client, notebook, page_id, base_revision, **parts):
    """The multipart save, as the client sends it."""
    data = {"base_revision": str(base_revision)}
    for key in ("for_revision", "client_id", "after"):
        if key in parts:
            data[key] = str(parts.pop(key))
    files = {}
    if "page" in parts:
        files["page"] = ("page.json", json.dumps(parts.pop("page")), "application/json")
    if "text" in parts:
        files["text"] = ("text.json", json.dumps(parts.pop("text")), "application/json")
    if "drawing" in parts:
        files["drawing"] = (
            "drawing.pkdrawing",
            parts.pop("drawing"),
            "application/octet-stream",
        )
    if "render" in parts:
        files["render"] = ("render.png", parts.pop("render"), "image/png")
    assert not parts, parts
    return client.put(
        f"/api/paper/pages/{notebook}/{page_id}", data=data, files=files
    )


# -- routing -----------------------------------------------------------------


def test_literal_routes_are_not_swallowed_by_the_notebook_path(state):
    """`{notebook:path}` is greedy; `rename` and `pages` must still win."""
    client = make_client(state)
    nb = new_notebook(client, "Ideas", folder="Work")

    created = client.post(f"/api/paper/notebooks/{nb['path']}/pages", json={})
    assert created.status_code == 200, created.text
    page_id = created.json()["id"]

    renamed = client.post(
        f"/api/paper/notebooks/{nb['path']}/rename",
        json={"new_path": "paper/Work/Renamed.paper"},
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["path"] == "paper/Work/Renamed.paper"

    # …and the page family still reads a nested notebook path plus an id.
    page = client.get(f"/api/paper/pages/paper/Work/Renamed.paper/{page_id}")
    assert page.status_code == 200, page.text
    assert page.json()["id"] == page_id

    # Sibling literal families are reachable with a notebook present.
    assert client.get("/api/paper/changes").status_code == 200
    assert client.get("/api/paper/search?q=x").status_code == 200
    assert client.get("/api/paper/trash").status_code == 200


def test_library_returns_folders_notebooks_and_a_cover_url(state):
    client = make_client(state)
    nb = new_notebook(client, "Ideas", folder="Work")
    page_id = client.post(f"/api/paper/notebooks/{nb['path']}/pages", json={}).json()[
        "id"
    ]
    put_page(client, nb["path"], page_id, 1, drawing=b"ink", render=PNG)

    library = client.get("/api/paper").json()
    assert library["folders"] == ["paper/Work"]
    entry = library["notebooks"][0]
    assert entry["path"] == nb["path"] and entry["page_count"] == 1
    assert entry["cover"] == page_id
    assert entry["cover_url"] == (
        f"/api/paper/blob/{nb['path']}/{page_id}/render.png?r=2&v={etag_of(PNG)}"
    )


def test_an_encoded_blob_url_serves_a_notebook_with_spaces(state):
    """page_url percent-encodes the notebook path; the blob route must still
    find "Notebook 1.paper" from it."""
    client = make_client(state)
    nb = new_notebook(client, "Notebook 1", folder="School (old)")
    page_id = client.post(f"/api/paper/notebooks/{nb['path']}/pages", json={}).json()[
        "id"
    ]
    put_page(client, nb["path"], page_id, 1, drawing=b"ink", render=PNG)

    entry = client.get("/api/paper").json()["notebooks"][0]
    assert " " not in entry["cover_url"] and "Notebook%201.paper" in entry["cover_url"]
    blob = client.get(entry["cover_url"])
    assert blob.status_code == 200
    assert blob.content == PNG


def test_stable_id_resolver_survives_notebook_rename(state):
    client = make_client(state)
    nb = new_notebook(client, "Ideas", id="notebook-stable")
    page_id = "a" * 32
    created = client.post(
        f"/api/paper/notebooks/{nb['path']}/pages", json={"id": page_id}
    )
    assert created.status_code == 200
    moved = client.post(
        f"/api/paper/notebooks/{nb['path']}/rename",
        json={"new_path": "paper/Archive/Renamed.paper"},
    )
    assert moved.status_code == 200
    resolved = client.get(
        f"/api/paper/id/notebook-stable/{page_id}?region=1,2,30,40"
    )
    assert resolved.status_code == 200, resolved.text
    body = resolved.json()
    assert body["notebook"] == moved.json()["path"]
    assert body["region"] == "1,2,30,40"
    assert body["url"] == f"/paper/id/notebook-stable/{page_id}?region=1,2,30,40"


def test_selection_action_uses_ids_and_does_not_write_the_page(
    state, monkeypatch
):
    client = make_client(state)
    nb = new_notebook(client, "Ideas", id="notebook-stable")
    page_id = "a" * 32
    put_page(client, nb["path"], page_id, 0, drawing=b"original")
    before = state.paper.resolve_page(nb["path"], page_id).joinpath(
        "drawing.pkdrawing"
    ).read_bytes()
    state.threads = SimpleNamespace(
        create=lambda title, thread_id=None: {"id": thread_id or "thread-1"}
    )

    async def fake_run(*args, **kwargs):
        return {
            "session_id": "thread-1",
            "reply": "selected",
            "assistant_message": {"text": "selected"},
            "confirmations": [],
            "actions": [],
            "pending_count": 0,
        }

    def fake_auth(_request):
        return None

    monkeypatch.setattr(overseer_api, "_run_thread_chat", fake_run)
    monkeypatch.setattr(overseer_api, "_auth_session_id", fake_auth)
    response = client.post(
        "/api/paper/selection-action",
        data={
            "notebook_id": "notebook-stable",
            "page_id": page_id,
            "action": "ask",
            "region": '{"x":1,"y":2,"width":30,"height":40}',
        },
        files={"image": ("crop.png", PNG, "image/png")},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["thread_id"] == "thread-1"
    assert body["page"]["url"].endswith("?region=1,2,30,40")
    assert state.paper.resolve_page(nb["path"], page_id).joinpath(
        "drawing.pkdrawing"
    ).read_bytes() == before


def test_selection_action_creates_the_thread_the_client_named(state, monkeypatch):
    """The iPad names a new action's thread before sending it, so a turn it
    stops waiting for still has a thread to open. The first request under
    that name creates it; a second one joins it."""
    client = make_client(state)
    nb = new_notebook(client, "Ideas", id="notebook-stable")
    page_id = "a" * 32
    put_page(client, nb["path"], page_id, 0, drawing=b"original")
    state.threads = ThreadStore(
        state.settings.vault_dir, state.settings.data_dir
    )
    ran: list[str] = []

    async def fake_run(_state, thread_id, *args, **kwargs):
        ran.append(thread_id)
        return {"reply": "selected", "confirmations": [], "actions": []}

    monkeypatch.setattr(overseer_api, "_run_thread_chat", fake_run)
    monkeypatch.setattr(overseer_api, "_auth_session_id", lambda _request: None)

    def act():
        return client.post(
            "/api/paper/selection-action",
            data={
                "notebook_id": "notebook-stable",
                "page_id": page_id,
                "action": "transcribe",
                "thread_id": "c" * 32,
            },
            files={"image": ("crop.png", PNG, "image/png")},
        )

    first = act()
    assert first.status_code == 200, first.text
    assert first.json()["thread_id"] == "c" * 32
    assert first.json()["navigate"] == "/omnia?t=" + "c" * 32
    assert state.threads.get("c" * 32)["title"] == "Paper selection"
    second = act()
    assert second.status_code == 200, second.text
    assert ran == ["c" * 32, "c" * 32]
    assert [t["id"] for t in state.threads.list()].count("c" * 32) == 1


# -- the multipart save ------------------------------------------------------


def test_a_full_save_stores_every_part_and_reports_the_urls(state):
    client = make_client(state)
    nb = new_notebook(client)
    response = put_page(
        client,
        nb["path"],
        "a" * 32,
        0,
        page={"template": "grid"},
        drawing=b"ink",
        render=PNG,
        text={"lines": [], "transcript": "hello"},
        client_id="ipad-1",
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["revision"] == 1 and body["stale"] is False
    assert body["page"]["template"] == "grid"
    assert body["page"]["render_url"].endswith(f"render.png?r=1&v={etag_of(PNG)}")
    assert body["page"]["drawing_url"].endswith("drawing.pkdrawing?r=1")


def test_a_conflicting_save_answers_409_with_both_versions(state):
    client = make_client(state)
    nb = new_notebook(client)
    put_page(client, nb["path"], "a" * 32, 0, drawing=b"one")
    put_page(client, nb["path"], "a" * 32, 1, drawing=b"two")

    late = put_page(client, nb["path"], "a" * 32, 1, drawing=b"stale-device")
    assert late.status_code == 409
    conflict = late.json()["conflict"]
    assert conflict["original"]["revision"] == 2
    assert conflict["sibling"]["conflict_of"] == "a" * 32

    manifest = client.get(f"/api/paper/notebooks/{nb['path']}").json()
    assert manifest["pages"] == ["a" * 32, conflict["sibling"]["id"]]


def test_a_retried_put_is_a_200_not_a_conflict(state):
    """The response to the first PUT was lost; the iPad sends the identical
    request again from the same base."""
    client = make_client(state)
    nb = new_notebook(client)
    first = put_page(
        client, nb["path"], "a" * 32, 0, drawing=b"ink", render=PNG,
        for_revision=1, client_id="ipad-1",
    )
    assert first.status_code == 200, first.text

    again = put_page(
        client, nb["path"], "a" * 32, 0, drawing=b"ink", render=PNG,
        for_revision=1, client_id="ipad-1",
    )
    assert again.status_code == 200, again.text
    body = again.json()
    assert set(body) == set(first.json()) == {"page", "revision", "stale"}
    assert body["revision"] == 1 and body["stale"] is False
    assert body["page"]["drawing_url"] == first.json()["page"]["drawing_url"]
    manifest = client.get(f"/api/paper/notebooks/{nb['path']}").json()
    assert manifest["pages"] == ["a" * 32]
    assert [page["id"] for page in manifest["pages_detail"]] == ["a" * 32]


def test_a_put_after_a_create_whose_answer_was_lost_is_the_same_devices(state):
    client = make_client(state)
    nb = new_notebook(client)
    page_id = "a" * 32
    body = {"id": page_id, "client_id": "ipad-1"}
    created = client.post(f"/api/paper/notebooks/{nb['path']}/pages", json=body)
    assert created.status_code == 200, created.text
    retried = client.post(f"/api/paper/notebooks/{nb['path']}/pages", json=body)
    assert retried.status_code == 409

    saved = put_page(client, nb["path"], page_id, 0, drawing=b"ink", client_id="ipad-1")
    assert saved.status_code == 200, saved.text
    assert saved.json()["revision"] == 2
    listed = client.get(f"/api/paper/notebooks/{nb['path']}").json()["pages"]
    assert listed == [page_id]


def test_a_created_page_keeps_the_size_name_the_client_sent(state):
    client = make_client(state)
    nb = new_notebook(client, paperSize="infinite")
    created = client.post(
        f"/api/paper/notebooks/{nb['path']}/pages",
        json={
            "id": "a" * 32,
            "size": {"width": 1024, "height": 2732},
            "size_name": "infinite",
        },
    )
    assert created.status_code == 200, created.text
    assert created.json()["size_name"] == "infinite"
    assert created.json()["size"] == {"width": 1024.0, "height": 2732.0}
    saved = put_page(
        client, nb["path"], "a" * 32, 1,
        page={"size": {"width": 1024, "height": 4000}, "size_name": "infinite",
              "size_points": {"width": 1024, "height": 4000}},
    )
    assert saved.status_code == 200, saved.text
    fetched = client.get(f"/api/paper/pages/{nb['path']}/{'a' * 32}").json()
    assert fetched["size_name"] == "infinite"
    assert fetched["size_points"] == {"width": 1024.0, "height": 4000.0}


def test_base_revision_zero_over_an_existing_page_is_409(state):
    client = make_client(state)
    nb = new_notebook(client)
    put_page(client, nb["path"], "a" * 32, 0, drawing=b"one")
    again = put_page(client, nb["path"], "a" * 32, 0, drawing=b"two")
    assert again.status_code == 409
    assert again.json()["conflict"]["original"]["revision"] == 1


def test_parts_over_their_cap_are_413(state, monkeypatch):
    monkeypatch.setattr(paper_api, "MAX_DRAWING_BYTES", 8)
    monkeypatch.setattr(paper_api, "MAX_RENDER_BYTES", 8)
    client = make_client(state)
    nb = new_notebook(client)

    too_much_ink = put_page(client, nb["path"], "a" * 32, 0, drawing=b"x" * 64)
    assert too_much_ink.status_code == 413

    put_page(client, nb["path"], "a" * 32, 0, drawing=b"ok")
    too_big_render = put_page(client, nb["path"], "a" * 32, 1, render=b"y" * 64)
    assert too_big_render.status_code == 413


def test_a_save_publishes_a_paper_event_carrying_its_origin(state):
    client = make_client(state)
    nb = new_notebook(client)
    queue = state.live.subscribe()
    put_page(client, nb["path"], "a" * 32, 0, drawing=b"ink", client_id="ipad-1")

    event = queue.get_nowait()
    assert event == {
        "type": "paper",
        "path": f"{nb['path']}/pages/{'a' * 32}",
        "revision": 1,
        "origin": "ipad-1",
    }


# -- blobs -------------------------------------------------------------------


def test_a_blob_is_immutable_only_when_the_url_names_its_revision_and_bytes(state):
    client = make_client(state)
    nb = new_notebook(client)
    saved = put_page(client, nb["path"], "a" * 32, 0, drawing=b"ink", render=PNG)
    render_url = saved.json()["page"]["render_url"]
    base = f"/api/paper/blob/{nb['path']}/{'a' * 32}"
    assert render_url == f"{base}/render.png?r=1&v={etag_of(PNG)}"

    fresh = client.get(render_url)
    assert fresh.status_code == 200 and fresh.content == PNG
    assert fresh.headers["cache-control"] == IMMUTABLE
    assert fresh.headers["content-type"] == "image/png"

    # The revision alone, no revision, the wrong revision or the wrong hash:
    # the bytes under this name can still change.
    for url in (
        f"{base}/render.png?r=1",
        f"{base}/render.png",
        f"{base}/render.png?r=9&v={etag_of(PNG)}",
        f"{base}/render.png?r=1&v=000000000000",
    ):
        response = client.get(url)
        assert response.status_code == 200 and response.content == PNG, url
        assert response.headers["cache-control"] == NO_CACHE, url
    assert client.get(f"{base}/notebook.json").status_code == 404


def test_a_correction_at_the_same_revision_moves_the_text_url(state):
    """A corrected transcript rewrites text.json without moving text_revision;
    the URL a browser was told to keep forever must not serve it."""
    client = make_client(state)
    nb = new_notebook(client)
    put_page(client, nb["path"], "a" * 32, 0, drawing=b"ink")
    first = put_page(
        client, nb["path"], "a" * 32, 1,
        text={"lines": [], "transcript": "helo wrld"}, for_revision=1,
    ).json()["page"]
    old_url = first["text_url"]
    assert "?r=1&v=" in old_url
    assert client.get(old_url).headers["cache-control"] == IMMUTABLE

    second = put_page(
        client, nb["path"], "a" * 32, 1,
        text={
            "lines": [],
            "transcript": "helo wrld",
            "corrected_transcript": "hello world",
        },
        for_revision=1,
    ).json()["page"]
    new_url = second["text_url"]
    assert second["text_revision"] == first["text_revision"] == 1
    assert new_url != old_url and "?r=1&v=" in new_url

    stale = client.get(old_url)
    assert stale.headers["cache-control"] == NO_CACHE
    fetched = client.get(new_url)
    assert fetched.headers["cache-control"] == IMMUTABLE
    assert fetched.json()["corrected_transcript"] == "hello world"


def test_a_matching_if_none_match_is_a_304_without_the_bytes(state):
    client = make_client(state)
    nb = new_notebook(client)
    saved = put_page(client, nb["path"], "a" * 32, 0, drawing=b"ink", render=PNG)
    url = saved.json()["page"]["render_url"]
    base = f"/api/paper/blob/{nb['path']}/{'a' * 32}"
    etag = client.get(url).headers["etag"]
    assert etag == f'"1-{etag_of(PNG)}"'

    for header in (etag, f"W/{etag}", f'"1-000000000000", {etag}', "*"):
        again = client.get(url, headers={"If-None-Match": header})
        assert again.status_code == 304, header
        assert again.content == b""
        assert again.headers["etag"] == etag
        assert again.headers["cache-control"] == IMMUTABLE
    # The iPad's own URL names only the revision: revalidated, then a 304.
    ipad = client.get(f"{base}/render.png?r=1", headers={"If-None-Match": etag})
    assert ipad.status_code == 304
    assert ipad.headers["cache-control"] == NO_CACHE

    miss = client.get(url, headers={"If-None-Match": '"1-000000000000"'})
    assert miss.status_code == 200 and miss.content == PNG

    # A new render at the same revision no longer matches.
    put_page(client, nb["path"], "a" * 32, 1, render=PNG + b"x", for_revision=1)
    changed = client.get(f"{base}/render.png?r=1", headers={"If-None-Match": etag})
    assert changed.status_code == 200 and changed.content == PNG + b"x"
    assert changed.headers["etag"] == f'"1-{etag_of(PNG + b"x")}"'


def test_bytes_back_at_a_later_revision_do_not_match_an_old_etag(state):
    """The iPad keeps the ETag of its last fetch even after drawing over the
    file locally; a restore that brings those bytes back must still send them."""
    client = make_client(state)
    nb = new_notebook(client)
    base = f"/api/paper/blob/{nb['path']}/{'a' * 32}"
    put_page(client, nb["path"], "a" * 32, 0, drawing=b"one")
    old = client.get(f"{base}/drawing.pkdrawing?r=1").headers["etag"]
    put_page(client, nb["path"], "a" * 32, 1, drawing=b"two")
    put_page(client, nb["path"], "a" * 32, 2, drawing=b"one")

    back = client.get(f"{base}/drawing.pkdrawing?r=3", headers={"If-None-Match": old})
    assert back.status_code == 200 and back.content == b"one"
    assert back.headers["etag"] == f'"3-{etag_of(b"one")}"' != old
    same = client.get(
        f"{base}/drawing.pkdrawing?r=3", headers={"If-None-Match": back.headers["etag"]}
    )
    assert same.status_code == 304


def test_a_page_written_before_blobs_were_hashed_still_revalidates(state):
    client = make_client(state)
    nb = new_notebook(client)
    put_page(
        client, nb["path"], "a" * 32, 0, drawing=b"ink", render=PNG,
        text={"lines": [], "transcript": "hello"},
    )
    page_json = state.paper.dir / nb["path"] / "pages" / ("a" * 32) / "page.json"
    record = json.loads(page_json.read_text())
    del record["render_etag"], record["text_etag"]
    page_json.write_text(json.dumps(record))

    page = client.get(f"/api/paper/pages/{nb['path']}/{'a' * 32}").json()
    # Nothing names the bytes, so nothing is cacheable forever.
    assert page["render_url"].endswith("render.png?r=1")
    assert page["text_url"].endswith("text.json?r=1")
    first = client.get(page["render_url"])
    assert first.status_code == 200 and first.content == PNG
    assert first.headers["cache-control"] == NO_CACHE
    assert first.headers["etag"] == f'"1-{etag_of(PNG)}"'
    again = client.get(
        page["render_url"], headers={"If-None-Match": first.headers["etag"]}
    )
    assert again.status_code == 304 and again.content == b""
    # A guessed hash cannot make a legacy URL immutable.
    guessed = client.get(f"{page['render_url']}&v={etag_of(PNG)}")
    assert guessed.headers["cache-control"] == NO_CACHE

    text = client.get(page["text_url"])
    assert text.status_code == 200 and text.json()["transcript"] == "hello"
    assert text.headers["etag"] == f'"1-{etag_of(text.content)}"'


# -- change feed -------------------------------------------------------------


def test_the_feed_reports_only_what_moved_after_the_cursor(state):
    client = make_client(state)
    nb = new_notebook(client)
    put_page(client, nb["path"], "a" * 32, 0, drawing=b"ink")

    first = client.get("/api/paper/changes?since=0").json()
    assert first["full"] is True
    assert len(first["pages"]) == 1 and first["deleted"] == []
    cursor, epoch = first["seq"], first["epoch"]

    quiet = client.get(f"/api/paper/changes?since={cursor}&epoch={epoch}").json()
    assert quiet["full"] is False and quiet["pages"] == [] and quiet["notebooks"] == []

    put_page(client, nb["path"], "b" * 32, 0, drawing=b"more")
    moved = client.get(f"/api/paper/changes?since={cursor}&epoch={epoch}").json()
    assert [page["id"] for page in moved["pages"]] == ["b" * 32]
    assert moved["notebooks"][0]["path"] == nb["path"]

    cursor = moved["seq"]
    client.delete(f"/api/paper/pages/{nb['path']}/{'b' * 32}")
    deleted = client.get(f"/api/paper/changes?since={cursor}&epoch={epoch}").json()
    assert [row["path"] for row in deleted["deleted"]] == [
        f"{nb['path']}/pages/{'b' * 32}"
    ]
    assert deleted["deleted"][0]["kind"] == "page"


def test_an_epoch_mismatch_forces_a_full_reconcile(state):
    client = make_client(state)
    nb = new_notebook(client)
    put_page(client, nb["path"], "a" * 32, 0, drawing=b"ink")
    cursor = client.get("/api/paper/changes").json()["seq"]

    stale = client.get(f"/api/paper/changes?since={cursor}&epoch=not-ours").json()
    assert stale["full"] is True
    assert len(stale["pages"]) == 1


def test_a_cursor_older_than_the_pruned_tombstones_forces_a_full_reconcile(state):
    client = make_client(state)
    nb = new_notebook(client)
    put_page(client, nb["path"], "a" * 32, 0, drawing=b"ink")
    epoch = client.get("/api/paper/changes").json()["epoch"]
    client.delete(f"/api/paper/pages/{nb['path']}/{'a' * 32}")
    cursor = client.get(f"/api/paper/changes?since=1&epoch={epoch}").json()["seq"]

    state.paper.prune_tombstones(days=0)
    answer = client.get(f"/api/paper/changes?since={cursor - 1}&epoch={epoch}").json()
    assert answer["full"] is True


# -- trash -------------------------------------------------------------------


def test_trash_lists_paper_kinds_only_and_restores_them(state):
    client = make_client(state)
    nb = new_notebook(client)
    put_page(client, nb["path"], "a" * 32, 0, drawing=b"ink")
    state.trash.trash_note("Welcome.md")

    deleted = client.delete(f"/api/paper/pages/{nb['path']}/{'a' * 32}")
    assert deleted.status_code == 200
    items = client.get("/api/paper/trash").json()["items"]
    assert [item["kind"] for item in items] == ["page"]

    restored = client.post(f"/api/paper/trash/{items[0]['id']}/restore")
    assert restored.status_code == 200
    assert client.get(f"/api/paper/notebooks/{nb['path']}").json()["pages"] == [
        "a" * 32
    ]
    assert client.get("/api/paper/trash").json()["items"] == []


def test_deleting_a_missing_page_is_404(state):
    client = make_client(state)
    nb = new_notebook(client)
    assert client.delete(f"/api/paper/pages/{nb['path']}/{'a' * 32}").status_code == 404


def test_a_restored_notebook_comes_back_through_the_feed(state):
    """The iPad's Trash restores on the server and then pulls from its cursor,
    which is already past every seq the restored files carried."""
    client = make_client(state)
    nb = new_notebook(client)
    put_page(client, nb["path"], "a" * 32, 0, drawing=b"ink")
    first = client.get("/api/paper/changes").json()
    epoch = first["epoch"]

    item = client.delete(f"/api/paper/notebooks/{nb['path']}").json()
    gone = client.get(f"/api/paper/changes?since={first['seq']}&epoch={epoch}").json()
    assert [row["path"] for row in gone["deleted"]] == [nb["path"]]
    cursor = gone["seq"]

    assert client.post(f"/api/paper/trash/{item['id']}/restore").status_code == 200
    back = client.get(f"/api/paper/changes?since={cursor}&epoch={epoch}").json()
    assert back["full"] is False
    assert [row["path"] for row in back["notebooks"]] == [nb["path"]]
    assert [page["path"] for page in back["pages"]] == [
        f"{nb['path']}/pages/{'a' * 32}"
    ]
    assert back["pages"][0]["revision"] == 1
    assert back["deleted"] == []
    assert back == paper_api.changes_since(state.paper, epoch, cursor)
    # A device that never saw the delete must not be handed it now either.
    missed = client.get(f"/api/paper/changes?since={first['seq']}&epoch={epoch}")
    assert missed.json()["deleted"] == []
    # A fresh device reads the pages from the index, which the restore refilled.
    full = client.get("/api/paper/changes?since=0").json()
    assert [page["id"] for page in full["pages"]] == ["a" * 32]


def test_a_restored_page_comes_back_through_the_feed(state):
    client = make_client(state)
    nb = new_notebook(client)
    put_page(client, nb["path"], "a" * 32, 0, drawing=b"ink")
    put_page(client, nb["path"], "b" * 32, 0, drawing=b"more")
    epoch = client.get("/api/paper/changes").json()["epoch"]
    item = client.delete(f"/api/paper/pages/{nb['path']}/{'b' * 32}").json()
    cursor = client.get(f"/api/paper/changes?since=1&epoch={epoch}").json()["seq"]

    assert client.post(f"/api/paper/trash/{item['id']}/restore").status_code == 200
    back = client.get(f"/api/paper/changes?since={cursor}&epoch={epoch}").json()
    assert [page["id"] for page in back["pages"]] == ["b" * 32]
    assert [row["path"] for row in back["notebooks"]] == [nb["path"]]
    assert back["notebooks"][0]["page_count"] == 2
    assert back["deleted"] == []
    assert back == paper_api.changes_since(state.paper, epoch, cursor)


def test_a_reused_notebook_path_is_not_reported_deleted(state):
    """Delete 'Ideas' and create it again, or rename A to B and back: the
    client applies deletes after upserts, so a tombstone left on a live path
    would drop the notebook it had just received."""
    client = make_client(state)
    new_notebook(client, "Seed")
    first = client.get("/api/paper/changes").json()
    cursor, epoch = first["seq"], first["epoch"]

    ideas = new_notebook(client, "Ideas")
    client.delete(f"/api/paper/notebooks/{ideas['path']}")
    assert new_notebook(client, "Ideas")["path"] == ideas["path"]

    alpha = new_notebook(client, "Alpha")
    client.post(
        f"/api/paper/notebooks/{alpha['path']}/rename",
        json={"new_path": "paper/Beta.paper"},
    )
    client.post(
        "/api/paper/notebooks/paper/Beta.paper/rename",
        json={"new_path": alpha["path"]},
    )

    feed = client.get(f"/api/paper/changes?since={cursor}&epoch={epoch}").json()
    assert {row["path"] for row in feed["notebooks"]} == {ideas["path"], alpha["path"]}
    assert [row["path"] for row in feed["deleted"]] == ["paper/Beta.paper"]
    assert feed == paper_api.changes_since(state.paper, epoch, cursor)


def test_a_rename_moves_the_pages_through_the_feed(state):
    """A second device learns the new path from the feed; without its pages it
    would get an empty notebook there and trash the old one."""
    client = make_client(state)
    nb = new_notebook(client, "Gamma")
    put_page(client, nb["path"], "a" * 32, 0, drawing=b"ink")
    first = client.get("/api/paper/changes").json()

    renamed = client.post(
        f"/api/paper/notebooks/{nb['path']}/rename",
        json={"new_path": "paper/Delta.paper"},
    )
    assert renamed.status_code == 200, renamed.text
    feed = client.get(
        f"/api/paper/changes?since={first['seq']}&epoch={first['epoch']}"
    ).json()
    assert [row["path"] for row in feed["notebooks"]] == ["paper/Delta.paper"]
    assert [page["path"] for page in feed["pages"]] == [
        f"paper/Delta.paper/pages/{'a' * 32}"
    ]
    assert feed["pages"][0]["revision"] == 1
    assert [row["path"] for row in feed["deleted"]] == [nb["path"]]
    assert feed == paper_api.changes_since(
        state.paper, first["epoch"], first["seq"]
    )
    # The renaming device is told the seqs its pages moved to, so the echo
    # above is not news to it.
    assert [
        (row["id"], row["seq"]) for row in renamed.json()["moved_pages"]
    ] == [(page["id"], page["seq"]) for page in feed["pages"]]


def test_a_rename_keeps_the_title_unless_the_body_carries_one(state):
    """The iPad retitles with the slug path plus the verbatim title, and moves
    with the path alone; neither may replace the title with the slug."""
    client = make_client(state)
    nb = new_notebook(client, "Q3: Plans?")
    assert nb["path"] == "paper/Q3 Plans.paper"

    moved = client.post(
        f"/api/paper/notebooks/{nb['path']}/rename",
        json={"new_path": "paper/Archive/Q3 Plans.paper"},
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["title"] == "Q3: Plans?"

    ideas = new_notebook(client, "Ideas")
    retitled = client.post(
        f"/api/paper/notebooks/{ideas['path']}/rename",
        json={"new_path": "paper/Q3 Plans.paper", "title": "Q3: Plans?"},
    )
    assert retitled.status_code == 200, retitled.text
    assert retitled.json()["path"] == "paper/Q3 Plans.paper"
    assert retitled.json()["title"] == "Q3: Plans?"
    stored = client.get("/api/paper/notebooks/paper/Q3 Plans.paper").json()
    assert stored["title"] == "Q3: Plans?"

    # Same slug, new title: the path stays and the feed carries the title.
    cursor = client.get("/api/paper/changes").json()
    same = client.post(
        "/api/paper/notebooks/paper/Q3 Plans.paper/rename",
        json={"new_path": "paper/Q3 Plans.paper", "title": "Q3 Plans!"},
    )
    assert same.status_code == 200, same.text
    feed = client.get(
        f"/api/paper/changes?since={cursor['seq']}&epoch={cursor['epoch']}"
    ).json()
    assert [(row["path"], row["title"]) for row in feed["notebooks"]] == [
        ("paper/Q3 Plans.paper", "Q3 Plans!")
    ]
    assert feed["deleted"] == []


# -- history -----------------------------------------------------------------


def test_history_lists_commits_and_restore_is_a_new_revision(state):
    client = make_client(state)
    nb = new_notebook(client)
    page_id = "a" * 32
    put_page(client, nb["path"], page_id, 0, drawing=b"first")
    asyncio.run(state.vault.commit_all("paper: first"))
    put_page(client, nb["path"], page_id, 1, drawing=b"second")
    asyncio.run(state.vault.commit_all("paper: second"))

    history = client.get(f"/api/paper/history/{nb['path']}/{page_id}").json()
    assert len(history["versions"]) == 2
    assert history["path"] == f"{nb['path']}/pages/{page_id}"
    oldest = history["versions"][-1]["sha"]

    restored = client.post(
        f"/api/paper/history/{nb['path']}/{page_id}/restore", json={"sha": oldest}
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["revision"] == 3
    blob = client.get(
        f"/api/paper/blob/{nb['path']}/{page_id}/drawing.pkdrawing"
    )
    assert blob.content == b"first"
    # The restore added a revision; it did not rewrite the log.
    after = client.get(f"/api/paper/history/{nb['path']}/{page_id}").json()
    assert len(after["versions"]) == 2


def test_a_restore_is_never_taken_for_a_devices_retry(state):
    """Even when the device itself asked for the restore, its pending save
    from before it must come back as a conflict, not silently undo it."""
    client = make_client(state)
    nb = new_notebook(client)
    page_id = "a" * 32
    put_page(client, nb["path"], page_id, 0, drawing=b"first", client_id="ipad-1")
    asyncio.run(state.vault.commit_all("paper: first"))
    put_page(client, nb["path"], page_id, 1, drawing=b"second", client_id="ipad-1")
    asyncio.run(state.vault.commit_all("paper: second"))
    oldest = client.get(f"/api/paper/history/{nb['path']}/{page_id}").json()[
        "versions"
    ][-1]["sha"]

    restored = client.post(
        f"/api/paper/history/{nb['path']}/{page_id}/restore",
        json={"sha": oldest, "client_id": "ipad-1"},
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["revision"] == 3

    late = put_page(
        client, nb["path"], page_id, 2, drawing=b"pending", client_id="ipad-1"
    )
    assert late.status_code == 409
    blob = client.get(f"/api/paper/blob/{nb['path']}/{page_id}/drawing.pkdrawing")
    assert blob.content == b"first"


def test_paper_writes_run_on_the_loop_under_the_git_lock(state, monkeypatch):
    """The autocommitter stages the vault from a worker thread under the git
    lock; a Paper write that holds it cannot be caught half done, and one on
    the loop cannot interleave with another."""
    client = make_client(state)
    nb = new_notebook(client)
    page_id = "a" * 32
    locked = []

    def on_the_loop(method):
        def wrapper(*args, **kwargs):
            asyncio.get_running_loop()  # RuntimeError in a worker thread
            locked.append(state.vault._git_lock.locked())
            return method(*args, **kwargs)

        return wrapper

    monkeypatch.setattr(state.paper, "save_page", on_the_loop(state.paper.save_page))
    monkeypatch.setattr(
        state.paper, "restore_trash", on_the_loop(state.paper.restore_trash)
    )

    assert put_page(client, nb["path"], page_id, 0, drawing=b"first").status_code == 200
    asyncio.run(state.vault.commit_all("paper: first"))
    assert put_page(client, nb["path"], page_id, 1, drawing=b"second").status_code == 200
    asyncio.run(state.vault.commit_all("paper: second"))
    oldest = client.get(f"/api/paper/history/{nb['path']}/{page_id}").json()[
        "versions"
    ][-1]["sha"]

    restored = client.post(
        f"/api/paper/history/{nb['path']}/{page_id}/restore", json={"sha": oldest}
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["revision"] == 3

    item = client.delete(f"/api/paper/pages/{nb['path']}/{page_id}").json()
    back = client.post(f"/api/paper/trash/{item['id']}/restore")
    assert back.status_code == 200, back.text
    assert client.get(f"/api/paper/notebooks/{nb['path']}").json()["pages"] == [
        page_id
    ]
    # Two PUTs, the history restore's save, the trash restore.
    assert locked == [True, True, True, True]


def test_index_hooks_run_under_the_feed_lock_and_outside_the_git_lock(
    state, monkeypatch
):
    """Each write takes its seq in the store before the index sees the change,
    and /changes waits on the index's feed lock: every hook has to run inside
    that lock, and outside the git lock, which is let go after the store call."""
    index = state.paper_index
    seen = set()

    def guarded(name):
        method = getattr(index, name)

        async def hook(*args, **kwargs):
            assert index.feed_lock.locked(), name
            assert not state.vault._git_lock.locked(), name
            seen.add(name)
            return await method(*args, **kwargs)

        return hook

    hooks = (
        "index_page",
        "index_notebook",
        "reindex_notebook",
        "remove_page",
        "remove_notebook",
    )
    for name in hooks:
        monkeypatch.setattr(index, name, guarded(name))

    client = make_client(state)
    nb = new_notebook(client)["path"]
    page_id, other = "a" * 32, "b" * 32
    assert put_page(client, nb, page_id, 0, drawing=b"one").status_code == 200
    asyncio.run(state.vault.commit_all("paper: one"))
    assert put_page(client, nb, page_id, 1, drawing=b"two").status_code == 200
    assert put_page(client, nb, page_id, 1, drawing=b"late").status_code == 409
    sha = client.get(f"/api/paper/history/{nb}/{page_id}").json()["versions"][-1]["sha"]
    restored = client.post(
        f"/api/paper/history/{nb}/{page_id}/restore", json={"sha": sha}
    )
    assert restored.status_code == 200, restored.text

    created = client.post(f"/api/paper/notebooks/{nb}/pages", json={"id": other})
    assert created.status_code == 200, created.text
    patched = client.patch(f"/api/paper/notebooks/{nb}", json={"favorite": True})
    assert patched.status_code == 200, patched.text
    item = client.delete(f"/api/paper/pages/{nb}/{other}").json()
    assert client.post(f"/api/paper/trash/{item['id']}/restore").status_code == 200
    moved = client.post(
        f"/api/paper/notebooks/{nb}/rename", json={"new_path": "paper/Moved.paper"}
    )
    assert moved.status_code == 200, moved.text
    assert client.delete("/api/paper/notebooks/paper/Moved.paper").status_code == 200
    assert seen == set(hooks)
    assert not index.feed_lock.locked()


def test_changes_never_wait_on_git_behind_a_queued_write(state):
    """A commit of the whole vault, or git gc, holds the git lock for as long
    as it takes. A Paper write queued behind it must not hold the feed lock
    while it waits, or every /changes read hangs until git is done."""
    import httpx

    client = make_client(state)
    nb = new_notebook(client)
    cursor = client.get("/api/paper/changes").json()
    git_lock = state.vault._git_lock
    params = {"since": cursor["seq"], "epoch": cursor["epoch"]}

    async def scenario():
        transport = httpx.ASGITransport(app=client.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
            await git_lock.acquire()
            try:
                write = asyncio.create_task(
                    http.put(
                        f"/api/paper/pages/{nb['path']}/{'a' * 32}",
                        data={"base_revision": "0"},
                        files={"drawing": ("drawing.pkdrawing", b"ink", "x/y")},
                    )
                )
                for _ in range(500):
                    if git_lock._waiters:
                        break
                    await asyncio.sleep(0.01)
                assert git_lock._waiters, "the write never reached the git lock"
                during = await asyncio.wait_for(
                    http.get("/api/paper/changes", params=params), timeout=5
                )
                assert not write.done()
            finally:
                git_lock.release()
            saved = await asyncio.wait_for(write, timeout=5)
            after = await http.get("/api/paper/changes", params=params)
        return during, saved, after

    during, saved, after = asyncio.run(scenario())
    assert during.status_code == 200 and during.json()["pages"] == []
    assert saved.status_code == 200, saved.text
    assert [page["id"] for page in after.json()["pages"]] == ["a" * 32]


# -- assets ------------------------------------------------------------------


def test_assets_round_trip_and_are_cached_forever(state):
    client = make_client(state)
    nb = new_notebook(client)
    uploaded = client.post(
        f"/api/paper/assets/{nb['path']}",
        files={"file": ("Scan One.png", PNG, "image/png")},
    )
    assert uploaded.status_code == 200, uploaded.text
    asset = uploaded.json()

    fetched = client.get(asset["url"])
    assert fetched.status_code == 200 and fetched.content == PNG
    assert fetched.headers["cache-control"] == "private, max-age=31536000, immutable"


def test_assets_are_served_only_under_an_allow_listed_type(state):
    """The type comes from an explicit map, never a guess from the filename,
    so a file placed in assets/ by hand is not served at all."""
    from app import paper_store

    assert set(paper_api.ASSET_MEDIA_TYPES) == paper_store.ASSET_EXTENSIONS
    client = make_client(state)
    nb = new_notebook(client)
    asset = client.post(
        f"/api/paper/assets/{nb['path']}",
        files={"file": ("Scan.png", PNG, "image/png")},
    ).json()

    fetched = client.get(asset["url"])
    assert fetched.status_code == 200
    assert fetched.headers["content-type"] == "image/png"
    assert fetched.headers["content-disposition"] == "inline"
    assert fetched.headers["x-content-type-options"] == "nosniff"

    assets_dir = state.paper.dir / nb["path"] / "assets"
    (assets_dir / "evil.svg").write_bytes(b"<svg><script>alert(1)</script></svg>")
    refused = client.get(f"/api/paper/assets/{nb['path']}/evil.svg")
    assert refused.status_code == 404


def test_a_short_asset_is_rewritten_by_the_next_upload(state):
    """A write killed part-way leaves a truncated blob under its hashed name;
    uploading the same bytes again must repair it, leaving no temp file."""
    client = make_client(state)
    nb = new_notebook(client)

    def upload():
        return client.post(
            f"/api/paper/assets/{nb['path']}",
            files={"file": ("Scan.png", PNG, "image/png")},
        ).json()

    first = upload()
    target = state.paper.dir / first["path"]
    target.write_bytes(PNG[:10])

    assert upload()["path"] == first["path"]
    assert target.read_bytes() == PNG
    assert sorted(p.name for p in target.parent.iterdir()) == [target.name]


def test_notebook_patch_and_delete_round_trip(state):
    client = make_client(state)
    nb = new_notebook(client)
    client.post(f"/api/paper/notebooks/{nb['path']}/pages", json={"id": "a" * 32})
    client.post(f"/api/paper/notebooks/{nb['path']}/pages", json={"id": "b" * 32})

    patched = client.patch(
        f"/api/paper/notebooks/{nb['path']}",
        json={"favorite": True, "tags": ["work"], "pages": ["b" * 32]},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["favorite"] is True
    # The page the client left out is appended, never dropped.
    assert patched.json()["pages"] == ["b" * 32, "a" * 32]

    assert client.delete(f"/api/paper/notebooks/{nb['path']}").status_code == 200
    assert client.get(f"/api/paper/notebooks/{nb['path']}").status_code == 404


def test_index_hooks_run_only_when_a_paper_index_exists(state):
    """B2 attaches ``state.paper_index``; until then the routes must not look
    for it."""
    calls = []

    class Recorder:
        async def index_page(self, path):
            calls.append(("page", path))

        async def index_notebook(self, path):
            calls.append(("notebook", path))

        async def remove_page(self, path):
            calls.append(("remove_page", path))

        async def remove_notebook(self, path):
            calls.append(("remove_notebook", path))

    client = make_client(state)
    # The hooks are optional: an app booted without an index still serves.
    del state.paper_index
    nb = new_notebook(client)  # no index attached: must not raise
    state.paper_index = Recorder()

    put_page(client, nb["path"], "a" * 32, 0, drawing=b"ink")
    client.delete(f"/api/paper/pages/{nb['path']}/{'a' * 32}")
    # An index without `reindex_notebook` still gets the notebook on a rename.
    client.post(
        f"/api/paper/notebooks/{nb['path']}/rename",
        json={"new_path": "paper/Moved.paper"},
    )
    assert calls == [
        ("page", f"{nb['path']}/pages/{'a' * 32}"),
        ("remove_page", f"{nb['path']}/pages/{'a' * 32}"),
        ("notebook", nb["path"]),
        ("remove_notebook", nb["path"]),
        ("notebook", "paper/Moved.paper"),
    ]
