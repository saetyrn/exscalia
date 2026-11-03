"""Note and folder names: one canonical folder spelling, a narrow check on new
note names, and no two siblings that differ only by case or Unicode form.

The dev Mac's APFS is case- and normalization-insensitive while prod's ext4 is
neither, so the collision tests assert the 409's detail, which names the
existing sibling: on APFS a plain exists() check would also 409, but naming
the requested spelling instead.
"""
import asyncio
import unicodedata
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.api import notes as notes_api
from app.overseer.tool_expansion import (
    _unique_path,
    create_note,
    delete_folder,
    duplicate_note,
    move_folder,
    rename_note,
)
from app.index.db import content_hash
from app.vault import names
from app.vault.internal import TrashStore
from app.vault.notes import NoteError, NoteStore

NFC = unicodedata.normalize("NFC", "Café")
NFD = unicodedata.normalize("NFD", "Café")


def request_for(state):
    return SimpleNamespace(app=SimpleNamespace(state=state))


def indexed_paths(state, folder: str) -> list[str]:
    rows = asyncio.run(
        state.db.execute_fetchall(
            "SELECT path FROM notes WHERE path LIKE ? ORDER BY path", (f"{folder}/%",)
        )
    )
    return [row["path"] for row in rows]


def status_of(call) -> tuple[int, str]:
    with pytest.raises(NoteError) as exc:
        call()
    return exc.value.status, exc.value.detail


# -- folder paths ------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [("Work", "Work"), ("Work/", "Work"), ("./Work", "Work"), ("Work//Sub/", "Work/Sub")],
)
def test_folder_paths_have_one_spelling(tmp_path, raw, canonical):
    assert NoteStore(tmp_path).normalize_folder(raw) == canonical


@pytest.mark.parametrize("bad", ["", ".", "./", "/", "/Work", "../Work", "a/../b", ".git", "a/.exscalia"])
def test_folder_paths_refuse_the_vault_root_and_escapes(tmp_path, bad):
    store = NoteStore(tmp_path)
    assert status_of(lambda: store.resolve_folder(bad))[0] == 400


def test_moving_a_folder_given_with_a_trailing_slash_maps_every_note(tmp_path):
    store = NoteStore(tmp_path)
    store.create("Work/note.md")
    store.create("Work/deep/other.md")

    moved = store.rename_folder("Work/", "Archive/")

    # Slicing the raw 'Work/' used to drop a character: 'Archive/ote.md'.
    assert sorted(moved) == [
        ("Work/deep/other.md", "Archive/deep/other.md"),
        ("Work/note.md", "Archive/note.md"),
    ]
    assert [n.path for n in store.list_notes()] == ["Archive/deep/other.md", "Archive/note.md"]


def test_the_folder_move_route_leaves_the_index_on_the_new_paths(state):
    state.store.create("Work/note.md", "---\ntitle: note\n---\n\n- [ ] Ship it\n")
    asyncio.run(state.indexer.sync_all())

    result = asyncio.run(
        notes_api.rename_folder(
            request_for(state), notes_api.RenameFolder(path="Work/", new_path="Archive")
        )
    )

    assert result == {"path": "Archive", "moved": 1, "links_updated": 0, "linkers": []}
    assert indexed_paths(state, "Work") == []
    assert indexed_paths(state, "Archive") == ["Archive/note.md"]
    tasks = asyncio.run(
        state.db.execute_fetchall("SELECT path FROM tasks WHERE text = 'Ship it'")
    )
    assert [row["path"] for row in tasks] == ["Archive/note.md"]


