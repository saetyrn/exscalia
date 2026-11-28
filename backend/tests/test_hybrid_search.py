"""Retrieval: rank-fused ask_vault, and folder scoping on the vector index.

ask_vault used to run semantic search first and only call FTS to fill the
shortfall, so a question with k strong vector hits never reached full-text
search at all — exact terms (names, course codes, error strings) lost to
neighbours that merely read like the question.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from app.index.semantic import SemanticIndex, path_filter
from app.overseer.tool_expansion import ask_vault
from app.overseer.tools import execute_tool


def hit(path: str, text: str | None = None, heading: str = "Section") -> dict:
    return {
        "path": path,
        "heading": heading,
        "text": text or f"the meaning of {path}",
        "score": 0.9,
    }


class StubSemantic:
    """A scripted vector ranking that records the scope it was asked for."""

    available = True

    def __init__(self, hits: list[dict]):
        self.hits = hits
        self.calls: list[dict] = []

    async def search(
        self, query, k=8, path_prefix=None, exclude_prefix=None, include_paper=False
    ):
        self.calls.append(
            {
                "query": query,
                "k": k,
                "path_prefix": path_prefix,
                "exclude_prefix": exclude_prefix,
                "include_paper": include_paper,
            }
        )
        return list(self.hits)


class BrokenSemantic:
    available = True

    async def search(self, *args, **kwargs):
        raise RuntimeError("the vector store fell over")


def seed(state, path: str, body: str) -> None:
    created = state.store.create(path, body)
    asyncio.run(state.indexer.index_note(path, created))


def ask(state, **inp) -> dict:
    return asyncio.run(ask_vault(state, "t", inp))


def run_tool(state, name: str, inp: dict) -> dict:
    return json.loads(asyncio.run(execute_tool(state, "s", name, inp)))


# --- reciprocal-rank fusion --------------------------------------------------


def test_a_note_both_searches_find_outranks_either_ones_favourite(state):
    """The whole point of fusing: agreement beats a single list's top hit."""
    seed(state, "Both.md", "the PRICING9 decision was annual billing")
    seed(state, "Alpha.md", "unrelated musings")
    seed(state, "Zeta.md", "also unrelated")
    state.semantic = StubSemantic([hit("Alpha.md"), hit("Both.md"), hit("Zeta.md")])

    result = ask(state, query="PRICING9", k=3)

    assert [s["path"] for s in result["sources"]] == ["Both.md", "Alpha.md", "Zeta.md"]


def test_the_excerpt_comes_from_whichever_search_ranked_the_note_better(state):
    seed(state, "Both.md", "the PRICING9 decision was annual billing")
    seed(state, "Alpha.md", "unrelated musings")
    state.semantic = StubSemantic([hit("Alpha.md"), hit("Both.md")])

    sources = {s["path"]: s for s in ask(state, query="PRICING9", k=2)["sources"]}

    # Both.md is #1 on the keyword side and #2 on the semantic side, so it
    # carries the FTS snippet; Alpha.md is semantic-only.
    assert set(sources["Both.md"]) == {"path", "title", "snippet"}
    assert "PRICING9" in sources["Both.md"]["snippet"]
    assert set(sources["Alpha.md"]) == {"path", "heading", "excerpt"}
    assert sources["Alpha.md"]["excerpt"] == "the meaning of Alpha.md"


def test_a_full_semantic_list_no_longer_shuts_out_the_exact_match(state):
    """The regression this replaced: k strong vector hits meant FTS never ran."""
    seed(state, "Pricing.md", "we settled on PRICING9 annual billing")
    state.semantic = StubSemantic(
        [hit("S1.md"), hit("S2.md"), hit("S3.md"), hit("S4.md")]
    )

    paths = [s["path"] for s in ask(state, query="PRICING9", k=2)["sources"]]

    assert len(paths) == 2
    assert "Pricing.md" in paths


def test_top_k_is_respected_across_both_lists(state):
    seed(state, "Pricing.md", "PRICING9 again")
    state.semantic = StubSemantic([hit(f"S{i}.md") for i in range(6)])

    assert len(ask(state, query="PRICING9", k=3)["sources"]) == 3


def test_repeated_chunks_of_one_note_collapse_to_its_best(state):
    state.semantic = StubSemantic(
        [
            hit("Dup.md", "first chunk"),
            hit("Dup.md", "second chunk"),
            hit("Other.md", "elsewhere"),
        ]
    )

    sources = ask(state, query="zzzunfindable", k=3)["sources"]

    assert [s["path"] for s in sources] == ["Dup.md", "Other.md"]
    assert sources[0]["excerpt"] == "first chunk"


def test_a_broken_vector_store_still_leaves_the_keyword_half(state):
    seed(state, "Pricing.md", "PRICING9 annual billing")
    state.semantic = BrokenSemantic()

    assert [s["path"] for s in ask(state, query="PRICING9")["sources"]] == ["Pricing.md"]


def test_an_empty_ask_vault_states_the_fact_instead_of_inviting_citations(state):
    state.semantic = StubSemantic([])

    result = ask(state, query="zzzunfindable")

    assert result["sources"] == []
    assert "zzzunfindable" in result["note"]
    assert "cite" not in result["note"]


def test_ask_vault_keeps_its_citation_note_when_it_found_something(state):
    seed(state, "Pricing.md", "PRICING9 annual billing")
    state.semantic = StubSemantic([])

    assert "cite the note paths" in ask(state, query="PRICING9")["note"]


