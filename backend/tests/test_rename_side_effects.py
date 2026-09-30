"""A rename from the editor or the sidebar carries what pointed at the old
path: inbound [[links]] are rewritten, and the saved sidebar order keeps the
note's (or folder's) slot. OMNIA's move and rename tools keep the order too.
The repair tool fixes links that renames broke before any of this."""
import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import meta as meta_api
from app.api import notes as notes_api
from app.index.db import content_hash
from app.overseer.tool_expansion import (
    CONFIG_PATH,
    archive_note,
    create_note,
    move_folder,
    move_note,
    move_notes,
    rename_note,
)
from app.proofread import ProofreadStore
from app.tools import repair_links
from app.vault.internal import TrashStore, atomic_json, read_json
from app.vault.notes import NoteError

from test_live_consistency import drain


def make_client(state) -> TestClient:
    app = FastAPI()
    app.state = state
    state.trash = TrashStore(state.settings.vault_dir, state.store)
    state.proofread = ProofreadStore(state.settings.data_dir)

    @app.exception_handler(NoteError)
    async def handler(_request, exc: NoteError):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=exc.status, content={"detail": exc.detail})

    app.include_router(notes_api.router)
    app.include_router(meta_api.router)
    return TestClient(app)


@pytest.fixture
def client(state):
    return make_client(state)


def add(client, path: str, content: str) -> None:
    assert client.post("/api/notes", json={"path": path, "content": content}).status_code == 200


def body(state, path: str) -> str:
    return state.store.read(path).split("---\n", 2)[-1]


def backlinks(client, path: str) -> list[str]:
    return [r["path"] for r in client.get(f"/api/backlinks/{path}").json()]


# --- inbound links ------------------------------------------------------------

LOG = (
    "[[Plan]] [[plan|the plan]] ![[Plan]] [[Plan#Goals]]\n\n"
    "```\n[[Plan]]\n```\n"
)


def test_rename_rewrites_inbound_links_and_announces_the_linker(state, client):
    add(client, "Plan.md", "# Plan\n\n## Goals\n")
    add(client, "work/Log.md", LOG)
    assert backlinks(client, "Plan.md") == ["work/Log.md"]
    queue = state.live.subscribe()

    res = client.post(
        "/api/notes/Plan.md/rename",
        json={"new_path": "Roadmap.md", "new_title": "Roadmap", "client_id": "device-1"},
    )

    assert res.status_code == 200
    data = res.json()
    assert (data["path"], data["links_updated"], data["linkers"]) == (
        "Roadmap.md",
        4,
        ["work/Log.md"],
    )
    assert data["revision"] == content_hash(data["content"])
    assert body(state, "work/Log.md").endswith(
        "[[Roadmap]] [[Roadmap|the plan]] ![[Roadmap]] [[Roadmap#Goals]]\n\n"
        "```\n[[Plan]]\n```\n"
    )
    assert backlinks(client, "Roadmap.md") == ["work/Log.md"]
    edges = {(e["source"], e["target"]) for e in client.get("/api/graph").json()["edges"]}
    assert ("work/Log.md", "Roadmap.md") in edges

    events = {(e["type"], e["path"]): e for e in drain(queue)}
    # The renaming tab may hold the linker open too, so its event carries no
    # origin; the renamed note's own events stay the renamer's echo.
    linker = events[("note", "work/Log.md")]
    assert linker["origin"] is None
    assert linker["revision"] == content_hash(state.store.read("work/Log.md"))
    assert events[("note", "Roadmap.md")]["origin"] == "device-1"


def test_rename_follows_a_link_to_itself_in_the_returned_content(state, client):
    add(client, "Plan.md", "# Plan\n\n## Goals\n\nSee [[Plan#Goals]].\n")
    queue = state.live.subscribe()

    data = client.post("/api/notes/Plan.md/rename", json={"new_path": "Roadmap.md"}).json()

    assert "See [[Roadmap#Goals]]." in data["content"]
    assert data["content"] == state.store.read("Roadmap.md")
    assert data["revision"] == content_hash(data["content"])
    assert data["linkers"] == ["Roadmap.md"]
    # One announcement for the moved note, with the rewritten revision.
    notes = [e for e in drain(queue) if e["type"] == "note"]
    assert [(e["path"], e["revision"]) for e in notes] == [("Roadmap.md", data["revision"])]


