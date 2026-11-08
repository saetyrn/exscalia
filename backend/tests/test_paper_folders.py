"""Paper folders: folder.json records, their routes, and the change feed.

A folder is a directory under ``paper/`` with its record inside it. These
tests pin what the iPad and the web viewer build on: the exact JSON of each
route, the identity of a folder that predates its record, the uniqueness rule
that keeps a vault written on Linux checkout-safe on case-insensitive macOS,
and a rename that other devices apply as one directory rename because nothing
inside it was restamped.
"""

import asyncio
import json
from pathlib import Path

import pytest

from app import paper_folders
from app.api import paper as paper_api
from app.paper_folders import (
    FOLDER_COLORS,
    folder_slug,
    legacy_folder_id,
)
from app.paper_store import TOMBSTONE_DAYS
from app.vault import internal
from app.vault.notes import NoteError
from test_paper_api import PNG, make_client, new_notebook, put_page

RECORD_KEYS = {
    "id", "name", "color", "icon", "favorite", "created", "modified", "seq",
    "path", "parent",
}
TRASH_KEYS = {
    "id", "kind", "original_path", "deleted_at", "expires_at", "note_count",
    "folder_id", "notebook_count",
}
FOLDER_ID = "f0" * 16
OTHER_ID = "0f" * 16
PAGE = "a" * 32


def new_folder(client, name, parent="paper", **body) -> dict:
    response = client.post("/api/paper/folders", json={"parent": parent, "name": name, **body})
    assert response.status_code == 200, response.text
    return response.json()


def rename(client, path, new_path, **body):
    return client.post(
        f"/api/paper/folders/{path}/rename", json={"new_path": new_path, **body}
    )


def stored(state, path) -> dict:
    return json.loads((state.paper.dir / path / "folder.json").read_text())


def seq_now(state) -> int:
    return state.paper.seq_state()["seq"]


def feed(client, since: int) -> dict:
    epoch = client.get("/api/paper/changes").json()["epoch"]
    response = client.get(f"/api/paper/changes?since={since}&epoch={epoch}")
    assert response.status_code == 200, response.text
    return response.json()


# -- names and ids -------------------------------------------------------------


def test_folder_slugs_match_the_shared_vectors():
    """The iPad's folderSlug tests against the same file."""
    path = Path(__file__).parent / "fixtures" / "folder_slug_vectors.json"
    vectors = json.loads(path.read_text("utf-8"))
    assert len(vectors) >= 10
    for vector in vectors:
        assert folder_slug(vector["name"]) == vector["slug"], vector
    slugs = {vector["slug"] for vector in vectors}
    names = {vector["name"] for vector in vectors}
    # What the contract asks the file to hold.
    assert {"Notes.paper_", "folder.json_", "_CON", "Work Accounting"} <= slugs
    assert any(ord(char) > 0xFFFF for name in names for char in name)
    assert any(name.lower().endswith(".paper") for name in names)


def test_legacy_ids_are_the_contracts_vectors():
    assert legacy_folder_id("paper/Work") == "9e7201dacb87ef0d43fb22798ccf4af6"
    assert legacy_folder_id("paper/Work/Accounting") == "3dfc9f48645d394cc5a07b793b247654"
    # NFC first: the same directory spelled decomposed has the same id.
    assert legacy_folder_id("paper/Résumé") == legacy_folder_id("paper/Résumé")


# -- create --------------------------------------------------------------------


def test_create_answers_the_record_and_writes_folder_json(state):
    client = make_client(state)
    record = new_folder(
        client, "Accounting", id=FOLDER_ID, color="red", icon="emoji:\U0001F4D2", favorite=True
    )
    assert set(record) == RECORD_KEYS | {"existing"}
    assert record["existing"] is False
    assert record["id"] == FOLDER_ID
    assert record["path"] == "paper/Accounting" and record["parent"] == "paper"
    assert (record["name"], record["color"], record["icon"], record["favorite"]) == (
        "Accounting", "red", "emoji:\U0001F4D2", True,
    )
    assert record["created"] == record["modified"]
    assert record["seq"] == seq_now(state)
    on_disk = stored(state, "paper/Accounting")
    assert set(on_disk) == RECORD_KEYS - {"path", "parent"}
    assert on_disk["id"] == FOLDER_ID and on_disk["seq"] == record["seq"]


def test_create_defaults_and_trims(state):
    client = make_client(state)
    record = new_folder(client, "  Plans  ")
    assert record["name"] == "Plans" and record["path"] == "paper/Plans"
    assert record["color"] == "blue" and record["icon"] is None
    assert record["favorite"] is False
    assert len(record["id"]) == 32
    assert new_folder(client, "")["name"] == "Untitled"


