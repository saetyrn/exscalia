"""The Gallery: picture notes, boards, the index, the API, related pictures
and search. Offline: images are drawn with Pillow, the image model is a
colour-reading fake, no AI provider is configured."""
import asyncio
import io

import pytest
from PIL import Image

from app import gallery, gallery_jobs, gallery_search, recipe_import
from app.api import gallery as gallery_api
from app.api import notes as notes_api
from app.gallery_vectors import GalleryVectors
from app.vault import fm
from app.vault.notes import NoteError

from conftest import make_client


def run(coro):
    return asyncio.run(coro)


def png(color=(200, 30, 30), size=(40, 30)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


AXES = {"red": 0, "green": 1, "blue": 2}


class ColourEmbedder:
    """CLIP stand-in: an image is its mean colour, a text the colour words
    it names. Same space, so look-alike and text-to-image both work."""

    def embed_images(self, images):
        out = []
        for data in images:
            with Image.open(io.BytesIO(data)) as image:
                r, g, b = image.convert("RGB").resize((1, 1)).getpixel((0, 0))
            out.append([r / 255, g / 255, b / 255, 0.05])
        return out

    def embed_texts(self, texts):
        out = []
        for text in texts:
            v = [0.0, 0.0, 0.0, 0.05]
            for word in text.lower().split():
                if word in AXES:
                    v[AXES[word]] = 1.0
            out.append(v)
        return out


def add(state, color=(200, 30, 30), board="art", **kw):
    pin, created = run(gallery.add_picture(state, png(color), board=board, **kw))
    return pin, created


# -- the format ------------------------------------------------------------------------


def test_a_picture_is_a_note_on_its_board_named_by_its_image(state):
    pin, created = add(state, title="Hope", source_url="https://example.com/hope")
    assert created
    assert pin.path.startswith("gallery/art/") and pin.path.endswith(".md")
    assert pin.board == "art"
    assert pin.title == "Hope"
    assert pin.image.startswith("/api/attachments/")
    assert pin.thumb == "/api/gallery/thumb/" + pin.image.rsplit("/", 1)[1]
    assert (pin.width, pin.height) == (40, 30)
    assert pin.color == "#c81e1e"
    meta, body = fm.parse(state.store.read(pin.path))
    assert meta["gallery"] == 1
    assert meta["source_url"] == "https://example.com/hope"
    assert "described" not in meta
    assert body == ""


def test_an_untitled_picture_keeps_an_empty_title(state):
    pin, _ = add(state)
    assert pin.title == ""
    meta, _body = fm.parse(state.store.read(pin.path))
    assert meta["title"] == ""


def test_the_same_image_on_the_same_board_is_one_picture(state):
    first, _ = add(state)
    again, created = add(state)
    assert not created
    assert again.path == first.path
    elsewhere, created = add(state, board="dreamcore")
    assert created and elsewhere.path != first.path
    # One stored file for both boards.
    assert elsewhere.image == first.image


def test_not_an_image_is_refused(state):
    with pytest.raises(NoteError) as exc:
        run(gallery.add_picture(state, b"%PDF-1.4 nope", board="art"))
    assert exc.value.status == 400


def test_board_names_become_one_safe_folder(state):
    assert gallery.clean_board("  Life quotes ") == "Life quotes"
    assert gallery.clean_board("a/b") == "a b"
    assert gallery.clean_board("") == ""
    with pytest.raises(NoteError):
        gallery.clean_board(" .. ")


def test_the_marker_outside_gallery_or_not_the_integer_is_an_ordinary_note(state):
    content = fm.serialize(
        {"title": "x", "gallery": 1, "image": "/api/attachments/x-0123456789abcdef.png"}, ""
    )
    state.store.create("elsewhere/x.md", content)
    run(state.indexer.index_note("elsewhere/x.md", content))
    loose = fm.serialize(
        {"title": "y", "gallery": True, "image": "/api/attachments/y-0123456789abcdef.png"}, ""
    )
    state.store.create("gallery/art/y.md", loose, check_name=False)
    run(state.indexer.index_note("gallery/art/y.md", loose))
    assert run(gallery.count_pins(state)) == 0


def test_a_hand_broken_picture_never_stops_the_index(state):
    content = fm.serialize({"title": "x", "gallery": 1, "image": "https://far.away/x.png"}, "")
    state.store.create("gallery/art/x.md", content, check_name=False)
    run(state.indexer.index_note("gallery/art/x.md", content))
    assert run(gallery.count_pins(state)) == 0
    with pytest.raises(NoteError):
        run(gallery.get_pin(state, "gallery/art/x.md"))


# -- boards and grids --------------------------------------------------------------------


def test_boards_list_newest_first_with_a_three_picture_collage(state):
    for shade in range(4):
        add(state, color=(10 * shade, 0, 0), board="art")
    add(state, board="Life quotes")
    run(gallery.create_board(state, "empty one"))
    boards = run(gallery.list_boards(state))
    names = [b.name for b in boards]
    assert names == ["Life quotes", "art", "empty one"]
    art = boards[1]
    assert art.count == 4 and len(art.covers) == 3
    assert boards[2].count == 0 and boards[2].covers == []


def test_pins_page_newest_first_and_filter_by_board(state):
    paths = [add(state, color=(i, i, i), board="art" if i % 2 else "y")[0].path for i in range(5)]
    cards, total = run(gallery.list_pins(state, None, offset=0, limit=3))
    assert total == 5 and len(cards) == 3
    art, total_art = run(gallery.list_pins(state, "art"))
    assert total_art == 2 and {c.path for c in art} == {paths[1], paths[3]}


def test_move_rename_and_delete_boards(state):
    pin, _ = add(state, board="art")
    moved = run(gallery.move_pin(state, pin.path, "dreamcore"))
    assert moved.board == "dreamcore"
    assert not state.store.exists(pin.path)
    renamed = run(gallery.rename_board(state, "dreamcore", "dream core"))
    assert renamed == "dream core"
    [card], _ = run(gallery.list_pins(state, "dream core"))
    assert card.path.startswith("gallery/dream core/")
    assert run(gallery.delete_board(state, "dream core")) == 1
    assert run(gallery.count_pins(state)) == 0


def test_moving_onto_a_board_that_has_the_picture_is_a_conflict(state):
    a, _ = add(state, board="art")
    add(state, board="y")
    with pytest.raises(NoteError) as exc:
        run(gallery.move_pin(state, a.path, "y"))
    assert exc.value.status == 409


def test_update_changes_only_what_is_sent_and_checks_the_revision(state):
    pin, _ = add(state, title="Old")
    updated = run(
        gallery.update_pin(
            state, pin.path, description="A castle.", tags=["#Watercolor", "castle", "castle"],
            base_revision=pin.revision,
        )
    )
    assert updated.title == "Old"
    assert updated.description == "A castle."
    assert updated.tags == ["watercolor", "castle"]
    with pytest.raises(NoteError) as exc:
        run(gallery.update_pin(state, pin.path, title="x", base_revision=pin.revision))
    assert exc.value.status == 409


READING = {
    "title": "Red Square",
    "description": "A flat red square.",
    "kind": "Digital Painting",
    "era": None,
    "works": [],
    "themes": ["red", "flat", "#Minimal"],
}


def test_a_reading_is_hidden_from_the_owner_and_is_not_an_edit(state):
    pin, _ = add(state)
    run(gallery.update_pin(state, pin.path, description="Mine."))
    modified = fm.parse(state.store.read(pin.path))[0]["modified"]
    read = run(gallery.store_description(state, pin.path, READING))
    # What the owner sees stays theirs.
    assert (read.title, read.description, read.tags) == ("", "Mine.", [])
    assert read.described
    meta = fm.parse(state.store.read(pin.path))[0]
    assert meta["modified"] == modified
    reading = run(gallery.get_reading(state, pin.path))
    assert reading["kind"] == "digital painting"
    assert reading["themes"] == ["red", "flat", "minimal"]
    # The reading is searched all the same.
    assert [c.path for c in run(gallery_search.search(state, "square"))] == [pin.path]
    # Read once; a second landing without force changes nothing.
    run(gallery.store_description(state, pin.path, {**READING, "title": "Other"}))
    assert run(gallery.get_reading(state, pin.path))["title"] == "Red Square"
    run(gallery.store_description(state, pin.path, {**READING, "title": "Other"}, force=True))
    assert run(gallery.get_reading(state, pin.path))["title"] == "Other"


def test_a_reading_for_a_deleted_picture_is_dropped(state):
    pin, _ = add(state)
    run(gallery.delete_pin(state, pin.path))
    assert run(gallery.store_description(state, pin.path, READING)) is None


def test_weighted_themes_rank_works_and_era_above_plain_themes():
    reading = gallery.clean_reading(
        {"kind": "video game screenshot", "era": "1990s", "works": ["Final Fantasy VII"], "themes": ["jrpg", "1990s"]}
    )
    assert gallery.weighted_themes(reading) == [
        ("final fantasy vii", 2.0),
        ("1990s", 1.5),
        ("video game screenshot", 1.2),
        ("jrpg", 1.0),
    ]


# -- related and search ------------------------------------------------------------------


@pytest.fixture
def seen(state, tmp_path):
    state.gallery_vectors = GalleryVectors(tmp_path / "vec", embedder_factory=ColourEmbedder)
    return state


def embed_all(state):
    for card in run(gallery.list_pins(state, None, limit=500))[0]:
        pin = run(gallery.get_pin(state, card.path))
        run(gallery_jobs._embed(state, pin))


def test_related_puts_look_alikes_first(seen):
    red, _ = add(seen, color=(220, 20, 20), board="art")
    near, _ = add(seen, color=(200, 40, 30), board="y")
    far, _ = add(seen, color=(20, 20, 220), board="art")
    embed_all(seen)
    related = run(gallery_search.related(seen, red.path))
    assert [c.path for c in related] == [near.path, far.path]


def test_related_reads_shared_tags_when_there_is_no_image_model(state):
    a, _ = add(state, color=(1, 1, 1))
    b, _ = add(state, color=(2, 2, 2))
    c, _ = add(state, color=(3, 3, 3))
    run(gallery.update_pin(state, a.path, tags=["ink", "portrait"]))
    run(gallery.update_pin(state, b.path, tags=["oil"]))
    run(gallery.update_pin(state, c.path, tags=["ink", "portrait", "manga"]))
    related = run(gallery_search.related(state, a.path))
    assert [x.path for x in related][:1] == [c.path]
    # Every other picture still comes back, after the related ones.
    assert {x.path for x in related} == {b.path, c.path}


def test_search_finds_words_and_looks(seen):
    castle, _ = add(seen, color=(20, 20, 230), title="Castle in the sky")
    red, _ = add(seen, color=(230, 20, 20))
    green, _ = add(seen, color=(20, 230, 20), board="y")
    embed_all(seen)
    assert [c.path for c in run(gallery_search.search(seen, "castle"))] == [castle.path]
    assert [c.path for c in run(gallery_search.search(seen, "red"))][:1] == [red.path]
    assert [c.path for c in run(gallery_search.search(seen, "green", board="art"))] == []
    assert [c.path for c in run(gallery_search.search(seen, "green", board="y"))] == [green.path]


def test_vectors_persist_by_digest_and_ignore_another_model(seen, tmp_path):
    pin, _ = add(seen)
    embed_all(seen)
    digest = gallery.image_digest(pin.image.rsplit("/", 1)[1])
    fresh = GalleryVectors(tmp_path / "vec", embedder_factory=ColourEmbedder)
    assert run(fresh.has(digest))
    import sqlite3

    conn = sqlite3.connect(fresh.path)
    conn.execute("UPDATE vectors SET model = 'something-else'")
    conn.commit()
    conn.close()
    assert not run(GalleryVectors(tmp_path / "vec", embedder_factory=ColourEmbedder).has(digest))


def test_an_image_model_that_will_not_load_switches_the_look_off(state, tmp_path):
    def broken():
        raise RuntimeError("no model files")

    state.gallery_vectors = GalleryVectors(tmp_path / "vec", embedder_factory=broken)
    pin, _ = add(state)
    run(gallery_jobs._embed(state, pin))
    assert not state.gallery_vectors.available
    assert state.gallery_vectors.error == "no model files"
    assert [c.path for c in run(gallery_search.related(state, pin.path))] == []


# -- the API ---------------------------------------------------------------------------


def client_for(state):
    return make_client(state, gallery_api.router, notes_api.router)


def upload(client, color=(10, 200, 10), board="art", title=""):
    resp = client.post(
        "/api/gallery/upload",
        files={"file": ("leaf.png", png(color), "image/png")},
        data={"board": board, "title": title},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_api_upload_list_read_update_move_delete(state):
    client = client_for(state)
    made = upload(client, title="Leaf")
    pin = made["pin"]
    assert made["created"] and pin["board"] == "art" and pin["title"] == "Leaf"
    assert "leaf-" in pin["image"]
    overview = client.get("/api/gallery").json()
    assert overview["total"] == 1 and overview["boards"][0]["name"] == "art"
    listed = client.get("/api/gallery/pins", params={"board": "art"}).json()
    assert [p["path"] for p in listed["pins"]] == [pin["path"]] and listed["next"] is None
    item = client.get(f"/api/gallery/item/{pin['path']}").json()
    assert item["status"] == {"working": False, "error": None} or item["status"]["error"] is None
    put = client.put(
        f"/api/gallery/item/{pin['path']}",
        json={"description": "A leaf.", "board": "y", "base_revision": item["revision"]},
    )
    assert put.status_code == 200, put.text
    assert put.json()["board"] == "y" and put.json()["description"] == "A leaf."
    found = client.get("/api/gallery/pins", params={"q": "leaf"}).json()
    assert [p["path"] for p in found["pins"]] == [put.json()["path"]]
    assert client.delete(f"/api/gallery/item/{put.json()['path']}").status_code == 200
    assert client.get("/api/gallery").json()["total"] == 0


def test_api_thumbnail_is_a_small_webp(state):
    client = client_for(state)
    buffer = io.BytesIO()
    Image.new("RGB", (1600, 900), (5, 5, 5)).save(buffer, format="JPEG")
    resp = client.post(
        "/api/gallery/upload",
        files={"file": ("big.jpg", buffer.getvalue(), "image/jpeg")},
        data={"board": ""},
    )
    pin = resp.json()["pin"]
    assert pin["board"] == "" and pin["path"].count("/") == 1
    thumb = client.get(pin["thumb"])
    assert thumb.status_code == 200
    assert thumb.headers["content-type"] == "image/webp"
    assert "immutable" in thumb.headers["cache-control"]
    with Image.open(io.BytesIO(thumb.content)) as image:
        assert image.size == (480, 270)
    assert client.get("/api/gallery/thumb/..%2Fnotes.md").status_code == 404


def test_api_boards(state):
    client = client_for(state)
    assert client.post("/api/gallery/boards", json={"name": "Wishlist"}).json() == {"name": "Wishlist"}
    upload(client, board="Wishlist")
    assert client.post(
        "/api/gallery/boards/rename", json={"name": "Wishlist", "new_name": "Wants"}
    ).json() == {"name": "Wants"}
    assert [b["name"] for b in client.get("/api/gallery").json()["boards"]] == ["Wants"]
    assert client.post("/api/gallery/boards/delete", json={"name": "Wants"}).json()["deleted"] == 1
    # The section's own folder is never renamed or deleted from here.
    assert client.post("/api/gallery/boards/delete", json={"name": ""}).status_code == 400


def test_api_link_to_a_page_saves_its_og_image(state, monkeypatch):
    async def fetch(url):
        return recipe_import.FetchedPage(
            blocks=[], text="", url="https://site.example/pin/1", title="Site",
            image="https://cdn.example/castle.png", og_title="The Image Boards",
        )

    fetched = []

    async def fetch_image(url):
        fetched.append(url)
        return png((1, 2, 3)), "image/png"

    monkeypatch.setattr(recipe_import, "fetch", fetch)
    monkeypatch.setattr(gallery_api.media_lookup, "fetch_image_bytes", fetch_image)
    client = client_for(state)
    resp = client.post("/api/gallery/link", json={"url": "site.example/pin/1", "board": "art"})
    assert resp.status_code == 201, resp.text
    pin = resp.json()["pin"]
    assert fetched == ["https://cdn.example/castle.png"]
    assert pin["title"] == "The Image Boards"
    assert pin["source_url"] == "https://site.example/pin/1"


def test_api_link_to_a_page_without_a_picture_says_so(state, monkeypatch):
    async def fetch(url):
        return recipe_import.FetchedPage(blocks=[], text="", url=url, title="x")

    monkeypatch.setattr(recipe_import, "fetch", fetch)
    resp = client_for(state).post("/api/gallery/link", json={"url": "https://a.example/"})
    assert resp.status_code == 422


def test_page_reader_collects_og_image_and_title():
    page = recipe_import._read_html(
        b'<html><head><title>T</title><meta property="og:title" content="Boards">'
        b'<meta property="og:image" content="/img/a.png?x=1&amp;y=2"></head><body>hi</body></html>',
        "text/html",
        "https://site.example/p/1",
    )
    assert page.image == "https://site.example/img/a.png?x=1&y=2"
    assert page.og_title == "Boards"


def read_as(state, pin, **reading):
    run(gallery.store_description(state, pin.path, {"title": "", "description": "", "kind": "",
                                                   "era": None, "works": [], "themes": [], **reading}))


GAME = {"kind": "video game screenshot", "era": "1990s", "works": ["playstation"]}


def test_related_follows_themes_not_colours(seen):
    ff7, _ = add(seen, color=(30, 160, 60), board="y")
    mgs, _ = add(seen, color=(210, 210, 220), board="w")
    mario, _ = add(seen, color=(220, 40, 40), board="art")
    # A fashion photo the same green as the first game.
    outfit, _ = add(seen, color=(32, 158, 62), board="y")
    read_as(seen, ff7, themes=["jrpg", "low poly", "retro gaming"], works=["final fantasy vii", "playstation"], kind=GAME["kind"], era="1990s")
    read_as(seen, mgs, themes=["stealth", "low poly", "retro gaming"], works=["metal gear solid", "playstation"], kind=GAME["kind"], era="1990s")
    read_as(seen, mario, themes=["platformer", "retro gaming"], works=["nintendo 64"], kind=GAME["kind"], era="1990s")
    read_as(seen, outfit, themes=["y2k", "street style"], kind="fashion photo", era="2000s")
    embed_all(seen)
    related = [c.path for c in run(gallery_search.related(seen, ff7.path))]
    assert related[:2] == [mgs.path, mario.path]
    assert related[-1] == outfit.path


def test_theme_scores_count_rare_themes_more():
    others = {
        "a": {"illustration": 1.0},
        "b": {"illustration": 1.0, "playstation": 2.0},
        "c": {"illustration": 1.0},
    }
    mine = {"illustration": 1.0, "playstation": 2.0}
    scores = dict(gallery_search.theme_scores(mine, others))
    assert scores["b"] == 1.0
    assert scores["a"] < 0.12  # sharing only what everything shares is no theme


def test_search_keeps_only_readings_close_enough_to_the_query(state, tmp_path):
    a, _ = add(state, color=(1, 1, 1))
    b, _ = add(state, color=(2, 2, 2))
    read_as(state, a, description="castle")
    read_as(state, b, description="robot")
    axes = {"castle": [1.0, 0.0], "robot": [0.6, 0.8], "query": [1.0, 0.0]}

    async def text_embed(texts):
        return [axes["castle" if "castle" in t else "robot" if "robot" in t else "query"] for t in texts]

    state.gallery_vectors = GalleryVectors(tmp_path / "v", embedder_factory=ColourEmbedder, text_embed=text_embed, text_model="fake")
    for pin in (a, b):
        text = gallery.meaning_text(run(gallery.get_reading(state, pin.path)))
        run(state.gallery_vectors.embed_text(gallery.meaning_key(text), text))
    # castle . 1.0 to the query; robot . 0.6, under the floor.
    assert [c.path for c in run(gallery_search.search(state, "fortress"))] == [a.path]
