"""The tag endpoints, including nested tags (#project/alpha) whose slash
arrives percent-decoded in the request path."""
import asyncio

from app.api import meta as meta_api

from test_task_boards_api import make_client


def _index(state, path, text):
    async def run():
        content = state.store.create(path, text)
        await state.indexer.index_note(path, content)

    asyncio.run(run())


def test_nested_tag_notes_route_matches_the_decoded_slash(state):
    _index(state, "Alpha.md", "# Alpha\n\nKickoff notes #project/alpha\n")
    _index(state, "Plain.md", "# Plain\n\nBacklog #project\n")
    client = make_client(state, meta_api.router)

    nested = client.get("/api/tags/project%2Falpha/notes")
    assert nested.status_code == 200
    assert [n["path"] for n in nested.json()] == ["Alpha.md"]

    plain = client.get("/api/tags/project/notes")
    assert plain.status_code == 200
    assert [n["path"] for n in plain.json()] == ["Plain.md"]

    tags = {t["tag"] for t in client.get("/api/tags").json()}
    assert {"project", "project/alpha"} <= tags