def test_create_slugs_the_directory_but_keeps_the_name_verbatim(state):
    client = make_client(state)
    record = new_folder(client, "Notes.paper")
    assert record["path"] == "paper/Notes.paper_"
    assert record["name"] == "Notes.paper"
    assert state.paper.notebook_dirs() == []
    assert new_folder(client, "Q3: Plans?")["path"] == "paper/Q3 Plans"


@pytest.mark.parametrize(
    "body",
    [
        {"name": "x" * 201},
        {"name": "Ok", "color": "chartreuse"},
        {"name": "Ok", "icon": "symbol:Folder"},
        {"name": "Ok", "icon": "symbol:" + "a" * 65},
        {"name": "Ok", "icon": "emoji:A"},
        {"name": "Ok", "icon": "emoji:\U0001F4D2 \U0001F4D2"},
        {"name": "Ok", "icon": "emoji:" + "\U0001F4D2" * 17},
        {"name": "Ok", "icon": "glyph:star"},
        {"name": "Ok", "id": "NOT-HEX"},
        {"name": "Ok", "parent": "paper/Work/Ideas.paper"},
        {"name": "Ok", "parent": "paper/../etc"},
    ],
)
def test_create_refuses_bad_values(state, body):
    response = make_client(state).post("/api/paper/folders", json=body)
    assert response.status_code == 400, response.text
    assert not [p for p in state.paper.folders() if p.endswith("Ok")]


def test_every_colour_and_a_symbol_icon_are_accepted(state):
    client = make_client(state)
    for color in FOLDER_COLORS:
        assert new_folder(client, color, color=color)["color"] == color
    assert new_folder(client, "Sym", icon="symbol:chart.bar")["icon"] == "symbol:chart.bar"


def test_create_is_idempotent_by_id(state):
    client = make_client(state)
    first = new_folder(client, "Work", id=FOLDER_ID)
    again = new_folder(client, "Work", id=FOLDER_ID)
    assert again == first
    assert state.paper.folders() == ["paper/Work"]
    # A retry after another device moved it finds it where it is now.
    assert rename(client, "paper/Work", "paper/Jobs", name="Jobs").status_code == 200
    replay = new_folder(client, "Work", id=FOLDER_ID)
    assert replay["path"] == "paper/Jobs" and replay["existing"] is False
    assert state.paper.folders() == ["paper/Jobs"]


def test_two_devices_creating_one_name_end_with_one_folder(state):
    client = make_client(state)
    first = new_folder(client, "Work", id=FOLDER_ID)
    second = new_folder(client, "Work", id=OTHER_ID)
    assert second["existing"] is True
    assert second["id"] == FOLDER_ID and second["path"] == "paper/Work"
    # Casefolded, so Linux cannot grow a twin that macOS would refuse.
    third = new_folder(client, "WORK", id="ab" * 16)
    assert third["existing"] is True and third["id"] == FOLDER_ID
    assert state.paper.folders() == ["paper/Work"]
    assert stored(state, "paper/Work")["seq"] == first["seq"]


def test_a_notebook_holding_the_name_suffixes_the_folder(state):
    client = make_client(state)
    new_notebook(client, "Ideas")
    record = new_folder(client, "ideas")
    assert record["path"] == "paper/ideas 2" and record["name"] == "ideas"
    assert record["existing"] is False


def test_a_folder_holding_the_name_suffixes_a_new_notebook(state):
    client = make_client(state)
    new_folder(client, "Ideas")
    assert new_notebook(client, "IDEAS")["path"] == "paper/IDEAS 2.paper"
    # The parent is matched as the disk spells it, so no twin folder appears.
    new_folder(client, "Work")
    assert new_notebook(client, "Plans", folder="work")["path"] == "paper/Work/Plans.paper"
    assert state.paper.folders() == ["paper/Ideas", "paper/Work"]


def test_a_notebook_rename_onto_a_folders_name_is_refused(state):
    client = make_client(state)
    new_folder(client, "Ideas")
    nb = new_notebook(client, "Plans")
    response = client.post(
        f"/api/paper/notebooks/{nb['path']}/rename", json={"new_path": "paper/ideas.paper"}
    )
    assert response.status_code == 409
    assert state.paper.notebook_dirs() == ["paper/Plans.paper"]