def test_folder_rename_rewrites_folder_qualified_links(state, client):
    add(client, "proj/Spec.md", "# Spec\n")
    add(client, "old/deep/Spec.md", "# Spec\n")
    add(client, "proj/Notes.md", "# Notes\n\nSee [[proj/Spec]].\n")
    add(client, "Index.md", "# Index\n\n[[proj/Spec]] and [[Spec]] and [[proj/Spec.md|spec]]\n")

    res = client.post("/api/notes/folders/rename", json={"path": "proj", "new_path": "projects"})

    assert res.status_code == 200
    data = res.json()
    assert (data["path"], data["moved"]) == ("projects", 2)
    assert (data["links_updated"], data["linkers"]) == (3, ["Index.md", "projects/Notes.md"])
    # The bare name still opens the moved note (the shallower Spec), so it is
    # left as written rather than qualified.
    assert "[[projects/Spec]] and [[Spec]] and [[projects/Spec.md|spec]]" in body(state, "Index.md")
    assert "See [[projects/Spec]]." in body(state, "projects/Notes.md")
    assert set(backlinks(client, "projects/Spec.md")) == {"Index.md", "projects/Notes.md"}


def test_rename_leaves_links_that_open_a_same_name_note_alone(state, client):
    add(client, "a/Plan.md", "# Plan\n")
    add(client, "b/Plan.md", "# Plan\n")
    add(client, "Hub.md", "# Hub\n\n[[Plan]]\n")
    before = state.store.read("Hub.md")

    data = client.post(
        "/api/notes/b/Plan.md/rename", json={"new_path": "b/Budget.md", "new_title": "Budget"}
    ).json()

    assert (data["links_updated"], data["linkers"]) == (0, [])
    assert state.store.read("Hub.md") == before
    assert backlinks(client, "a/Plan.md") == ["Hub.md"]


def test_a_retitle_onto_a_shared_name_keeps_links_on_the_note_they_opened(state, client):
    add(client, "deep/x/Plan.md", "# Plan\n")
    add(client, "Draft.md", "# Draft\n")
    add(client, "Hub.md", "# Hub\n\n[[Plan]]\n")

    data = client.post(
        "/api/notes/Draft.md/rename", json={"new_path": "Plan.md", "new_title": "Plan"}
    ).json()

    # The root Plan.md would take over [[Plan]], so the link is qualified.
    assert (data["links_updated"], data["linkers"]) == (1, ["Hub.md"])
    assert body(state, "Hub.md").endswith("[[deep/x/Plan]]\n")
    assert backlinks(client, "deep/x/Plan.md") == ["Hub.md"]
    assert backlinks(client, "Plan.md") == []


# A template's placeholder is not YAML: the block does not load, so the store
# refuses to re-serialize it.
TEMPLATE = "---\ntitle: {{title}}\n---\n\nFrom [[Plan]].\n"


def test_a_linker_whose_frontmatter_does_not_load_never_fails_the_rename(state, client):
    add(client, "Plan.md", "# Plan\n")
    add(client, "A.md", "# A\n\n[[Plan]]\n")
    state.store.write_raw("B.md", TEMPLATE)
    asyncio.run(state.indexer.index_note("B.md", TEMPLATE))
    add(client, "C.md", "# C\n\n[[Plan]]\n")
    queue = state.live.subscribe()

    res = client.post("/api/notes/Plan.md/rename", json={"new_path": "Roadmap.md"})

    assert res.status_code == 200
    data = res.json()
    assert (data["links_updated"], data["linkers"]) == (3, ["A.md", "B.md", "C.md"])
    assert body(state, "A.md").endswith("[[Roadmap]]\n")
    assert body(state, "C.md").endswith("[[Roadmap]]\n")
    # Only the body changed, so the block is left exactly as the user wrote it.
    assert state.store.read("B.md") == TEMPLATE.replace("[[Plan]]", "[[Roadmap]]")
    assert set(backlinks(client, "Roadmap.md")) == {"A.md", "B.md", "C.md"}
    moves = [e for e in drain(queue) if e["type"] == "note_moved"]
    assert [(e["path"], e["new_path"]) for e in moves] == [("Plan.md", "Roadmap.md")]


