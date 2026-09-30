"""The Paper index: search, counts, the change feed and the semantic hand-off.

The index is a cache, so every test here starts from files on disk and asks
whether the tables agree with them — including the one test that matters most
for the iPad, which pins the indexed change feed against the walking one that
defines what the feed means.
"""

import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.api import meta as meta_api
from app.index.highlight import HIGHLIGHT_OPEN
from app.api import paper as paper_api
from app.vault.notes import NoteError

PAGE_A = "a" * 32
PAGE_B = "b" * 32


# -- vault fixtures ----------------------------------------------------------


def seed_notebook(state, title="Ideas", folder=None) -> str:
    return state.paper.create_notebook(title=title, folder=folder)["path"]


def seed_page(state, notebook, page_id, transcript="", **page):
    """A page written the way the iPad writes one: source then derived text."""
    state.paper.save_page(
        notebook,
        page_id,
        base_revision=0,
        page=page or {},
        drawing=b"ink",
        text={"lines": [], "transcript": transcript} if transcript else None,
    )
    return f"{notebook}/pages/{page_id}"


def sync(state, force=False) -> int:
    return asyncio.run(state.paper_index.sync_all(force=force))


def rows(state, table) -> list[dict]:
    async def run():
        return [
            dict(row)
            for row in await state.db.execute_fetchall(f"SELECT * FROM {table}")
        ]

    return asyncio.run(run())


# -- sync --------------------------------------------------------------------


def test_sync_builds_both_tables_from_disk(state):
    nb = seed_notebook(state, "Ideas", folder="Work")
    seed_page(state, nb, PAGE_A, "quarterly review notes")
    seed_page(state, nb, PAGE_B)

    assert sync(state) == 2
    notebooks = rows(state, "paper_notebooks")
    assert [row["path"] for row in notebooks] == [nb]
    assert notebooks[0]["title"] == "Ideas"
    assert notebooks[0]["folder"] == "paper/Work"
    assert notebooks[0]["page_count"] == 2

    pages = {row["path"]: row for row in rows(state, "paper_pages")}
    assert set(pages) == {f"{nb}/pages/{PAGE_A}", f"{nb}/pages/{PAGE_B}"}
    first = pages[f"{nb}/pages/{PAGE_A}"]
    assert first["position"] == 0 and first["revision"] == 1
    assert first["transcript"] == "quarterly review notes"
    assert first["title"] == "Page 1"
    # The untranscribed page still has a row; it is simply unsearchable.
    assert pages[f"{nb}/pages/{PAGE_B}"]["transcript"] == ""

    assert asyncio.run(state.paper_index.count_pages()) == 2
    assert asyncio.run(state.paper_index.count_transcribed()) == 1


def test_sync_skips_unchanged_pages_and_purges_missing_ones(state):
    nb = seed_notebook(state)
    seed_page(state, nb, PAGE_A, "ink and paper")
    assert sync(state) == 1
    # Nothing moved: the fingerprint (revision, text_revision, mtime) matches.
    assert sync(state) == 0
    assert sync(state, force=True) == 1

    # A page removed behind the index's back is purged on the next reconcile.
    state.paper.delete_page(nb, PAGE_A)
    assert sync(state) == 0
    assert rows(state, "paper_pages") == []
    assert state.semantic.removed[-1] == f"{nb}/pages/{PAGE_A}"


def test_sync_drops_a_notebook_that_left_the_vault(state):
    nb = seed_notebook(state)
    seed_page(state, nb, PAGE_A, "gone soon")
    sync(state)
    state.paper.delete_notebook(nb)
    sync(state)
    assert rows(state, "paper_notebooks") == []
    assert rows(state, "paper_pages") == []


def test_a_text_object_supplies_the_page_title(state):
    nb = seed_notebook(state)
    seed_page(
        state,
        nb,
        PAGE_A,
        "handwriting",
        objects=[{"type": "text", "text": "Budget meeting\nsecond line"}],
    )
    sync(state)
    assert rows(state, "paper_pages")[0]["title"] == "Budget meeting"


# -- search ------------------------------------------------------------------


