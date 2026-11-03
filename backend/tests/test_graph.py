"""The graph endpoint: notes as nodes, resolved wikilinks as edges."""
import asyncio

from app.api import meta as meta_api

from test_task_boards_api import make_client


def test_graph_resolves_wikilinks_by_stem(state):
    async def run():
        content = state.store.create(
            "Journal.md", "# J\n\nSee [[Welcome]] and [[No Such Note]].\n"
        )
        await state.indexer.index_note("Journal.md", content)

    asyncio.run(run())
    client = make_client(state, meta_api.router)
    data = client.get("/api/graph").json()

    paths = {n["path"] for n in data["nodes"]}
    assert {"Journal.md", "Welcome.md"} <= paths
    assert not any(p.startswith("templates/") for p in paths)

    edges = {(e["source"], e["target"]) for e in data["edges"]}
    assert ("Journal.md", "Welcome.md") in edges
    # A wikilink to a note that doesn't exist is not an edge.
    assert not any(t == "No Such Note.md" for _s, t in edges)