@pytest.mark.parametrize("spelling", ["daily", "daily/", "Daily/", "./templates", "MEDIA"])
def test_built_in_folders_cannot_be_moved_or_trashed_by_any_spelling(state, spelling):
    state.store.create("daily/2026-09-23.md")
    if not state.store.exists("templates/daily.md"):
        state.store.create("templates/daily.md")
    state.store.create("media/Dune.md")
    request = request_for(state)

    renamed = status_of(
        lambda: asyncio.run(
            notes_api.rename_folder(
                request, notes_api.RenameFolder(path=spelling, new_path="Elsewhere")
            )
        )
    )
    deleted = status_of(
            lambda: asyncio.run(
                notes_api.delete_folder(request, notes_api.DeleteFolder(path=spelling))
            )
        )

    assert renamed[0] == deleted[0] == 400
    assert "built-in folder" in renamed[1] and "built-in folder" in deleted[1]
    for path in ("daily/2026-09-23.md", "templates/daily.md", "media/Dune.md"):
        assert state.store.exists(path)
    assert not (state.settings.vault_dir / "Elsewhere").exists()


def test_only_the_built_in_root_itself_is_protected(tmp_path):
    store = NoteStore(tmp_path)
    store.create("daily/archive/old.md")
    assert store.rename_folder("daily/archive", "Old dailies") == [
        ("daily/archive/old.md", "Old dailies/old.md")
    ]


@pytest.mark.parametrize("spelling", ["daily/", "Daily/"])
def test_omnia_folder_tools_refuse_built_in_folders_by_any_spelling(acting_state, spelling):
    acting_state.store.create("daily/2026-09-23.md")
    for tool, inp in (
        (move_folder, {"path": spelling, "new_path": "Elsewhere"}),
        (delete_folder, {"path": spelling}),
    ):
        status, detail = status_of(
            lambda tool=tool, inp=inp: asyncio.run(tool(acting_state, "t", inp))
        )
        assert status == 400 and "built-in folder" in detail
    assert acting_state.store.exists("daily/2026-09-23.md")


def test_omnia_move_folder_with_a_trailing_slash_keeps_the_index_right(acting_state):
    asyncio.run(
        create_note(acting_state, "t", {"path": "Work/note.md", "content": "# note\n"})
    )

    result = asyncio.run(
        move_folder(acting_state, "t", {"path": "Work/", "new_path": "Archive/"})
    )

    assert (result["old_path"], result["path"]) == ("Work", "Archive")
    assert sorted(result["_action"]["paths"]) == ["Archive", "Work"]
    assert indexed_paths(acting_state, "Work") == []
    assert indexed_paths(acting_state, "Archive") == ["Archive/note.md"]


def test_trash_refuses_a_built_in_folder_given_with_a_slash(tmp_path):
    store = NoteStore(tmp_path)
    store.create("templates/daily.md")
    trash = TrashStore(tmp_path, store)
    assert status_of(lambda: trash.trash_folder("templates/"))[0] == 400
    assert store.exists("templates/daily.md")


def test_trashing_a_folder_records_its_canonical_path(tmp_path):
    store = NoteStore(tmp_path)
    store.create("Work/note.md")
    trash = TrashStore(tmp_path, store)

    manifest, notes = trash.trash_folder("./Work/")

    assert manifest["original_path"] == "Work"
    assert notes == ["Work/note.md"]
    assert trash.restore(manifest["id"])["path"] == "Work"


def test_the_unused_rmtree_folder_delete_is_gone():
    # Every folder delete goes through the Trash; an rmtree beside it was one
    # '.' away from deleting the vault.
    assert not hasattr(NoteStore, "delete_folder")


# -- new note names ----------------------------------------------------------


@pytest.mark.parametrize(
    "stem",
    ["C# tips", "Issue #42", "a|b", "[draft]", "x]", "tab\there", "bell\x07", "c1\x85", " lead", "trail ", ""],
)
def test_new_note_names_that_break_links_or_listings_are_refused(tmp_path, stem):
    store = NoteStore(tmp_path)
    store.create("Plain.md")

    assert status_of(lambda: store.create(f"{stem}.md"))[0] == 400
    assert status_of(lambda: store.rename("Plain.md", f"{stem}.md"))[0] == 400
    assert [n.path for n in store.list_notes()] == ["Plain.md"]


@pytest.mark.parametrize("stem", ["Why?", "Q3: plan", 'Say "hi"', "a*b", "<tag>", "v1.", NFD, "日本語のメモ"])
def test_ordinary_punctuation_stays_allowed(tmp_path, stem):
    # The filename is the title: Windows-only restrictions would ban common
    # titles for a checkout that never happens.
    NoteStore(tmp_path).create(f"{stem}.md")


