"""Unified search and Related, at the route: handwriting is opt-in, a cold
embedding model never holds a request, and case folding is Unicode-aware."""
import asyncio

from app.api import meta as meta_api
from app.index.semantic import search_filter
from test_hybrid_search import StubSemantic, hit, seed
from test_paper_index import PAGE_A, seed_notebook, seed_page, sync
from test_task_boards_api import make_client

PAPER_CLAUSE = "path NOT LIKE 'paper/%' ESCAPE '\\'"


class ColdSemantic(StubSemantic):
    """An index whose model has not loaded yet."""

    ready = False

    def __init__(self, hits: list[dict]):
        super().__init__(hits)
        self.warmed = 0

    def warm(self):
        self.warmed += 1


def unified(state, **params) -> dict:
    return make_client(state, meta_api.router).get("/api/search/unified", params=params).json()


# -- handwriting stays out of note-only results ------------------------------


def test_search_keeps_paper_pages_out_unless_asked():
    assert PAPER_CLAUSE in search_filter(None, None)
    assert search_filter(None, None, include_paper=True) == ""
    scoped = search_filter("School/", "media/")
    assert "path LIKE 'School/%'" in scoped
    assert "path NOT LIKE 'media/%'" in scoped
    assert PAPER_CLAUSE in scoped
    assert PAPER_CLAUSE not in search_filter("School/", "media/", include_paper=True)


def test_related_lists_notes_only(state):
    seed(state, "Hub.md", "# Hub\n\nquarterly planning")
    seed(state, "Plan.md", "# Plan\n\nquarterly planning")
    page = f"paper/Ideas.paper/pages/{PAGE_A}"
    state.semantic = StubSemantic([hit(page), hit("Plan.md"), hit("paper/Ideas.paper")])

    body = make_client(state, meta_api.router).get("/api/related/Hub.md").json()

    assert [row["path"] for row in body["related"]] == ["Plan.md"]
    assert all(row["path"].endswith(".md") for row in body["related"])
    assert not state.semantic.calls[-1]["include_paper"]


def test_unified_search_merges_paper_only_on_request(state):
    nb = seed_notebook(state, "Ideas")
    page = seed_page(state, nb, PAGE_A, "the quarterly review is on Thursday")
    sync(state)
    seed(state, "Review.md", "# Review\n\nquarterly numbers")
    state.semantic = StubSemantic([hit(page, "vector wording"), hit("Review.md")])

    default = unified(state, q="quarterly")
    assert {row["type"] for row in default["results"]} == {"note"}
    assert state.semantic.calls[-1]["include_paper"] is False

    opted = unified(state, q="quarterly", include_paper="true")
    assert [row["path"] for row in opted["results"] if row["type"] == "paper"] == [page]
    assert state.semantic.calls[-1]["include_paper"] is True

    only_paper = unified(state, q="quarterly", result_type="paper")
    assert {row["type"] for row in only_paper["results"]} == {"paper"}
    assert state.semantic.calls[-1]["include_paper"] is True


# -- a cold model never holds the request ------------------------------------


def test_unified_search_answers_with_keywords_while_the_model_warms(state):
    seed(state, "Budget.md", "# Budget\n\nthe budget is agreed")
    state.semantic = ColdSemantic([hit("Other.md")])

    body = unified(state, q="budget")

    assert [row["path"] for row in body["results"]] == ["Budget.md"]
    assert body["semantic_state"] == "warming"
    assert body["semantic_available"] is True
    assert state.semantic.warmed == 1
    assert state.semantic.calls == []


def test_unified_search_reports_ready_and_unavailable(state):
    seed(state, "Budget.md", "# Budget\n\nthe budget is agreed")
    assert unified(state, q="budget")["semantic_state"] == "unavailable"

    state.semantic = StubSemantic([hit("Budget.md")])
    body = unified(state, q="budget")
    assert body["semantic_state"] == "ready"
    assert len(state.semantic.calls) == 1


def test_related_answers_empty_while_the_model_warms(state):
    seed(state, "Hub.md", "# Hub\n\nquarterly planning")
    state.semantic = ColdSemantic([hit("Plan.md")])

    body = make_client(state, meta_api.router).get("/api/related/Hub.md").json()

    assert body == {"available": True, "related": [], "state": "warming"}
    assert state.semantic.warmed == 1
    assert state.semantic.calls == []


# -- Unicode case folding ------------------------------------------------------


def test_a_task_is_found_regardless_of_non_ascii_case(state):
    seed(state, "Work.md", "# Work\n\n- [ ] Über den Bericht\n- [ ] 100% done_x\n")

    found = unified(state, q="über", result_type="task")
    assert [row["title"] for row in found["results"]] == ["Über den Bericht"]

    # A literal match, not a LIKE pattern: `_` and `%` are just characters.
    assert unified(state, q="0%", result_type="task")["results"][0]["title"] == "100% done_x"
    assert unified(state, q="e_x", result_type="task")["results"][0]["title"] == "100% done_x"
    assert unified(state, q="d_n", result_type="task")["results"] == []


def test_an_exact_non_ascii_title_gets_the_exact_boost(state):
    seed(state, "Ärger.md", "# Ärger\n\nsome trouble")
    seed(state, "Ärger im Büro.md", "# Ärger im Büro\n\nmore trouble")

    body = unified(state, q="ärger", result_type="note")
    scores = {row["path"]: row["score"] for row in body["results"]}

    assert body["results"][0]["path"] == "Ärger.md"
    # The title (+5) and path (+4) boosts on top of the FTS pass, which alone
    # stays under 5.
    assert scores["Ärger.md"] > scores["Ärger im Büro.md"] + 5


def test_fold_is_registered_on_the_index_connection(state):
    async def run():
        cur = await state.db.execute(
            "SELECT fold(?) = fold(?), fold(?) = fold(?), fold(NULL) IS NULL",
            ("Über", "über", "Ärger", "ärger"),
        )
        return tuple(await cur.fetchone())

    assert asyncio.run(run()) == (1, 1, 1)
