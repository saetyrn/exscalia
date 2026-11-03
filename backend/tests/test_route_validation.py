"""Malformed input is a 4xx with a message, never a 500 or a stray file:
history version ids, control characters in paths, non-canonical daily dates,
and notes under a top-level folder named `folders`, which the old
folder-delete route used to shadow."""
import asyncio

import pytest

from app.api import meta as meta_api
from app.api import notes as notes_api
from app.api import recovery as recovery_api
from app.proofread import ProofreadStore
from app.vault.notes import NoteError

from test_task_boards_api import make_client


def notes_client(state):
    state.proofread = ProofreadStore(state.settings.data_dir)
    return make_client(state, notes_api.router)


def test_history_version_ids_are_validated(state):
    client = make_client(state, recovery_api.router)
    for sha in ("zzz", "--help", "HEAD~1", "abc"):
        response = client.get(
            "/api/history/content", params={"path": "Welcome.md", "sha": sha}
        )
        assert response.status_code == 400, sha
    diff = client.get("/api/history/diff", params={"path": "Welcome.md", "sha": "zzz"})
    assert diff.status_code == 400
    restore = client.post("/api/history/restore", json={"path": "Welcome.md", "sha": "zzz"})
    assert restore.status_code == 400

    # Well-formed but unknown: a clean miss, not gitdb's BadName escaping.
    assert state.vault.commit_content_sync("deadbeef", "Welcome.md") is None
    assert state.vault.commit_bytes_sync("deadbeef", "Welcome.md") is None
    missing = client.get(
        "/api/history/content", params={"path": "Welcome.md", "sha": "deadbeef"}
    )
    assert missing.status_code == 404

    # Real ids, full and abbreviated, still read.
    head = state.vault.repo.head.commit.hexsha
    for sha in (head, head[:7], head.upper()):
        found = client.get(
            "/api/history/content", params={"path": "Welcome.md", "sha": sha}
        )
        assert found.status_code == 200, sha


def test_control_characters_in_paths_are_rejected(state):
    for bad in ("a\x00.md", "a\x1f.md", "a\x7f.md", "dir\n/a.md"):
        with pytest.raises(NoteError) as err:
            state.store.resolve(bad)
        assert err.value.status == 400, repr(bad)
    for resolver, bad in (
        (state.store.resolve_folder, "a\x00b"),
        (state.store.resolve_folder, "tab\there"),
        (state.store.resolve_attachment, "attachments/a\x00.png"),
        (state.trash.resolve_paper, "paper/a\x00.paper"),
    ):
        with pytest.raises(NoteError) as err:
            resolver(bad)
        assert err.value.status == 400, repr(bad)

    client = notes_client(state)
    assert client.post("/api/notes", json={"path": "a\u0000.md"}).status_code == 400
    assert client.post("/api/notes", json={"path": "tab\there.md"}).status_code == 400
    assert client.get("/api/notes/a%00.md").status_code == 400
    assert client.post("/api/notes/folders", json={"path": "new\nfolder"}).status_code == 400
    names = [n.path for n in state.store.list_notes()] + state.store.list_folders()
    assert not [name for name in names if any(ord(c) < 32 for c in name)]


def test_a_control_character_name_already_on_disk_stays_reachable(state):
    """Nothing the app creates carries one, but a file that arrived with one
    (a synced vault, an older build) must not wedge the boot index sync or
    become impossible to rename away."""
    odd = "odd\tname.md"
    (state.settings.vault_dir / odd).write_text("# odd\n", encoding="utf-8")
    asyncio.run(state.indexer.sync_all())
    assert state.store.read(odd) == "# odd\n"

    client = notes_client(state)
    renamed = client.post("/api/notes/odd%09name.md/rename", json={"new_path": "Odd.md"})
    assert renamed.status_code == 200
    assert state.store.exists("Odd.md") and not state.store.exists(odd)


def test_daily_dates_are_canonicalised(state):
    client = make_client(state, meta_api.router)
    assert client.post("/api/daily/20260921").json() == {
        "path": "daily/2026-09-21.md",
        "created": True,
    }
    assert state.store.exists("daily/2026-09-21.md")
    assert not state.store.exists("daily/20260921.md")
    # The same day in its ISO week spelling finds that note.
    assert client.post("/api/daily/2026-W39-1").json() == {
        "path": "daily/2026-09-21.md",
        "created": False,
    }
    assert client.post("/api/daily/not-a-date").status_code == 400


def test_folder_delete_no_longer_shadows_notes_under_folders(state):
    client = notes_client(state)
    client.post("/api/notes", json={"path": "folders/x.md", "content": "# x\n"})

    # Once routed to delete_folder('x.md') and answered "Folder not found".
    deleted = client.delete("/api/notes/folders/x.md")
    assert deleted.status_code == 200
    assert not state.store.exists("folders/x.md")
    assert [(i["kind"], i["original_path"]) for i in state.trash.list()] == [
        ("note", "folders/x.md")
    ]

    client.post("/api/notes", json={"path": "a/Inner.md", "content": "# inner\n"})
    trashed = client.post("/api/notes/folders/delete", json={"path": "a"})
    assert trashed.json() == {"ok": True, "deleted": 1}
    assert not (state.settings.vault_dir / "a").exists()
    assert ("folder", "a") in [(i["kind"], i["original_path"]) for i in state.trash.list()]

    # The old spelling still deletes a real folder for clients that send it.
    client.post("/api/notes", json={"path": "b/Inner.md", "content": "# inner\n"})
    assert client.delete("/api/notes/folders/b").json() == {"ok": True, "deleted": 1}
    assert not (state.settings.vault_dir / "b").exists()

    assert client.post("/api/notes/folders/delete", json={"path": "daily"}).status_code == 400
    assert client.delete("/api/notes/folders/daily").status_code == 400
    assert client.post("/api/notes/folders/delete", json={"path": "missing"}).status_code == 404