def test_a_name_past_the_byte_limit_is_a_400_not_a_500(tmp_path):
    store = NoteStore(tmp_path)
    stem = "漢" * 77  # 231 bytes: under ext4's 255, over the 200-byte cap
    store.create("Short.md")

    status, detail = status_of(lambda: store.create(f"{stem}.md"))
    assert (status, detail) == (400, "Note name is too long (231 bytes; the limit is 200)")
    assert status_of(lambda: store.rename("Short.md", f"{stem}.md"))[0] == 400

    # Past the filesystem's own limit, the OSError is a 400 too.
    status, detail = status_of(lambda: store.create(f"{'a' * 300}.md", check_name=False))
    assert status == 400 and detail.startswith("Path is too long")


def test_an_existing_odd_name_stays_movable_and_copyable(tmp_path):
    # Names that predate the rules: reads, moves and copies named after them
    # must keep working; only a new name is checked.
    (tmp_path / "C# tips.md").write_text("---\ntitle: C# tips\n---\n\nBody.\n", encoding="utf-8")
    store = NoteStore(tmp_path)

    assert "Body." in store.read("C# tips.md")
    store.rename("C# tips.md", "Archive/C# tips.md")
    store.create("Archive/C# tips (conflict).md", "Copy.\n", check_name=False)
    assert [n.path for n in store.list_notes()] == [
        "Archive/C# tips (conflict).md",
        "Archive/C# tips.md",
    ]


# -- sibling identity ----------------------------------------------------------


def test_a_case_variant_of_an_existing_note_is_refused(tmp_path):
    store = NoteStore(tmp_path)
    store.create("Ideas.md")

    status, detail = status_of(lambda: store.create("ideas.md"))

    assert (status, detail) == (409, "Note already exists: Ideas.md")
    assert [n.path for n in store.list_notes()] == ["Ideas.md"]


def test_an_nfd_twin_of_an_nfc_note_is_refused(tmp_path):
    store = NoteStore(tmp_path)
    store.create(f"{NFC}.md")

    status, detail = status_of(lambda: store.create(f"{NFD}.md"))

    assert (status, detail) == (409, f"Note already exists: {NFC}.md")
    assert [n.path for n in store.list_notes()] == [f"{NFC}.md"]


def test_renaming_onto_a_case_variant_of_another_note_is_refused(tmp_path):
    store = NoteStore(tmp_path)
    store.create("Ideas.md")
    store.create("Other.md")

    status, detail = status_of(lambda: store.rename("Other.md", "ideas.md"))

    assert (status, detail) == (409, "Note already exists: Ideas.md")
    assert sorted(n.path for n in store.list_notes()) == ["Ideas.md", "Other.md"]


def test_a_case_only_rename_of_the_same_note_is_allowed(tmp_path):
    store = NoteStore(tmp_path)
    store.create("Ideas.md", "---\ntitle: Ideas\n---\n\nKeep.\n")

    store.rename("Ideas.md", "ideas.md")

    assert [n.path for n in store.list_notes()] == ["ideas.md"]
    assert "Keep." in store.read("ideas.md")
    # Renaming a note onto its own exact path is still a conflict.
    assert status_of(lambda: store.rename("ideas.md", "ideas.md"))[0] == 409


def test_omnia_retitling_by_case_only_keeps_the_name(acting_state):
    asyncio.run(
        create_note(acting_state, "t", {"path": "Ideas.md", "content": "# Ideas\n"})
    )

    result = asyncio.run(
        rename_note(
            acting_state,
            "t",
            {
                "path": "Ideas.md",
                "new_title": "ideas",
                "base_revision": content_hash(acting_state.store.read("Ideas.md")),
            },
        )
    )

    assert result["path"] == "ideas.md"
    paths = [n.path for n in acting_state.store.list_notes()]
    assert "ideas.md" in paths and "Ideas.md" not in paths and "ideas 2.md" not in paths