def test_search_finds_the_page_and_marks_the_match(state):
    nb = seed_notebook(state, "Ideas")
    seed_page(state, nb, PAGE_A, "the quarterly review is on Thursday")
    seed_page(state, nb, PAGE_B, "shopping list: milk")
    sync(state)

    hits = asyncio.run(state.paper_index.search("quarterly"))
    assert [hit["path"] for hit in hits] == [f"{nb}/pages/{PAGE_A}"]
    hit = hits[0]
    assert hit["notebook"] == nb
    assert hit["notebook_title"] == "Ideas"
    assert hit["page_id"] == PAGE_A and hit["position"] == 0
    assert "quarterly" in hit["snippet"]
    # The snippet is plain text; the marked-up run is reported separately.
    assert HIGHLIGHT_OPEN not in hit["snippet"]
    assert any(
        segment["highlight"] and "quarterly" in segment["text"].lower()
        for segment in hit["highlights"]
    )
    assert hit["url"] == f"/paper/Ideas.paper#{PAGE_A}"


def test_search_honours_the_notebook_filter_and_odd_queries(state):
    work = seed_notebook(state, "Work")
    home = seed_notebook(state, "Home")
    seed_page(state, work, PAGE_A, "budget planning")
    seed_page(state, home, PAGE_B, "budget for the kitchen")
    sync(state)

    assert len(asyncio.run(state.paper_index.search("budget"))) == 2
    only_work = asyncio.run(state.paper_index.search("budget", notebook=work))
    assert [hit["notebook"] for hit in only_work] == [work]
    # Without the paper/ prefix, the way a URL spells it.
    assert asyncio.run(state.paper_index.search("budget", notebook="Work.paper"))
    # FTS syntax typed by accident must not raise; the words still match.
    assert asyncio.run(state.paper_index.search('budget" NEAR( -'))
    assert asyncio.run(state.paper_index.search("   ")) == []
    assert asyncio.run(state.paper_index.search("budget", notebook="not-a-notebook")) == []


def test_removing_a_page_clears_both_tables(state):
    nb = seed_notebook(state)
    seed_page(state, nb, PAGE_A, "findable text")
    sync(state)
    assert asyncio.run(state.paper_index.search("findable"))

    asyncio.run(state.paper_index.remove_page(f"{nb}/pages/{PAGE_A}"))
    assert rows(state, "paper_pages") == []
    assert rows(state, "paper_fts") == []
    assert asyncio.run(state.paper_index.search("findable")) == []


def test_removing_a_notebook_clears_its_pages(state):
    nb = seed_notebook(state)
    seed_page(state, nb, PAGE_A, "findable text")
    seed_page(state, nb, PAGE_B, "more text")
    sync(state)

    asyncio.run(state.paper_index.remove_notebook(nb))
    assert rows(state, "paper_notebooks") == []
    assert rows(state, "paper_pages") == []
    assert rows(state, "paper_fts") == []
    assert set(state.semantic.removed) >= {
        f"{nb}/pages/{PAGE_A}",
        f"{nb}/pages/{PAGE_B}",
    }


# -- semantic hand-off -------------------------------------------------------


def test_a_transcript_is_handed_to_the_semantic_index(state):
    nb = seed_notebook(state, "Ideas")
    seed_page(state, nb, PAGE_A, "the quarterly review is on Thursday")
    seed_page(state, nb, PAGE_B)
    sync(state)

    indexed = dict(state.semantic.indexed)
    assert f"{nb}/pages/{PAGE_A}" in indexed
    document = indexed[f"{nb}/pages/{PAGE_A}"]
    assert document.startswith("---\ntitle: Ideas · Page 1\n---\n\n")
    assert "quarterly review" in document
    # An untranscribed page is nothing to embed.
    assert f"{nb}/pages/{PAGE_B}" not in indexed

    docs = dict(asyncio.run(state.paper_index.semantic_documents()))
    assert list(docs) == [f"{nb}/pages/{PAGE_A}"]
    assert docs[f"{nb}/pages/{PAGE_A}"] == document


def test_an_emptied_transcript_removes_the_vector(state):
    nb = seed_notebook(state)
    page_path = seed_page(state, nb, PAGE_A, "temporary words")
    sync(state)
    assert page_path in dict(state.semantic.indexed)

    current = state.paper.read_page(nb, PAGE_A)
    state.paper.save_page(
        nb,
        PAGE_A,
        base_revision=int(current["revision"]),
        text={"lines": [], "transcript": ""},
    )
    asyncio.run(state.paper_index.index_page(page_path))
    assert state.semantic.removed[-1] == page_path


