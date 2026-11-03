"""Paper items restored through the generic Trash — the web Trash page and
OMNIA's restore_trash — come back with the Paper bookkeeping, exactly as
through /api/paper/trash: re-listed in their notebook, tombstone cleared,
handwriting index updated, and a `paper` event on the LiveBus."""
import asyncio

import pytest

from app.api import recovery as recovery_api
from app.overseer.tool_expansion import restore_trash as restore_trash_tool
from app.vault.notes import NoteError

from test_task_boards_api import make_client

PAGE = "a" * 32


def drain(queue):
    events = []
    while True:
        try:
            events.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            return events


def rows(state, sql, *args):
    return asyncio.run(state.db.execute_fetchall(sql, args))


def deleted_page(state):
    nb = state.paper.create_notebook(title="Ideas")["path"]
    state.paper.save_page(nb, PAGE, base_revision=0, page={}, drawing=b"ink", text=None)
    item = state.paper.delete_page(nb, PAGE)
    return nb, item


def tombstoned(state):
    return {row["path"] for row in state.paper.tombstones()}


def test_trash_route_restores_a_page_with_its_bookkeeping(state):
    nb, item = deleted_page(state)
    page_dir = f"{nb}/pages/{PAGE}"
    assert PAGE not in state.paper.read_notebook(nb)["pages"]
    assert page_dir in tombstoned(state)

    client = make_client(state, recovery_api.router)
    queue = state.live.subscribe()
    restored = client.post(f"/api/trash/{item['id']}/restore", json={})
    assert restored.status_code == 200
    assert restored.json()["path"] == page_dir

    assert PAGE in state.paper.read_notebook(nb)["pages"]
    assert page_dir not in tombstoned(state)
    assert [(e["type"], e["path"]) for e in drain(queue)] == [("paper", page_dir)]
    assert rows(state, "SELECT path FROM paper_pages WHERE path = ?", page_dir)
    assert client.get("/api/trash").json()["items"] == []


def test_trash_route_restores_a_notebook_and_clears_its_tombstone(state):
    nb = state.paper.create_notebook(title="Sketches")["path"]
    item = state.paper.delete_notebook(nb)
    assert nb in tombstoned(state)

    client = make_client(state, recovery_api.router)
    queue = state.live.subscribe()
    assert client.post(f"/api/trash/{item['id']}/restore").status_code == 200

    assert state.paper.read_notebook(nb)["title"] == "Sketches"
    assert nb not in tombstoned(state)
    assert [(e["type"], e["path"]) for e in drain(queue)] == [("paper", nb)]
    assert rows(state, "SELECT path FROM paper_notebooks WHERE path = ?", nb)


def test_paper_items_only_restore_to_where_they_were(state):
    nb = state.paper.create_notebook(title="Ideas")["path"]
    item = state.paper.delete_notebook(nb)
    client = make_client(state, recovery_api.router)

    for target in ("paper/Elsewhere.paper", f"{nb} (restored)"):
        refused = client.post(
            f"/api/trash/{item['id']}/restore", json={"target_path": target}
        )
        assert refused.status_code == 400, target
    assert not (state.settings.vault_dir / f"{nb} (restored)").exists()
    assert nb in tombstoned(state)

    # Naming the original location is the same as naming none.
    same = client.post(f"/api/trash/{item['id']}/restore", json={"target_path": nb})
    assert same.status_code == 200
    assert nb not in tombstoned(state)


def test_notes_still_restore_through_the_generic_route(state):
    state.trash.trash_note("Welcome.md")
    (item,) = state.trash.list()
    client = make_client(state, recovery_api.router)
    queue = state.live.subscribe()
    moved = client.post(
        f"/api/trash/{item['id']}/restore", json={"target_path": "Back.md"}
    )
    assert moved.status_code == 200
    assert state.store.exists("Back.md")
    assert [(e["type"], e["path"]) for e in drain(queue)] == [("note", "Back.md")]


def test_omnia_restore_trash_relists_a_page(acting_state):
    state = acting_state
    nb, item = deleted_page(state)
    page_dir = f"{nb}/pages/{PAGE}"
    # The autocommitter has swept the delete by the time anyone asks OMNIA.
    asyncio.run(state.vault.commit_all("Delete a page"))
    queue = state.live.subscribe()

    result = asyncio.run(restore_trash_tool(state, "t", {"item_id": item["id"]}))
    assert result["path"] == page_dir
    assert PAGE in state.paper.read_notebook(nb)["pages"]
    assert page_dir not in tombstoned(state)
    assert ("paper", page_dir) in [(e["type"], e["path"]) for e in drain(queue)]
    assert rows(state, "SELECT path FROM paper_pages WHERE path = ?", page_dir)
    # One undoable commit carries the files and the re-listing together.
    committed = state.vault.repo.head.commit.stats.files
    assert f"{nb}/notebook.json" in committed
    assert any(path.startswith(page_dir) for path in committed)
    assert result["_action"]["undoable"] is True

    again = state.paper.delete_page(nb, PAGE)
    with pytest.raises(NoteError) as err:
        asyncio.run(
            restore_trash_tool(
                state, "t", {"item_id": again["id"], "target_path": "paper/Other.paper"}
            )
        )
    assert err.value.status == 400
    assert PAGE not in state.paper.read_notebook(nb)["pages"]
