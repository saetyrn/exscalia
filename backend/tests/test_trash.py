"""The Trash's permanent-deletion paths and the restores that overwrite or
move a note.

purge_expired runs at boot, every six hours and on every GET /api/trash, and
purge/empty are rmtrees: a wrong expiry comparison or a loose id guard deletes
notes with nothing else in the suite noticing.
"""
import asyncio
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.api import recovery as recovery_api
from app.index import parse
from app.vault import fm
from app.vault.internal import TrashStore, atomic_json, parse_utc
from app.vault.notes import NoteError, NoteStore


@pytest.fixture
def trash(tmp_path: Path) -> TrashStore:
    return TrashStore(tmp_path, NoteStore(tmp_path))


def request_for(state):
    return SimpleNamespace(app=SimpleNamespace(state=state))


def trashed(trash: TrashStore, rel_path: str) -> dict:
    trash.store.create(rel_path, f"---\ntitle: {Path(rel_path).stem}\n---\n\nBody.\n")
    return trash.trash_note(rel_path)


def rewrite_manifest(trash: TrashStore, manifest: dict, **fields) -> None:
    """Hand-edit a manifest; a field given as ... is dropped."""
    value = {**manifest, **fields}
    value = {key: val for key, val in value.items() if val is not ...}
    atomic_json(trash.root / manifest["id"] / "manifest.json", value)


# -- purge_expired ----------------------------------------------------------


def test_purge_expired_keeps_an_item_until_its_thirty_days_are_up(trash):
    item = trashed(trash, "Plan.md")
    deleted = parse_utc(item["deleted_at"])

    assert trash.purge_expired(now=deleted + timedelta(days=29, hours=23)) == []
    assert (trash.root / item["id"] / "payload" / "Plan.md").is_file()

    removed = trash.purge_expired(now=deleted + timedelta(days=30, seconds=1))
    assert removed == [item]
    assert not (trash.root / item["id"]).exists()
    assert trash.list() == []


def test_purge_expired_removes_only_the_expired_items(trash):
    old = trashed(trash, "Old.md")
    fresh = trashed(trash, "Fresh.md")
    deleted = parse_utc(fresh["deleted_at"])
    rewrite_manifest(
        trash,
        old,
        deleted_at=(deleted - timedelta(days=10)).isoformat(),
        expires_at=(deleted + timedelta(days=20)).isoformat(),
    )

    removed = trash.purge_expired(now=deleted + timedelta(days=25))

    assert [item["id"] for item in removed] == [old["id"]]
    assert [item["id"] for item in trash.list()] == [fresh["id"]]


@pytest.mark.parametrize(
    "expires_at",
    [
        pytest.param(..., id="missing"),
        pytest.param("next month", id="words"),
        pytest.param("2020-13-45T00:00:00Z", id="impossible-date"),
        pytest.param(0, id="number"),
    ],
)
def test_purge_expired_skips_an_entry_without_a_usable_expiry(trash, expires_at):
    """A missing or unparseable expires_at is skipped, never read as "expired",
    and the loop still expires the entries after it."""
    kept = trashed(trash, "Kept.md")
    rewrite_manifest(trash, kept, expires_at=expires_at)
    expired = trashed(trash, "Expired.md")
    far_future = parse_utc(expired["deleted_at"]) + timedelta(days=3650)

    removed = trash.purge_expired(now=far_future)

    assert [item["id"] for item in removed] == [expired["id"]]
    assert (trash.root / kept["id"] / "payload" / "Kept.md").is_file()
    assert [item["id"] for item in trash.list()] == [kept["id"]]


# -- purge / empty ----------------------------------------------------------


def outside_the_trash(trash: TrashStore) -> list[Path]:
    """Files an rmtree of the Trash must never reach: notes beside it and the
    internal files that share .exscalia with it."""
    trash.store.create("Keep.md", "---\ntitle: Keep\n---\n\nStays.\n")
    trash.store.create("Folder/Inside.md", "---\ntitle: Inside\n---\n\nStays.\n")
    internal = trash.vault_dir / NoteStore.INTERNAL_DIR / "task-tombstones.json"
    atomic_json(internal, {})
    return [trash.vault_dir / "Keep.md", trash.vault_dir / "Folder/Inside.md", internal]


def test_purge_removes_one_entry_and_nothing_else(trash):
    gone = trashed(trash, "Gone.md")
    other = trashed(trash, "Other.md")
    siblings = outside_the_trash(trash)

    assert trash.purge(gone["id"]) == gone

    assert not (trash.root / gone["id"]).exists()
    assert (trash.root / other["id"] / "payload" / "Other.md").is_file()
    assert all(path.is_file() for path in siblings)
    with pytest.raises(NoteError) as exc:
        trash.purge(gone["id"])
    assert exc.value.status == 404


def test_empty_removes_the_trash_root_and_nothing_else(trash):
    trashed(trash, "One.md")
    trashed(trash, "Two.md")
    siblings = outside_the_trash(trash)

    assert trash.empty() == 2

    assert not trash.root.exists()
    assert all(path.is_file() for path in siblings)
    assert trash.empty() == 0
    assert trash.list() == []