def test_the_index_survives_a_state_with_no_semantic_index(state):
    nb = seed_notebook(state)
    seed_page(state, nb, PAGE_A, "still indexed")
    state.paper_index.semantic = None
    assert sync(state) == 1
    assert asyncio.run(state.paper_index.search("indexed"))


# -- change feed -------------------------------------------------------------


def build_vault(state):
    """A vault with a bit of everything the feed has to describe."""
    work = seed_notebook(state, "Work", folder="Projects")
    home = seed_notebook(state, "Home")
    seed_page(state, work, PAGE_A, "kickoff notes")
    seed_page(state, work, PAGE_B)
    seed_page(state, home, PAGE_A)
    state.paper.delete_page(home, PAGE_A)
    sync(state)
    return work, home


@pytest.mark.parametrize("since", [0, 1, 3, 5, 99])
def test_the_indexed_feed_matches_the_walker(state, since):
    build_vault(state)
    epoch = state.paper.seq_state()["epoch"]
    walked = paper_api.changes_since(state.paper, epoch, since)
    indexed = asyncio.run(state.paper_index.changes_since(epoch, since))
    assert indexed == walked


def test_an_unknown_epoch_forces_a_full_answer(state):
    build_vault(state)
    indexed = asyncio.run(state.paper_index.changes_since("not-the-epoch", 3))
    assert indexed["full"] is True
    assert indexed["deleted"] == []
    assert len(indexed["pages"]) == 2
    assert indexed == paper_api.changes_since(state.paper, "not-the-epoch", 3)


@pytest.mark.parametrize("since", [0, 1, 5, 99])
def test_the_feeds_agree_when_a_deleted_path_is_back_on_disk(state, since):
    """The generic Trash restore (web Trash page, OMNIA) moves a page back
    without clearing its tombstone. Neither feed may report a live path as
    deleted: the client applies deletes last and would drop it again."""
    _, home = build_vault(state)
    item = next(row for row in state.trash.list() if row["kind"] == "page")
    state.trash.restore(item["id"])
    sync(state)
    epoch = state.paper.seq_state()["epoch"]
    walked = paper_api.changes_since(state.paper, epoch, since)
    indexed = asyncio.run(state.paper_index.changes_since(epoch, since))
    assert indexed == walked
    assert item["original_path"] not in [row["path"] for row in walked["deleted"]]


def test_a_rewound_seq_file_rotates_the_epoch_at_the_next_sync(state):
    """seq.json rides in the vault, so a `git reset` rewinds the counter while
    the epoch survives. The index is the witness that does not rewind."""
    build_vault(state)
    before = state.paper.seq_state()
    cursor = asyncio.run(state.paper_index.changes_since(before["epoch"], 0))["seq"]
    indexed = max(
        row["seq"] for row in rows(state, "paper_pages") + rows(state, "paper_notebooks")
    )

    # A boot over a consistent file keeps the epoch clients hold.
    sync(state, force=True)
    assert state.paper.seq_state() == before

    seq_file = state.paper.dir / ".exscalia" / "paper" / "seq.json"
    assert seq_file.is_file()
    rewound = {"epoch": before["epoch"], "seq": 1, "pruned": 0}
    seq_file.write_text(json.dumps(rewound))
    sync(state)

    after = state.paper.seq_state()
    assert after["epoch"] != before["epoch"]
    assert after["seq"] == indexed
    feed = asyncio.run(state.paper_index.changes_since(before["epoch"], cursor))
    assert feed["full"] is True and feed["epoch"] == after["epoch"]
    # New writes land above every seq the index still holds.
    nb = seed_notebook(state, "After")
    assert state.paper.read_notebook(nb)["seq"] > indexed

    sync(state)
    assert state.paper.seq_state()["epoch"] == after["epoch"]