# --- folder scoping ----------------------------------------------------------


def test_ask_vault_scopes_both_halves_to_the_folder(state):
    seed(state, "School/Chem.md", "the PRICING9 lab budget")
    seed(state, "media/Film.md", "PRICING9 for the film festival")
    state.semantic = StubSemantic([])

    result = ask(state, query="PRICING9", path_prefix="School/")

    assert [s["path"] for s in result["sources"]] == ["School/Chem.md"]
    assert state.semantic.calls[0]["path_prefix"] == "School/"


def test_ask_vault_excludes_the_folder_from_both_halves(state):
    seed(state, "School/Chem.md", "the PRICING9 lab budget")
    seed(state, "media/Film.md", "PRICING9 for the film festival")
    state.semantic = StubSemantic([])

    result = ask(state, query="PRICING9", exclude_prefix="media/")

    assert [s["path"] for s in result["sources"]] == ["School/Chem.md"]
    assert state.semantic.calls[0]["exclude_prefix"] == "media/"


def test_an_empty_scoped_ask_vault_names_the_scope(state):
    seed(state, "media/Film.md", "PRICING9 for the film festival")
    state.semantic = StubSemantic([])

    result = ask(state, query="PRICING9", path_prefix="School/")

    assert result["sources"] == []
    assert "School/" in result["note"]


def test_semantic_search_threads_the_scope_and_names_it_when_empty(state):
    state.semantic = StubSemantic([])

    out = run_tool(
        state,
        "semantic_search",
        {"query": "budgets", "path_prefix": "School/", "exclude_prefix": "media/"},
    )

    assert out["results"] == []
    assert "School/" in out["note"] and "media/" in out["note"]
    assert state.semantic.calls[0]["path_prefix"] == "School/"
    assert state.semantic.calls[0]["exclude_prefix"] == "media/"


# --- the LanceDB predicate ---------------------------------------------------


def test_no_scope_produces_no_predicate():
    assert path_filter(None, None) == ""
    assert path_filter("", []) == ""


def test_includes_are_ored_and_excludes_anded():
    clause = path_filter(["School/", "Work/"], "School/Archive/")
    assert clause == (
        "(path LIKE 'School/%' ESCAPE '\\' OR path LIKE 'Work/%' ESCAPE '\\') "
        "AND path NOT LIKE 'School/Archive/%' ESCAPE '\\'"
    )


@pytest.fixture
def lance_index(tmp_path):
    """A SemanticIndex over a real LanceDB table, with the embedding model
    stubbed out — the filter is what is under test, not the vectors."""

    def build(paths: list[str]) -> SemanticIndex:
        import lancedb
        import pyarrow as pa

        schema = pa.schema(
            [
                pa.field("id", pa.string()),
                pa.field("path", pa.string()),
                pa.field("heading", pa.string()),
                pa.field("text", pa.string()),
                pa.field("hash", pa.string()),
                pa.field("vector", pa.list_(pa.float32(), 2)),
            ]
        )
        table = lancedb.connect(tmp_path / "lancedb").create_table("chunks", schema=schema)
        table.add(
            [
                {
                    "id": f"{p}#0",
                    "path": p,
                    "heading": "",
                    "text": p,
                    "hash": "h",
                    "vector": [1.0, 0.0],
                }
                for p in paths
            ]
        )

        index = SemanticIndex(tmp_path, "stub-model")
        index._table = table
        index._model = SimpleNamespace(embed=lambda texts: iter([[1.0, 0.0]]))

        async def ready():
            return True

        index._ensure = ready
        return index

    return build


def search(index, **kwargs) -> list[str]:
    return sorted(h["path"] for h in asyncio.run(index.search("anything", **kwargs)))


def test_path_prefix_keeps_only_the_folder(lance_index):
    index = lance_index(["School/Chem.md", "School/Math.md", "media/Film.md", "Top.md"])
    assert search(index, k=10, path_prefix="School/") == ["School/Chem.md", "School/Math.md"]


def test_exclude_prefix_removes_the_folder(lance_index):
    index = lance_index(["School/Chem.md", "media/Film.md", "Top.md"])
    assert search(index, k=10, exclude_prefix="media/") == ["School/Chem.md", "Top.md"]


def test_an_unscoped_search_is_unchanged(lance_index):
    index = lance_index(["School/Chem.md", "media/Film.md"])
    assert search(index, k=10) == ["School/Chem.md", "media/Film.md"]


def test_an_underscore_in_a_folder_name_is_not_a_wildcard(lance_index):
    """LIKE would otherwise read `School_x/` as "School, any character, x/"."""
    index = lance_index(["School_x/A.md", "SchoolYx/B.md"])
    assert search(index, k=10, path_prefix="School_x/") == ["School_x/A.md"]


def test_paper_pages_come_back_only_when_asked_for(lance_index):
    """Transcripts share the table under a page directory, which is no note
    path — only unified search maps those, so only it asks."""
    page = "paper/Ideas.paper/pages/" + "a" * 32
    index = lance_index(["Top.md", page])
    assert search(index, k=10) == ["Top.md"]
    assert search(index, k=10, include_paper=True) == ["Top.md", page]
    assert search(index, k=10, exclude_prefix="media/", include_paper=True) == ["Top.md", page]