def test_title_only_rename_reports_no_link_changes(state, client):
    add(client, "media/Dune.md", "# Dune\n")
    data = client.post(
        "/api/notes/media/Dune.md/rename",
        json={"new_path": "media/Dune.md", "new_title": "Dune (1965)"},
    ).json()
    assert (data["links_updated"], data["linkers"]) == (0, [])


# --- sidebar order --------------------------------------------------------------


def orders(client) -> tuple[list[str], list[str]]:
    return (
        client.get("/api/settings/note-order").json()["order"],
        client.get("/api/settings/folder-order").json()["order"],
    )


def test_note_rename_keeps_its_slot(state, client):
    for name in ("a.md", "b.md", "c.md"):
        add(client, name, "# note\n")
    # "d.md" names a note deleted since: stale, and must not outrank the move.
    client.put("/api/settings/note-order", json={"order": ["d.md", "b.md", "a.md", "c.md"]})

    client.post("/api/notes/a.md/rename", json={"new_path": "d.md"})

    assert orders(client)[0] == ["b.md", "d.md", "c.md"]


def test_folder_rename_keeps_the_folder_and_its_notes_in_place(state, client):
    add(client, "x/n.md", "# n\n")
    add(client, "x/sub/m.md", "# m\n")
    add(client, "y/o.md", "# o\n")
    client.put("/api/settings/note-order", json={"order": ["x/sub/m.md", "x/n.md", "y/o.md"]})
    client.put("/api/settings/folder-order", json={"order": ["y/", "x", "x/sub"]})

    client.post("/api/notes/folders/rename", json={"path": "x", "new_path": "z"})

    assert orders(client) == (["z/sub/m.md", "z/n.md", "y/o.md"], ["y", "z", "z/sub"])


def test_a_corrupt_config_never_fails_a_rename(state, client):
    add(client, "a.md", "# a\n")
    config = state.settings.vault_dir / CONFIG_PATH
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("{", encoding="utf-8")

    res = client.post("/api/notes/a.md/rename", json={"new_path": "b.md"})

    assert res.status_code == 200
    assert state.store.exists("b.md")


def test_omnia_moves_carry_the_order_into_their_commit(acting_state):
    state = acting_state
    for path in ("a.md", "b.md", "x/n.md"):
        asyncio.run(create_note(state, "t", {"path": path, "content": "# note"}))
    config = state.settings.vault_dir / CONFIG_PATH
    atomic_json(config, {"note_order": ["b.md", "a.md", "x/n.md"], "folder_order": ["x"]})
    asyncio.run(state.vault.commit_all("seed order"))

    rev = content_hash(state.store.read("a.md"))
    result = asyncio.run(
        move_note(state, "t", {"path": "a.md", "new_path": "c.md", "base_revision": rev})
    )
    assert CONFIG_PATH in result["_action"]["paths"]
    assert read_json(config, {})["note_order"] == ["b.md", "c.md", "x/n.md"]
    committed = state.vault.repo.git.show("--name-only", "--format=", "HEAD").split()
    assert CONFIG_PATH in committed

    asyncio.run(move_folder(state, "t", {"path": "x", "new_path": "w"}))
    saved = read_json(config, {})
    assert (saved["note_order"], saved["folder_order"]) == (["b.md", "c.md", "w/n.md"], ["w"])

    rev = content_hash(state.store.read("b.md"))
    result = asyncio.run(
        rename_note(state, "t", {"path": "b.md", "new_title": "Budget", "base_revision": rev})
    )
    assert CONFIG_PATH in result["_action"]["paths"]
    assert read_json(config, {})["note_order"] == ["Budget.md", "c.md", "w/n.md"]