def test_the_feed_waits_for_a_write_to_reach_the_index(state):
    """A route takes its seq in the store, then awaits the index. A feed read
    in between would hand out that seq as a cursor without the row."""
    work, _ = build_vault(state)
    index = state.paper_index
    epoch = state.paper.seq_state()["epoch"]
    cursor = asyncio.run(index.changes_since(epoch, 0))["seq"]

    async def run():
        async with index.feed_lock:
            seed_page(state, work, "c" * 32)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(index.changes_since(epoch, cursor), 0.2)
            await index.index_page(f"{work}/pages/{'c' * 32}")
        return await index.changes_since(epoch, cursor)

    feed = asyncio.run(run())
    assert feed["full"] is False
    assert [page["id"] for page in feed["pages"]] == ["c" * 32]
    assert feed["seq"] == state.paper.seq_state()["seq"]


def test_the_route_prefers_the_index_and_falls_back_to_the_walker(state):
    work, _ = build_vault(state)
    client = make_client(state)
    epoch = state.paper.seq_state()["epoch"]

    with_index = client.get(f"/api/paper/changes?since=2&epoch={epoch}").json()
    del state.paper_index
    without_index = client.get(f"/api/paper/changes?since=2&epoch={epoch}").json()
    assert with_index == without_index
    assert any(page["notebook"] == work for page in with_index["pages"])


# -- routes ------------------------------------------------------------------


def make_client(state) -> TestClient:
    app = FastAPI()
    app.state = state

    @app.exception_handler(NoteError)
    async def handler(_request, exc: NoteError):
        return JSONResponse(status_code=exc.status, content={"detail": exc.detail})

    app.include_router(paper_api.router)
    app.include_router(meta_api.router)
    return TestClient(app)


def test_the_search_route_returns_hits_and_explains_empty_answers(state):
    nb = seed_notebook(state, "Ideas")
    client = make_client(state)

    blank = client.get("/api/paper/search?q=budget").json()
    assert blank["results"] == []
    assert "no Paper pages yet" in blank["note"]

    seed_page(state, nb, PAGE_A)
    sync(state)
    untranscribed = client.get("/api/paper/search?q=budget").json()
    assert untranscribed["results"] == []
    assert "have been transcribed" in untranscribed["note"]

    seed_page(state, nb, PAGE_B, "the budget is agreed")
    sync(state)
    found = client.get("/api/paper/search?q=budget").json()
    assert [hit["page_id"] for hit in found["results"]] == [PAGE_B]
    assert "note" not in found

    miss = client.get("/api/paper/search?q=zzzunfindable").json()
    assert miss["results"] == [] and "Try fewer or different words" in miss["note"]

    other = seed_notebook(state, "Other")
    sync(state)
    filtered = client.get(f"/api/paper/search?q=budget&notebook={other}").json()
    assert filtered["results"] == []
    assert "drop the notebook filter" in filtered["note"]


def test_unified_search_returns_paper_pages_with_a_viewer_url(state):
    nb = seed_notebook(state, "Ideas", folder="Work")
    seed_page(state, nb, PAGE_A, "the quarterly review is on Thursday")
    sync(state)
    client = make_client(state)

    # Handwriting is opt-in: a client that predates Paper never sends the
    # flag and would open a page directory as a note.
    default = client.get("/api/search/unified?q=quarterly").json()
    assert all(row["type"] != "paper" for row in default["results"])

    body = client.get("/api/search/unified?q=quarterly&include_paper=true").json()
    paper = [row for row in body["results"] if row["type"] == "paper"]
    assert len(paper) == 1
    hit = paper[0]
    assert hit["path"] == f"{nb}/pages/{PAGE_A}"
    assert hit["title"] == "Ideas · Page 1"
    assert hit["url"] == f"/paper/Work/Ideas.paper#{PAGE_A}"
    assert "quarterly" in hit["snippet"]
    assert hit["score"] > 0

    # The type filters still partition the answer.
    only_paper = client.get("/api/search/unified?q=quarterly&result_type=paper").json()
    assert {row["type"] for row in only_paper["results"]} == {"paper"}
    only_notes = client.get(
        "/api/search/unified?q=quarterly&result_type=note&include_paper=true"
    ).json()
    assert all(row["type"] != "paper" for row in only_notes["results"])
    # A page carries no tags of its own, so a tag filter excludes handwriting.
    tagged = client.get("/api/search/unified?q=quarterly&tag=ml&include_paper=true").json()
    assert all(row["type"] != "paper" for row in tagged["results"])
    # And the folder filter is honoured against the page's vault path.
    elsewhere = client.get(
        "/api/search/unified?q=quarterly&folder=paper/Elsewhere&include_paper=true"
    ).json()
    assert all(row["type"] != "paper" for row in elsewhere["results"])