@pytest.mark.parametrize(
    "item_id", ["..", "", ".", "abc/def", "ABCDEF", "deadbeef/../x", "../.."]
)
def test_an_item_id_that_is_not_lowercase_hex_never_reaches_an_rmtree(trash, item_id):
    """_entry's guard is all that stands between DELETE /api/trash/{item_id}
    and shutil.rmtree."""
    item = trashed(trash, "Trashed.md")
    siblings = outside_the_trash(trash)

    with pytest.raises(NoteError) as exc:
        trash.purge(item_id)
    assert exc.value.status == 400

    assert trash.root.is_dir()
    assert (trash.root / item["id"] / "manifest.json").is_file()
    assert all(path.is_file() for path in siblings)


# -- restore ----------------------------------------------------------------


def test_restore_under_a_new_name_takes_the_new_title(state):
    state.store.create("Old Name.md", "---\ntitle: Old Name\n---\n\nBody.\n")
    created = fm.parse(state.store.read("Old Name.md"))[0]["created"]
    item = state.trash.trash_note("Old Name.md")

    # The route the Trash page's 409 fallback calls; it indexes what is on disk.
    restored = asyncio.run(
        recovery_api.restore_trash(
            request_for(state),
            item["id"],
            recovery_api.RestoreTrashBody(target_path="New Name.md"),
        )
    )

    assert restored["notes"] == ["New Name.md"]
    meta, body = fm.parse(state.store.read("New Name.md"))
    assert meta["title"] == "New Name"
    assert meta["created"] == created
    assert body == "Body.\n"
    rows = asyncio.run(
        state.db.execute_fetchall(
            "SELECT title FROM notes WHERE path = ?", ("New Name.md",)
        )
    )
    assert [row["title"] for row in rows] == ["New Name"]
    assert state.trash.list() == []


def test_restore_to_the_original_path_leaves_the_note_byte_identical(trash):
    # Even a hand-edited title that does not match: a plain un-delete is not
    # an edit, so it is not the restore's to fix (nor to stamp modified).
    text = "---\ntitle: Hand edited\n---\n\nBody.\n"
    trash.store.write_raw("Same.md", text)
    item = trash.trash_note("Same.md")

    assert trash.restore(item["id"])["notes"] == ["Same.md"]
    assert (trash.vault_dir / "Same.md").read_text("utf-8") == text


def test_restore_into_another_folder_under_the_same_name_is_not_rewritten(trash):
    text = "---\ntitle: Moved\n---\n\nBody.\n"
    trash.store.write_raw("Moved.md", text)
    item = trash.trash_note("Moved.md")

    assert trash.restore(item["id"], "Archive/Moved.md")["notes"] == ["Archive/Moved.md"]
    assert (trash.vault_dir / "Archive/Moved.md").read_text("utf-8") == text


def test_restore_of_a_title_exempt_note_keeps_its_title(trash):
    text = "---\ntitle: 'Elon Musk: A Biography'\n---\n\nBody.\n"
    trash.store.write_raw("media/Elon Musk A Biography.md", text)
    item = trash.trash_note("media/Elon Musk A Biography.md")

    trash.restore(item["id"], "media/Elon Musk A Biography (restored).md")

    restored = trash.vault_dir / "media/Elon Musk A Biography (restored).md"
    assert restored.read_text("utf-8") == text


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(b"---\ntitle: [unclosed\n---\n\nBody.\n", id="bad-yaml"),
        pytest.param(
            "---\ntitle: Caf\u00e9\n---\n\nBody.\n".encode("latin-1"), id="latin-1"
        ),
    ],
)
def test_restore_under_a_new_name_stands_when_the_title_cannot_be_rewritten(trash, raw):
    (trash.vault_dir / "Broken.md").write_bytes(raw)
    item = trash.trash_note("Broken.md")

    restored = trash.restore(item["id"], "Renamed.md")

    assert restored["notes"] == ["Renamed.md"]
    assert (trash.vault_dir / "Renamed.md").read_bytes() == raw
    assert not (trash.root / item["id"]).exists()


# -- history restore --------------------------------------------------------


def test_history_replace_snapshots_first_and_keeps_task_ids(state):
    # The old version's task predates task metadata, so only the live note
    # knows its id; replace must carry it over through previous_content.
    state.store.create("Plan.md", "---\ntitle: Plan\n---\n\none\n\n- [ ] Ship it\n")
    asyncio.run(state.vault.commit_all("Add plan"))
    old_sha = state.vault.repo.head.commit.hexsha
    current, _added = parse.normalize_task_metadata(
        state.store.read("Plan.md").replace("one", "two")
    )
    state.store.write("Plan.md", current)
    asyncio.run(state.indexer.index_note("Plan.md", state.store.read("Plan.md")))
    [task_id] = parse.task_ids(state.store.read("Plan.md"))

    restored = asyncio.run(
        recovery_api.restore_history(
            request_for(state),
            recovery_api.RestoreHistoryBody(path="Plan.md", sha=old_sha, mode="replace"),
        )
    )

    live = state.store.read("Plan.md")
    assert restored["path"] == "Plan.md"
    assert restored["content"] == live
    assert "one" in live and "two" not in live
    assert parse.task_ids(live) == {task_id}
    rows = asyncio.run(
        state.db.execute_fetchall("SELECT id FROM tasks WHERE path = ?", ("Plan.md",))
    )
    assert [row["id"] for row in rows] == [task_id]

    head = state.vault.repo.head.commit
    snapshot = head.parents[0]
    assert head.message == f"Restore Plan.md from {old_sha[:8]}"
    assert snapshot.message == "Snapshot before restoring Plan.md"
    assert snapshot.parents[0].hexsha == old_sha
    # The snapshot holds the unsaved text the restore replaced.
    assert b"two" in (snapshot.tree / "Plan.md").data_stream.read()