def test_omnia_moves_rewrite_inbound_links_into_their_commit(acting_state):
    state = acting_state
    for path in ("Plan.md", "proj/Spec.md", "inbox/Idea.md", "x/Draft.md"):
        asyncio.run(create_note(state, "t", {"path": path, "content": "# note"}))
    hub = "# Hub\n\n[[Plan]] [[proj/Spec]] [[inbox/Idea]] [[x/Draft]]\n"
    asyncio.run(create_note(state, "t", {"path": "Hub.md", "content": hub}))

    def committed() -> list[str]:
        return state.vault.repo.git.show("--name-only", "--format=", "HEAD").split()

    rev = content_hash(state.store.read("Plan.md"))
    result = asyncio.run(
        move_note(state, "t", {"path": "Plan.md", "new_path": "Roadmap.md", "base_revision": rev})
    )
    assert "Hub.md" in result["_action"]["paths"] and "Hub.md" in committed()

    result = asyncio.run(move_folder(state, "t", {"path": "proj", "new_path": "projects"}))
    assert "Hub.md" in result["_action"]["paths"] and "Hub.md" in committed()

    result = asyncio.run(archive_note(state, "t", {"path": "inbox/Idea.md"}))
    assert "Hub.md" in result["_action"]["paths"] and "Hub.md" in committed()

    result = asyncio.run(move_notes(state, "t", {"paths": ["x/Draft.md"], "folder": "y"}))
    assert "Hub.md" in result["_action"]["paths"] and "Hub.md" in committed()

    assert body(state, "Hub.md").endswith(
        "[[Roadmap]] [[projects/Spec]] [[archive/Idea]] [[y/Draft]]\n"
    )


def test_omnia_move_without_a_saved_order_commits_no_config(acting_state):
    state = acting_state
    asyncio.run(create_note(state, "t", {"path": "a.md", "content": "# note"}))
    rev = content_hash(state.store.read("a.md"))
    result = asyncio.run(
        move_note(state, "t", {"path": "a.md", "new_path": "c.md", "base_revision": rev})
    )
    assert CONFIG_PATH not in result["_action"]["paths"]
    assert not (state.settings.vault_dir / CONFIG_PATH).exists()


# --- repair tool -----------------------------------------------------------------

PLAN = "# Plan\n\n## Goals\n\n" + "".join(f"- step {i}\n" for i in range(12))


def test_repair_tool_repoints_links_a_past_rename_broke(state, monkeypatch, capsys):
    store, vault = state.store, state.vault
    store.create("Plan.md", PLAN)
    store.create("work/Log.md", "# Log\n\n[[Plan]], [[plan#Goals|goals]] and `[[Plan]]`\n")
    # Two notes once named Draft went separate ways: [[Draft]] is ambiguous.
    store.create("a/Draft.md", PLAN.replace("Plan", "Draft A"))
    store.create("b/Draft.md", PLAN.replace("Plan", "Draft B"))
    store.create("Hub.md", "# Hub\n\n[[Draft]]\n")
    asyncio.run(vault.commit_all("notes"))
    # What a UI rename did before it rewrote links: move the file, nothing else.
    store.rename("Plan.md", "Roadmap.md")
    asyncio.run(vault.commit_all("rename"))
    store.rename("Roadmap.md", "work/Roadmap.md")
    store.rename("a/Draft.md", "a/Final.md")
    store.rename("b/Draft.md", "b/Other.md")
    asyncio.run(vault.commit_all("move"))
    log_before = store.read("work/Log.md")
    monkeypatch.setenv("OMNIA_DATA_DIR", str(state.settings.data_dir))

    assert repair_links.main([]) == 0
    out = capsys.readouterr().out
    assert "work/Log.md\n  [[Plan]] -> [[Roadmap]]\n  [[plan]] -> [[Roadmap]]\n" in out
    assert "ambiguous: [[Draft]] in Hub.md could be a/Final.md, b/Other.md" in out
    # The seeded Welcome note's dead [[Daily Notes]] has no rename behind it.
    assert "Daily Notes" not in out
    assert store.read("work/Log.md") == log_before

    head = vault.repo.head.commit.hexsha
    assert repair_links.main(["--apply"]) == 0
    assert body(state, "work/Log.md").endswith(
        "[[Roadmap]], [[Roadmap#Goals|goals]] and `[[Plan]]`\n"
    )
    assert "[[Draft]]" in store.read("Hub.md")
    commit = vault.repo.head.commit
    assert commit.parents[0].hexsha == head
    assert commit.message == repair_links.COMMIT_MESSAGE
    assert list(commit.stats.files) == ["work/Log.md"]

    capsys.readouterr()
    assert repair_links.main([]) == 0
    assert "0 link(s) to repoint in 0 note(s)" in capsys.readouterr().out


def test_repair_tool_refuses_a_directory_without_a_vault(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OMNIA_DATA_DIR", str(tmp_path))
    assert repair_links.main([]) == 2
    assert not (tmp_path / "vault").exists()