def test_unified_search_falls_back_to_paper_for_a_semantic_hit(state):
    """A vector hit whose path is a page directory used to be dropped."""
    nb = seed_notebook(state, "Ideas")
    page_path = seed_page(state, nb, PAGE_A, "vector only wording")
    sync(state)

    class Semantic:
        available = True

        async def search(
            self, q, k=8, path_prefix=None, exclude_prefix=None, include_paper=False
        ):
            return [{"path": page_path, "text": "vector only wording", "score": 0.9}]

    state.semantic = Semantic()
    client = make_client(state)
    body = client.get("/api/search/unified?q=unrelatedwords&include_paper=true").json()
    paper = [row for row in body["results"] if row["type"] == "paper"]
    assert [row["path"] for row in paper] == [page_path]
    assert paper[0]["url"] == f"/paper/Ideas.paper#{PAGE_A}"


def test_saving_a_page_through_the_api_indexes_it(state):
    """The route hooks and the index agree without a sync in between."""
    client = make_client(state)
    nb = client.post("/api/paper/notebooks", json={"title": "Ideas"}).json()["path"]
    response = client.put(
        f"/api/paper/pages/{nb}/{PAGE_A}",
        data={"base_revision": "0"},
        files={
            "text": (
                "text.json",
                json.dumps({"lines": [], "transcript": "handwritten minutes"}),
                "application/json",
            ),
            "drawing": ("drawing.pkdrawing", b"ink", "application/octet-stream"),
        },
    )
    assert response.status_code == 200, response.text
    hits = client.get("/api/paper/search?q=minutes").json()["results"]
    assert [hit["page_id"] for hit in hits] == [PAGE_A]

    client.delete(f"/api/paper/pages/{nb}/{PAGE_A}")
    assert client.get("/api/paper/search?q=minutes").json()["results"] == []


def test_search_still_finds_a_renamed_notebooks_pages(state):
    nb = seed_notebook(state, "Ideas")
    seed_page(state, nb, PAGE_A, "quarterly minutes")
    sync(state)
    client = make_client(state)

    renamed = client.post(
        f"/api/paper/notebooks/{nb}/rename",
        json={"new_path": "paper/Archive/Minutes.paper"},
    )
    assert renamed.status_code == 200, renamed.text
    moved = f"{renamed.json()['path']}/pages/{PAGE_A}"
    hits = client.get("/api/paper/search?q=quarterly").json()["results"]
    assert [hit["path"] for hit in hits] == [moved]
    assert [row["path"] for row in rows(state, "paper_pages")] == [moved]
    assert moved in dict(state.semantic.indexed)


def test_a_restored_notebook_is_searchable_and_in_the_full_feed(state):
    nb = seed_notebook(state, "Ideas")
    seed_page(state, nb, PAGE_A, "quarterly minutes")
    seed_page(state, nb, PAGE_B)
    sync(state)
    client = make_client(state)

    item = client.delete(f"/api/paper/notebooks/{nb}").json()
    assert client.get("/api/paper/search?q=quarterly").json()["results"] == []
    assert client.post(f"/api/paper/trash/{item['id']}/restore").status_code == 200

    hits = client.get("/api/paper/search?q=quarterly").json()["results"]
    assert [hit["page_id"] for hit in hits] == [PAGE_A]
    full = asyncio.run(state.paper_index.changes_since(None, 0))
    assert full["full"] is True
    assert sorted(page["id"] for page in full["pages"]) == [PAGE_A, PAGE_B]
    assert full == paper_api.changes_since(state.paper, None, 0)


def test_deleting_a_page_refreshes_its_notebook_row(state):
    nb = seed_notebook(state)
    seed_page(state, nb, PAGE_A)
    seed_page(state, nb, PAGE_B)
    sync(state)
    client = make_client(state)

    client.delete(f"/api/paper/pages/{nb}/{PAGE_A}")
    row = rows(state, "paper_notebooks")[0]
    assert row["page_count"] == 1 and row["cover"] == PAGE_B
    assert row["seq"] == state.paper.read_notebook(nb)["seq"]