def test_a_notebook_moved_beside_a_folder_or_notebook_of_its_name_is_refused(state):
    """The server's half of the iPad's naming rule (``uniquePackagePath``):
    a move into a folder holding a folder of the notebook's name, or a rename
    onto a sibling notebook's name in another case, is a 409. The iPad picks
    "<name> 2" itself, and follows a 409 it could not foresee to the first
    name free here, rather than retrying the refused one."""
    client = make_client(state)
    work = new_folder(client, "Work")
    new_folder(client, "Physics", parent=work["path"])
    physics = new_notebook(client, "Physics")
    response = client.post(
        f"/api/paper/notebooks/{physics['path']}/rename",
        json={"new_path": f"{work['path']}/Physics.paper"},
    )
    assert response.status_code == 409, response.text
    new_notebook(client, "plans")
    ideas = new_notebook(client, "Ideas")
    response = client.post(
        f"/api/paper/notebooks/{ideas['path']}/rename",
        json={"new_path": "paper/Plans.paper", "title": "Plans"},
    )
    assert response.status_code == 409, response.text
    response = client.post(
        f"/api/paper/notebooks/{ideas['path']}/rename",
        json={"new_path": "paper/Plans 2.paper", "title": "Plans"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["title"] == "Plans"


def test_a_notebook_rename_that_only_changes_case(state):
    client = make_client(state)
    nb = new_notebook(client, "Plans", folder="Work")
    response = client.post(
        f"/api/paper/notebooks/{nb['path']}/rename",
        json={"new_path": "paper/work/PLANS.paper", "title": "PLANS"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["path"] == "paper/Work/PLANS.paper"
    assert state.paper.notebook_dirs() == ["paper/Work/PLANS.paper"]
    assert state.paper.folders() == ["paper/Work"]
    assert not list(state.paper.root.rglob(".rename-*"))


def test_same_name_on_two_paths_is_two_folders(state):
    client = make_client(state)
    a = new_folder(client, "Work", parent="paper/A")
    b = new_folder(client, "Work", parent="paper/B")
    assert a["path"] == "paper/A/Work" and b["path"] == "paper/B/Work"
    assert a["id"] != b["id"]
    assert a["parent"] == "paper/A" and b["parent"] == "paper/B"


def test_a_missing_parent_is_created_with_records_at_the_same_seq(state):
    client = make_client(state)
    record = new_folder(client, "Accounting", parent="Work")
    assert record["path"] == "paper/Work/Accounting"
    parent = stored(state, "paper/Work")
    assert parent["id"] == legacy_folder_id("paper/Work")
    assert parent["seq"] == record["seq"]
    assert parent["name"] == "Work"


def test_the_parent_is_matched_case_insensitively(state):
    client = make_client(state)
    new_folder(client, "Work")
    record = new_folder(client, "Accounting", parent="paper/work")
    assert record["path"] == "paper/Work/Accounting"
    assert state.paper.folders() == ["paper/Work", "paper/Work/Accounting"]


def test_a_path_longer_than_the_cap_is_refused(state, monkeypatch):
    # 900 bytes in production; smaller here, where the temp directory's own
    # path would otherwise pass the OS limit first.
    monkeypatch.setattr(paper_folders, "MAX_VAULT_PATH_BYTES", 300)
    client = make_client(state)
    parent = "paper"
    # A slug is at most 80 characters: three levels make 248 bytes.
    for _ in range(3):
        parent = new_folder(client, "d" * 80, parent=parent)["path"]
    assert len(parent.encode()) == 248
    response = client.post("/api/paper/folders", json={"parent": parent, "name": "d" * 60})
    assert response.status_code == 400
    assert "300" in response.json()["detail"]
    # A move that would push something inside past the cap is refused too.
    new_folder(client, "t" * 80)
    response = rename(client, "paper/" + "d" * 80, "paper/" + "t" * 80 + "/" + "d" * 80)
    assert response.status_code == 400
    assert state.paper.folders()[0] == "paper/" + "d" * 80


# -- folders without a record ------------------------------------------------


def test_a_directory_without_a_record_is_listed_but_never_written_by_a_get(state):
    (state.paper.root / "Old" / "Inner").mkdir(parents=True)
    client = make_client(state)
    listed = client.get("/api/paper/folders").json()["folders"]
    assert [r["path"] for r in listed] == ["paper/Old", "paper/Old/Inner"]
    old = listed[0]
    assert old["id"] == legacy_folder_id("paper/Old")
    assert old["seq"] == 0 and old["name"] == "Old" and old["color"] == "blue"
    assert listed[1]["parent"] == "paper/Old"
    library = client.get("/api/paper").json()
    assert [r["id"] for r in library["folder_records"]] == [r["id"] for r in listed]
    assert not (state.paper.root / "Old" / "folder.json").exists()


def test_create_claims_a_directory_that_has_no_record(state):
    (state.paper.root / "Old").mkdir(parents=True)
    record = new_folder(make_client(state), "old", id=FOLDER_ID, color="green")
    assert record["path"] == "paper/Old" and record["id"] == FOLDER_ID
    assert record["existing"] is False
    assert record["name"] == "old" and record["color"] == "green"
    assert stored(state, "paper/Old")["id"] == FOLDER_ID


def test_boot_writes_the_missing_records_and_re_ids_a_copy(state):
    root = state.paper.root
    (root / "Work" / "Sub").mkdir(parents=True)
    (root / "Work Copy").mkdir()
    (root / "Bad").mkdir()
    (root / "Work" / "folder.json").write_text(json.dumps({"id": FOLDER_ID, "name": "Work"}))
    (root / "Work Copy" / "folder.json").write_text(
        json.dumps({"id": FOLDER_ID, "name": "Work", "extra": 1})
    )
    (root / "Bad" / "folder.json").write_text("{not json")

    before = seq_now(state)
    written = state.paper.ensure_folder_records()
    # The earlier path keeps a shared id; the copy takes its own legacy one.
    assert written == ["paper/Bad", "paper/Work Copy", "paper/Work/Sub"]
    assert stored(state, "paper/Work")["id"] == FOLDER_ID
    copy = stored(state, "paper/Work Copy")
    assert copy["id"] == legacy_folder_id("paper/Work Copy")
    assert copy["name"] == "Work" and copy["extra"] == 1
    assert stored(state, "paper/Bad")["id"] == legacy_folder_id("paper/Bad")
    assert list(root.glob("Bad/folder.json.corrupt-*"))
    sub = stored(state, "paper/Work/Sub")
    assert sub["id"] == legacy_folder_id("paper/Work/Sub")
    # One new seq each.
    seqs = sorted(stored(state, p)["seq"] for p in written)
    assert seqs == list(range(before + 1, before + 4))
    assert state.paper.ensure_folder_records() == []


def test_renaming_a_folder_without_a_record_keeps_its_legacy_id(state):
    (state.paper.root / "Old" / "Inner").mkdir(parents=True)
    client = make_client(state)
    response = rename(client, "Old", "paper/Archive", name="Archive")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["folder"]["id"] == legacy_folder_id("paper/Old")
    assert body["folder"]["name"] == "Archive"
    # The folder inside got its record before it moved, so its id did not
    # change with its path.
    assert stored(state, "paper/Archive/Inner")["id"] == legacy_folder_id("paper/Old/Inner")


# -- rename and move ---------------------------------------------------------


def test_rename_moves_the_directory_and_restamps_nothing_inside(state):
    client = make_client(state)
    work = new_folder(client, "Work", id=FOLDER_ID)
    nb = new_notebook(client, "Ideas", folder="Work")
    assert put_page(client, nb["path"], PAGE, 0, drawing=b"ink", render=PNG).status_code == 200
    page_seq = state.paper.read_page(nb["path"], PAGE)["seq"]
    manifest_seq = state.paper.read_notebook(nb["path"])["seq"]
    cursor = seq_now(state)

    response = rename(client, "paper/Work", "paper/Work 2026", name="Work 2026")
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"folder", "from", "path"}
    assert body["from"] == "paper/Work" and body["path"] == "paper/Work 2026"
    folder = body["folder"]
    assert set(folder) == RECORD_KEYS
    assert folder["id"] == FOLDER_ID and folder["name"] == "Work 2026"
    assert folder["path"] == "paper/Work 2026" and folder["seq"] == cursor + 1
    assert folder["created"] == work["created"] and folder["modified"] != work["modified"]

    moved = "paper/Work 2026/Ideas.paper"
    assert not (state.paper.root / "Work").exists()
    assert state.paper.read_page(moved, PAGE)["seq"] == page_seq
    assert state.paper.read_notebook(moved)["seq"] == manifest_seq

    changes = feed(client, cursor)
    assert changes["notebooks"] == [] and changes["pages"] == []
    assert changes["deleted"] == []
    assert [r["path"] for r in changes["folders"]] == ["paper/Work 2026"]
    assert changes["folder_moves"] == [
        {
            "id": FOLDER_ID,
            "from": "paper/Work",
            "to": "paper/Work 2026",
            "seq": cursor + 1,
            "at": changes["folder_moves"][0]["at"],
        }
    ]


def test_a_move_without_a_name_keeps_the_name(state):
    client = make_client(state)
    new_folder(client, "Accounting", parent="paper/Work", id=FOLDER_ID)
    new_folder(client, "Archive")
    body = rename(client, "paper/Work/Accounting", "paper/Archive/Accounting").json()
    assert body["path"] == "paper/Archive/Accounting"
    assert body["folder"]["name"] == "Accounting"
    assert body["folder"]["parent"] == "paper/Archive"


def test_a_rename_onto_a_siblings_name_is_suffixed(state):
    client = make_client(state)
    new_folder(client, "Work")
    new_folder(client, "Jobs", id=FOLDER_ID)
    body = rename(client, "paper/Jobs", "paper/work", name="work").json()
    assert body["path"] == "paper/work 2"
    assert body["folder"]["name"] == "work" and body["folder"]["id"] == FOLDER_ID
    # A notebook's name counts too.
    new_notebook(client, "Plans")
    assert rename(client, "paper/work 2", "paper/Plans").json()["path"] == "paper/Plans 2"


def test_a_rename_that_only_changes_case(state):
    client = make_client(state)
    new_folder(client, "Work", id=FOLDER_ID)
    new_notebook(client, "Ideas", folder="Work")
    cursor = seq_now(state)
    body = rename(client, "paper/Work", "paper/WORK", name="WORK").json()
    assert body["path"] == "paper/WORK" and body["from"] == "paper/Work"
    assert state.paper.folders() == ["paper/WORK"]
    assert state.paper.notebook_dirs() == ["paper/WORK/Ideas.paper"]
    assert not list(state.paper.root.glob(".rename-*"))
    assert [m["to"] for m in feed(client, cursor)["folder_moves"]] == ["paper/WORK"]


def test_a_retitle_with_the_same_slug_stays_put(state):
    client = make_client(state)
    new_folder(client, "Work", id=FOLDER_ID)
    cursor = seq_now(state)
    body = rename(client, "paper/Work", "paper/Work", name="Work").json()
    assert body["path"] == "paper/Work" and seq_now(state) == cursor
    body = rename(client, "paper/Work", "paper/Work", name=" Work ").json()
    assert seq_now(state) == cursor
    body = rename(client, "Work", "paper/Work", name="Work!").json()
    assert body == {"folder": body["folder"], "from": "paper/Work", "path": "paper/Work"}
    assert body["folder"]["name"] == "Work!" and body["folder"]["seq"] == cursor + 1
    assert feed(client, cursor)["folder_moves"] == []


@pytest.mark.parametrize(
    "new_path, status",
    [
        ("paper/Work/Inside", 400),
        ("paper/Work/Sub/Deeper", 400),
        ("paper/Work.paper", 400),
        ("paper/Archive.PAPER", 400),
        ("paper/folder.json", 400),
        ("paper", 400),
        ("paper/../out", 400),
    ],
)
def test_rename_refuses_impossible_targets(state, new_path, status):
    client = make_client(state)
    new_folder(client, "Sub", parent="paper/Work")
    response = rename(client, "paper/Work", new_path)
    assert response.status_code == status, response.text
    assert state.paper.folders() == ["paper/Work", "paper/Work/Sub"]


def test_rename_of_a_missing_folder_is_404(state):
    response = rename(make_client(state), "paper/Nowhere", "paper/Somewhere")
    assert response.status_code == 404


def test_a_move_into_a_new_parent_makes_the_parent_with_a_record(state):
    client = make_client(state)
    new_folder(client, "Work", id=FOLDER_ID)
    body = rename(client, "paper/Work", "paper/Archive/2026/Work").json()
    assert body["path"] == "paper/Archive/2026/Work"
    for path in ("paper/Archive", "paper/Archive/2026"):
        assert stored(state, path)["id"] == legacy_folder_id(path)
        assert stored(state, path)["seq"] == body["folder"]["seq"]


# -- patch ---------------------------------------------------------------------


def test_patch_sets_colour_icon_and_favourite_but_never_the_name(state):
    client = make_client(state)
    new_folder(client, "Work", id=FOLDER_ID, icon="symbol:folder")
    response = client.patch(
        "/api/paper/folders/paper/Work",
        json={"color": "purple", "favorite": True, "name": "Hacked"},
    )
    assert response.status_code == 200, response.text
    record = response.json()
    assert set(record) == RECORD_KEYS
    assert (record["color"], record["favorite"], record["name"]) == ("purple", True, "Work")
    assert record["icon"] == "symbol:folder"
    cleared = client.patch("/api/paper/folders/Work", json={"icon": ""}).json()
    assert cleared["icon"] is None and cleared["seq"] == record["seq"] + 1
    emoji = client.patch("/api/paper/folders/Work", json={"icon": "emoji:\U0001F4D2"}).json()
    assert emoji["icon"] == "emoji:\U0001F4D2"


def test_a_patch_that_changes_nothing_writes_nothing(state):
    client = make_client(state)
    record = new_folder(client, "Work", color="red")
    cursor = seq_now(state)
    same = client.patch("/api/paper/folders/paper/Work", json={"color": "red"}).json()
    assert same == {key: value for key, value in record.items() if key != "existing"}
    assert seq_now(state) == cursor


def test_patch_refuses_bad_values_and_unknown_folders(state):
    client = make_client(state)
    new_folder(client, "Work")
    assert client.patch("/api/paper/folders/Work", json={"color": "beige"}).status_code == 400
    assert client.patch("/api/paper/folders/Work", json={"icon": "emoji:ab"}).status_code == 400
    assert client.patch("/api/paper/folders/Nope", json={"favorite": True}).status_code == 404


def test_routes_are_not_swallowed_by_the_folder_path(state):
    """A folder literally named "rename" still renames and patches."""
    client = make_client(state)
    new_folder(client, "rename", id=FOLDER_ID)
    assert client.patch("/api/paper/folders/paper/rename", json={"favorite": True}).json()["favorite"]
    body = rename(client, "paper/rename", "paper/renamed").json()
    assert body["from"] == "paper/rename" and body["path"] == "paper/renamed"
    assert client.get("/api/paper/changes").status_code == 200
    assert client.get("/api/paper/trash").status_code == 200


# -- delete and restore --------------------------------------------------------


def _work_with_contents(client, state):
    new_folder(client, "Work", id=FOLDER_ID)
    new_folder(client, "Sub", parent="paper/Work", id=OTHER_ID)
    a = new_notebook(client, "Ideas", folder="Work")
    b = new_notebook(client, "Plans", folder="Work/Sub")
    for nb in (a, b):
        assert put_page(client, nb["path"], PAGE, 0, drawing=b"ink").status_code == 200
    asyncio.run(state.paper_index.sync_all())
    return a, b


def test_delete_trashes_the_folder_as_one_item_and_tombstones_its_contents(state):
    client = make_client(state)
    a, b = _work_with_contents(client, state)
    cursor = seq_now(state)

    response = client.delete("/api/paper/folders/paper/Work")
    assert response.status_code == 200, response.text
    item = response.json()
    assert set(item) == TRASH_KEYS
    assert item["kind"] == "paper_folder" and item["original_path"] == "paper/Work"
    assert item["note_count"] == 0 and item["notebook_count"] == 2
    assert item["folder_id"] == FOLDER_ID
    assert not (state.paper.root / "Work").exists()

    changes = feed(client, cursor)
    deleted = changes["deleted"]
    assert {row["seq"] for row in deleted} == {cursor + 1}
    assert sorted((row["kind"], row["path"], row.get("id")) for row in deleted) == [
        ("folder", "paper/Work", FOLDER_ID),
        ("folder", "paper/Work/Sub", OTHER_ID),
        ("notebook", a["path"], None),
        ("notebook", b["path"], None),
    ]
    assert changes["folders"] == [] and changes["notebooks"] == []

    async def rows():
        db = state.paper_index.db
        return [
            await db.execute_fetchall(f"SELECT path FROM {table}")
            for table in ("paper_folders", "paper_notebooks", "paper_pages")
        ]

    assert asyncio.run(rows()) == [[], [], []]
    trash = client.get("/api/paper/trash").json()["items"]
    assert [i["kind"] for i in trash] == ["paper_folder"]
    assert state.trash.preview(item["id"])["content"] is None


def test_delete_of_a_missing_folder_is_404(state):
    assert make_client(state).delete("/api/paper/folders/Nope").status_code == 404


def test_restore_puts_everything_back_and_restamps_it(state):
    client = make_client(state)
    a, b = _work_with_contents(client, state)
    modified = state.paper.read_notebook(a["path"])["modified"]
    item = client.delete("/api/paper/folders/Work").json()
    cursor = seq_now(state)

    response = client.post(f"/api/paper/trash/{item['id']}/restore")
    assert response.status_code == 200, response.text
    restored = response.json()
    assert restored == {
        "id": item["id"], "kind": "paper_folder", "path": "paper/Work",
        "notes": [], "folder_id": FOLDER_ID,
    }
    assert state.paper.notebook_dirs() == sorted([a["path"], b["path"]])
    assert state.paper.tombstones() == []

    changes = feed(client, cursor)
    assert changes["deleted"] == []
    assert sorted(r["path"] for r in changes["folders"]) == ["paper/Work", "paper/Work/Sub"]
    assert sorted(n["path"] for n in changes["notebooks"]) == sorted([a["path"], b["path"]])
    assert len(changes["pages"]) == 2
    assert all(row["seq"] > cursor for row in changes["pages"])
    # A restore re-stamps for the feed; the library's dates stay the user's.
    assert state.paper.read_notebook(a["path"])["modified"] == modified
    assert asyncio.run(
        state.paper_index.db.execute_fetchall("SELECT id FROM paper_folders ORDER BY path")
    ) and asyncio.run(state.paper_index.count_pages()) == 2


def test_a_notebook_made_in_a_trashed_folders_path_brings_a_new_folder(state):
    """The folder comes back with a legacy id, holding that notebook; the
    trashed one restores beside it under a suffix."""
    client = make_client(state)
    _work_with_contents(client, state)
    item = client.delete("/api/paper/folders/Work").json()

    nb = new_notebook(client, "Late", folder="Work")
    recreated = stored(state, "paper/Work")
    assert recreated["id"] == legacy_folder_id("paper/Work")
    assert recreated["seq"] == nb["seq"]
    assert not [t for t in state.paper.tombstones() if t["path"] == "paper/Work"]
    assert [t["path"] for t in feed(client, 1)["deleted"] if t["kind"] == "folder"] == [
        "paper/Work/Sub"
    ]

    restored = client.post(f"/api/paper/trash/{item['id']}/restore").json()
    assert restored["path"] == "paper/Work 2" and restored["folder_id"] == FOLDER_ID
    assert stored(state, "paper/Work 2")["name"] == "Work"
    assert stored(state, "paper/Work")["id"] == legacy_folder_id("paper/Work")


def test_a_restored_folder_whose_id_was_taken_gets_its_own(state):
    """A legacy folder trashed, then recreated at its path by a notebook:
    both would carry legacy(paper/Old)."""
    (state.paper.root / "Old").mkdir(parents=True)
    client = make_client(state)
    item = client.delete("/api/paper/folders/Old").json()
    assert item["folder_id"] == legacy_folder_id("paper/Old")
    new_notebook(client, "Late", folder="Old")

    restored = client.post(f"/api/paper/trash/{item['id']}/restore").json()
    assert restored["path"] == "paper/Old 2"
    assert restored["folder_id"] == legacy_folder_id("paper/Old 2")
    ids = [r["id"] for r in client.get("/api/paper/folders").json()["folders"]]
    assert len(ids) == len(set(ids)) == 2


def test_the_generic_trash_restore_takes_the_folder_path(state):
    client = make_client(state)
    _work_with_contents(client, state)
    item = client.delete("/api/paper/folders/Work").json()
    restored = asyncio.run(paper_api.restore_paper_item(state, item["id"]))
    assert restored["kind"] == "paper_folder" and restored["path"] == "paper/Work"


# -- listing and feed ----------------------------------------------------------


def test_the_library_keeps_its_released_keys_and_adds_records(state):
    client = make_client(state)
    new_folder(client, "Empty")
    work = new_folder(client, "Work", id=FOLDER_ID)
    nested = new_notebook(client, "Ideas", folder="Work")
    top = new_notebook(client, "Loose")
    library = client.get("/api/paper").json()
    assert library["folders"] == ["paper/Empty", "paper/Work"]
    assert [r["path"] for r in library["folder_records"]] == library["folders"]
    by_path = {nb["path"]: nb for nb in library["notebooks"]}
    assert by_path[nested["path"]]["folder"] == "paper/Work"
    assert by_path[nested["path"]]["folder_id"] == work["id"]
    assert by_path[top["path"]]["folder"] == "paper"
    assert by_path[top["path"]]["folder_id"] is None
    # folder.json is a file in the folder, never a notebook or a folder.
    assert all(not p.endswith("folder.json") for p in library["folders"])


def test_a_full_feed_lists_every_folder_and_no_moves(state):
    client = make_client(state)
    new_folder(client, "Work", id=FOLDER_ID)
    rename(client, "paper/Work", "paper/Jobs")
    (state.paper.root / "Legacy").mkdir()
    full = client.get("/api/paper/changes").json()
    assert full["full"] is True and full["folder_moves"] == []
    assert [r["path"] for r in full["folders"]] == ["paper/Jobs", "paper/Legacy"]
    assert full["folders"][1]["seq"] == 0


def test_the_index_and_the_walker_answer_alike(state):
    client = make_client(state)
    _work_with_contents(client, state)
    cursor = seq_now(state)
    rename(client, "paper/Work/Sub", "paper/Sub", name="Sub")
    new_folder(client, "Fresh", color="teal")
    client.delete("/api/paper/folders/paper/Work")
    epoch = state.paper.seq_state()["epoch"]
    for since in (0, cursor):
        walked = paper_api.changes_since(state.paper, epoch, since)
        indexed = asyncio.run(state.paper_index.changes_since(epoch, since))
        assert indexed == walked, since
    incremental = paper_api.changes_since(state.paper, epoch, cursor)
    assert [m["to"] for m in incremental["folder_moves"]] == ["paper/Sub"]
    assert {r["path"] for r in incremental["folders"]} == {"paper/Sub", "paper/Fresh"}


def test_a_move_keeps_search_and_vectors_on_the_moved_pages(state):
    client = make_client(state)
    new_folder(client, "Work", id=FOLDER_ID)
    nb = new_notebook(client, "Ideas", folder="Work")
    put_page(
        client, nb["path"], PAGE, 0, drawing=b"ink",
        text={"lines": [], "transcript": "quarterly budget"},
    )
    before = asyncio.run(
        state.paper_index.db.execute_fetchall("SELECT path, seq FROM paper_pages")
    )
    rename(client, "paper/Work", "paper/Jobs", name="Jobs")
    moved = f"paper/Jobs/Ideas.paper/pages/{PAGE}"
    hits = asyncio.run(state.paper_index.search("budget"))
    assert [h["path"] for h in hits] == [moved]
    after = asyncio.run(
        state.paper_index.db.execute_fetchall("SELECT path, seq FROM paper_pages")
    )
    assert [tuple(r) for r in after] == [(moved, before[0]["seq"])]
    assert state.semantic.removed[-1] == f"paper/Work/Ideas.paper/pages/{PAGE}"
    assert state.semantic.indexed[-1][0] == moved
    rows = asyncio.run(
        state.paper_index.db.execute_fetchall(
            "SELECT path, folder FROM paper_notebooks"
        )
    )
    assert [tuple(r) for r in rows] == [("paper/Jobs/Ideas.paper", "paper/Jobs")]


def test_sync_all_indexes_records_and_seq_rollback_sees_them(state):
    client = make_client(state)
    new_folder(client, "Work", id=FOLDER_ID)
    asyncio.run(state.paper_index.sync_all())
    rows = asyncio.run(
        state.paper_index.db.execute_fetchall("SELECT id, path, parent, seq FROM paper_folders")
    )
    assert [tuple(r) for r in rows] == [(FOLDER_ID, "paper/Work", "paper", seq_now(state))]
    # seq.json rewound under the index (a git reset): a new epoch.
    epoch = state.paper.seq_state()["epoch"]
    seq_file = state.paper._seq_file()
    data = json.loads(seq_file.read_text())
    data["seq"] = 0
    seq_file.write_text(json.dumps(data))
    asyncio.run(state.paper_index.sync_all())
    assert state.paper.seq_state()["epoch"] != epoch


def test_moves_are_pruned_with_the_tombstones(state):
    client = make_client(state)
    new_folder(client, "Work")
    rename(client, "paper/Work", "paper/Jobs")
    moves = state.paper.folder_moves()
    assert len(moves) == 1
    stale = [{**moves[0], "at": "2000-01-01T00:00:00Z"}]
    internal.atomic_json(state.paper._moves_file(), stale)
    state.paper.prune_tombstones(TOMBSTONE_DAYS)
    assert state.paper.folder_moves() == []
    assert state.paper.seq_state()["pruned"] == moves[0]["seq"]


def test_store_errors_are_note_errors(state):
    with pytest.raises(NoteError) as exc:
        state.paper.find_folder("paper")
    assert exc.value.status == 400


def test_the_boot_pass_puts_back_a_case_only_rename_cut_short(state):
    """A folder and a notebook renamed only in case go through
    ``.rename-<id>``; a process stopped between the two renames left it
    there, a folder nobody named holding notebooks nothing found. The boot
    pass puts each back under the name its record carries, beside anything
    that took that name since, and never fails the boot."""
    client = make_client(state)
    work = new_folder(client, "Work")
    inside = new_notebook(client, "Ideas", folder=work["path"])
    plans = new_notebook(client, "Plans")
    root = state.paper.dir
    (root / "paper" / "Work").rename(root / "paper" / f".rename-{work['id']}")
    plans_id = json.loads((root / plans["path"] / "notebook.json").read_text())["id"]
    (root / plans["path"]).rename(root / "paper" / f".rename-{plans_id}")
    # Something took "Plans" since; the notebook goes beside it.
    new_folder(client, "Plans")
    # One whose record cannot be read comes back too, as "Untitled".
    (root / "paper" / ".rename-broken" / "folder.json").parent.mkdir()
    (root / "paper" / ".rename-broken" / "folder.json").write_text("{not json")

    state.paper.ensure_folder_records()

    assert (root / "paper" / "Work" / "folder.json").is_file()
    assert inside["path"] in state.paper.notebook_dirs()
    assert "paper/Plans 2.paper" in state.paper.notebook_dirs()
    assert not [p for p in state.paper.folders() if ".rename-" in p]
    assert "paper/Untitled" in state.paper.folders()
    assert state.paper.folder_record("paper/Work")["id"] == work["id"]