def test_omnia_duplicating_an_odd_legacy_name_keeps_working(acting_state):
    # The default "… copy" name is the source note's own name, so a note that
    # predates the rules must still be duplicable; a chosen target is checked.
    acting_state.store.create("C# tips.md", "# C# tips\n", check_name=False)

    result = asyncio.run(duplicate_note(acting_state, "t", {"path": "C# tips.md"}))

    assert result["path"] == "C# tips copy.md"
    assert "C# tips copy.md" in [n.path for n in acting_state.store.list_notes()]
    status, detail = status_of(
        lambda: asyncio.run(
            duplicate_note(
                acting_state, "t", {"path": "C# tips.md", "target_path": "C# more.md"}
            )
        )
    )
    assert status == 400 and "[ ] # |" in detail


def test_media_titles_with_hash_or_brackets_still_log(acting_state):
    # A media item's path is slugged from its title and found again by the
    # same slug; it is opened from the Media page, never linked by filename.
    from app.api import media as media_api
    from app.overseer.tool_expansion import bulk_log_media, log_media
    from tests.test_media_api import make_client

    acting_state.timezone = "UTC"
    client = make_client(acting_state, media_api.router)
    resp = client.post("/api/media", json={"title": "C# in Depth", "media_type": "book"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["path"] == "media/C# in Depth.md"

    logged = asyncio.run(
        log_media(acting_state, "t", {"title": "Persona 5 [Royal]", "media_type": "game"})
    )
    assert logged["path"] == "media/Persona 5 [Royal].md"

    bulk = asyncio.run(
        bulk_log_media(
            acting_state,
            "t",
            {"items": [{"title": "Detective Agency #1", "media_type": "book"}]},
        )
    )
    assert [item["path"] for item in bulk["items"]] == ["media/Detective Agency #1.md"]
    paths = [n.path for n in acting_state.store.list_notes()]
    assert {
        "media/C# in Depth.md",
        "media/Persona 5 [Royal].md",
        "media/Detective Agency #1.md",
    } <= set(paths)


def test_unique_path_numbers_past_a_case_or_form_twin(state):
    state.store.create(f"{NFD.lower()}.md")
    state.store.create("Plan.md")

    assert _unique_path(state, f"{NFC}.md") == f"{NFC} 2.md"
    assert _unique_path(state, "plan.md") == "plan 2.md"
    assert _unique_path(state, "Fresh.md") == "Fresh.md"


# -- conflict copies -----------------------------------------------------------


def test_a_long_name_can_still_be_conflict_copied(state):
    stem = "長" * 63 + "a"  # 190 bytes: allowed, and too long for a naive suffix
    path = f"{stem}.md"
    content = state.store.create(path, "---\ntitle: x\n---\n\nServer.\n")
    asyncio.run(state.indexer.index_note(path, content))

    result = asyncio.run(
        notes_api.save_note(
            request_for(state),
            path,
            notes_api.SaveNote(content="Mine.\n", base_revision="stale"),
        )
    )

    assert result["status"] == "conflict_copy"
    copy = Path(result["conflict_path"])
    assert len(copy.name.encode("utf-8")) <= 255
    assert copy.name.startswith("長") and " (conflict " in copy.name
    assert "Mine." in state.store.read(result["conflict_path"])


def test_an_odd_legacy_name_can_still_be_conflict_copied(state):
    (state.settings.vault_dir / "C# tips.md").write_text(
        "---\ntitle: C# tips\n---\n\nServer.\n", encoding="utf-8"
    )

    result = asyncio.run(
        notes_api.save_note(
            request_for(state),
            "C# tips.md",
            notes_api.SaveNote(content="Mine.\n", base_revision="stale"),
        )
    )

    assert result["conflict_path"].startswith("C# tips (conflict ")


def test_truncation_keeps_whole_characters():
    assert names.truncate_stem_bytes("漢字", 5) == "漢"
    assert names.truncate_stem_bytes("short", 100) == "short"
    assert names.identity_key(NFD) == names.identity_key(NFC.upper())
