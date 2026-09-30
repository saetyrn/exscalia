"""PaperStore: the rules that keep ink safe.

Ink is the one thing in the vault nothing can regenerate, so these tests are
mostly about what the store refuses to do — overwrite a page whose revision
moved, drop a page a stale client forgot to list, let a path out of ``paper/``.
"""

import asyncio
import hashlib
import json
import os

import pytest

from app.paper_store import PaperConflict, PaperStore, page_url
from app.vault import internal
from app.vault.notes import NoteError

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


def make_notebook(state, title="Ideas", **kwargs):
    return state.paper.create_notebook(title=title, **kwargs)


# -- resolvers ---------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "paper/../secrets.paper",
        "paper/..",
        "paper/Notes.md",
        "paper/Work",
        "paper/Outer.paper/Inner.paper",
        ".exscalia/paper/Sneaky.paper",
        "paper/.git/Evil.paper",
        "",
    ],
)
def test_resolver_rejects_bad_notebook_paths(state, bad):
    with pytest.raises(NoteError) as exc:
        state.paper.notebook_path(bad)
    assert exc.value.status == 400


def test_resolver_accepts_with_or_without_the_root(state):
    assert state.paper.notebook_path("Work/Ideas.paper") == "paper/Work/Ideas.paper"
    assert (
        state.paper.notebook_path("paper/Work/Ideas.paper") == "paper/Work/Ideas.paper"
    )


@pytest.mark.parametrize(
    "bad", ["..", "a/b", "with space", "x" * 65, ".hidden", "id.conflict-nope"]
)
def test_resolver_rejects_bad_page_ids(state, bad):
    with pytest.raises(NoteError):
        PaperStore.check_page_id(bad)


def test_conflict_sibling_ids_are_valid_page_ids(state):
    assert PaperStore.check_page_id("abc.conflict-20260902121314")


# -- notebooks ---------------------------------------------------------------


def test_create_read_rename_and_delete_a_notebook(state):
    nb = make_notebook(state, "Quarterly Review", folder="Work")
    assert nb["path"] == "paper/Work/Quarterly Review.paper"
    assert nb["template"] == "ruled" and nb["paperSize"] == "a4"
    assert state.paper.read_notebook(nb["path"])["title"] == "Quarterly Review"

    moved = state.paper.rename_notebook(nb["path"], "paper/Archive/Q3.paper")
    assert moved["path"] == "paper/Archive/Q3.paper"
    # A path alone is a move: the directory is only the slug, and the title is
    # the user's own text, so it stays.
    assert moved["title"] == "Quarterly Review"
    assert not (state.settings.vault_dir / nb["path"]).exists()

    item = state.paper.delete_notebook(moved["path"])
    assert item["kind"] == "notebook"
    assert not (state.settings.vault_dir / moved["path"]).exists()
    # The move left a tombstone at the old path (the feed reports a rename as
    # a delete plus an add), and the delete left one at the new path.
    tombstoned = {row["path"] for row in state.paper.tombstones()}
    assert tombstoned == {nb["path"], moved["path"]}

    restored = state.paper.restore_trash(item["id"])
    assert restored["path"] == moved["path"]
    assert state.paper.read_notebook(moved["path"])["title"] == "Quarterly Review"
    assert [row["path"] for row in state.paper.tombstones()] == [nb["path"]]


def test_library_lists_folders_and_survives_a_corrupt_manifest(state):
    good = make_notebook(state, "Good", folder="Work")
    bad = make_notebook(state, "Bad", folder="Work")
    (state.settings.vault_dir / bad["path"] / "notebook.json").write_text("{oops")

    library = state.paper.list_library()
    assert [nb["path"] for nb in library["notebooks"]] == [good["path"]]
    assert "paper/Work" in library["folders"]


def test_title_is_verbatim_while_the_directory_is_windows_safe(state):
    nb = make_notebook(state, 'Ideas: "2026/Q3"')
    assert nb["title"] == 'Ideas: "2026/Q3"'
    assert ":" not in nb["path"] and '"' not in nb["path"]


def test_a_rename_keeps_the_verbatim_title_unless_it_brings_one(state):
    """Moving 'Q3: Plans?' must not turn it into its slug 'Q3 Plans'."""
    nb = make_notebook(state, "Q3: Plans?", folder="Work")
    assert nb["path"] == "paper/Work/Q3 Plans.paper"

    moved = state.paper.rename_notebook(nb["path"], "paper/Archive/Q3 Plans.paper")
    assert moved["title"] == "Q3: Plans?"
    assert state.paper.read_notebook(moved["path"])["title"] == "Q3: Plans?"

    retitled = state.paper.rename_notebook(
        moved["path"], "paper/Archive/Q4 Plans.paper", title="  Q4: Plans!  "
    )
    assert retitled["title"] == "Q4: Plans!"
    blank = state.paper.rename_notebook(retitled["path"], "paper/Q4.paper", title=" ")
    assert blank["title"] == "Q4: Plans!"

    # A retitle whose slug does not change stays at its path and still lands,
    # with a fresh seq so other devices hear about it, and no tombstone.
    same = state.paper.rename_notebook(blank["path"], blank["path"], title="Q4?")
    assert same["path"] == "paper/Q4.paper" and same["title"] == "Q4?"
    assert same["seq"] > blank["seq"]
    assert "paper/Q4.paper" not in {row["path"] for row in state.paper.tombstones()}
    unchanged = state.paper.rename_notebook(same["path"], same["path"], title="Q4?")
    assert unchanged["seq"] == same["seq"]

    # Only a manifest with no title at all falls back to the directory name.
    manifest_file = state.settings.vault_dir / same["path"] / "notebook.json"
    raw = json.loads(manifest_file.read_text())
    del raw["title"]
    manifest_file.write_text(json.dumps(raw))
    fallback = state.paper.rename_notebook(same["path"], "paper/Fallback.paper")
    assert fallback["title"] == "Fallback"


def test_a_rename_with_no_title_in_the_same_folder_takes_the_new_name(state):
    """An iPad build from before titles rode the rename retitles with the path
    alone; keeping the old title would have its next pull revert the user's."""
    nb = make_notebook(state, "Draft", folder="Work")
    renamed = state.paper.rename_notebook(nb["path"], "paper/Work/Final.paper")
    assert renamed["title"] == "Final"
    assert state.paper.read_notebook(renamed["path"])["title"] == "Final"

    # A title that already slugs to the new name is left verbatim.
    first = make_notebook(state, "Q3: Plans?", folder="Work")
    second = make_notebook(state, "Q3: Plans?", folder="Work")
    assert second["path"] == "paper/Work/Q3 Plans 2.paper"
    state.paper.delete_notebook(first["path"])
    back = state.paper.rename_notebook(second["path"], first["path"])
    assert back["title"] == "Q3: Plans?"

    # A move to another folder is never a retitle.
    moved = state.paper.rename_notebook(renamed["path"], "paper/Archive/Draft.paper")
    assert moved["title"] == "Final"


# -- pages -------------------------------------------------------------------


def test_create_page_lists_it_and_seeds_the_cover(state):
    nb = make_notebook(state)
    first = state.paper.create_page(nb["path"], page_id="a" * 32)
    second = state.paper.create_page(nb["path"], page_id="b" * 32)
    third = state.paper.create_page(nb["path"], page_id="c" * 32, after="a" * 32)

    manifest = state.paper.read_notebook(nb["path"])
    assert manifest["pages"] == ["a" * 32, "c" * 32, "b" * 32]
    assert manifest["cover"] == "a" * 32
    assert first["revision"] == 1 and second["size"]["width"] == 595
    assert third["template"] == "ruled"


def test_source_write_bumps_the_revision_and_stores_the_ink(state):
    nb = make_notebook(state)
    page_id = "a" * 32
    created = state.paper.save_page(
        nb["path"], page_id, base_revision=0, drawing=b"ink-one"
    )
    assert created["revision"] == 1
    assert created["page"]["path"] == f"{nb['path']}/pages/{page_id}"
    assert state.paper.read_notebook(nb["path"])["pages"] == [page_id]

    again = state.paper.save_page(
        nb["path"], page_id, base_revision=1, drawing=b"ink-two"
    )
    assert again["revision"] == 2
    blob, _page = state.paper.read_blob(nb["path"], page_id, "drawing.pkdrawing")
    assert blob.read_bytes() == b"ink-two"


def test_derived_only_save_leaves_the_revision_alone(state):
    nb = make_notebook(state)
    page_id = "a" * 32
    state.paper.save_page(nb["path"], page_id, base_revision=0, drawing=b"ink")
    result = state.paper.save_page(
        nb["path"],
        page_id,
        base_revision=1,
        render=PNG,
        text={"lines": [], "transcript": "hello"},
        for_revision=1,
    )
    assert result["revision"] == 1
    assert result["stale"] is False
    page = state.paper.read_page(nb["path"], page_id)
    assert page["revision"] == 1
    assert page["render_revision"] == 1 and page["text_revision"] == 1


def test_a_derived_write_made_for_an_older_revision_is_kept_but_flagged(state):
    nb = make_notebook(state)
    page_id = "a" * 32
    state.paper.save_page(nb["path"], page_id, base_revision=0, drawing=b"one")
    state.paper.save_page(nb["path"], page_id, base_revision=1, drawing=b"two")
    result = state.paper.save_page(
        nb["path"], page_id, base_revision=2, render=PNG, for_revision=1
    )
    assert result["stale"] is True
    assert result["page"]["render_revision"] == 1
    assert result["page"]["revision"] == 2


def test_create_over_an_existing_page_conflicts(state):
    nb = make_notebook(state)
    page_id = "a" * 32
    state.paper.save_page(nb["path"], page_id, base_revision=0, drawing=b"mine")
    with pytest.raises(PaperConflict) as exc:
        state.paper.save_page(nb["path"], page_id, base_revision=0, drawing=b"theirs")
    assert exc.value.status == 409
    assert exc.value.conflict["sibling"]["conflict_of"] == page_id


def test_a_stale_source_write_parks_a_sibling_and_leaves_the_original(state):
    nb = make_notebook(state)
    page_id = "a" * 32
    state.paper.save_page(nb["path"], page_id, base_revision=0, drawing=b"one")
    state.paper.save_page(nb["path"], page_id, base_revision=1, drawing=b"two")
    # A page after it, so "directly after the original" is not the same as
    # "appended to the end".
    state.paper.create_page(nb["path"], page_id="z" * 32)

    with pytest.raises(PaperConflict) as exc:
        state.paper.save_page(
            nb["path"], page_id, base_revision=1, drawing=b"from-the-other-device"
        )
    conflict = exc.value.conflict

    # The original is byte-for-byte what it was.
    original_blob, _ = state.paper.read_blob(nb["path"], page_id, "drawing.pkdrawing")
    assert original_blob.read_bytes() == b"two"
    assert conflict["original"]["revision"] == 2

    sibling_id = conflict["sibling"]["id"]
    assert sibling_id.startswith(f"{page_id}.conflict-")
    sibling_blob, _ = state.paper.read_blob(
        nb["path"], sibling_id, "drawing.pkdrawing"
    )
    assert sibling_blob.read_bytes() == b"from-the-other-device"

    # …and the sibling sits directly after the page it forked from.
    pages = state.paper.read_notebook(nb["path"])["pages"]
    assert pages == [page_id, sibling_id, "z" * 32]


def test_a_missing_page_is_404_not_a_conflict(state):
    nb = make_notebook(state)
    with pytest.raises(NoteError) as exc:
        state.paper.save_page(nb["path"], "a" * 32, base_revision=3, drawing=b"x")
    assert exc.value.status == 404


# -- retries after a lost response -------------------------------------------


def _page_dirs(state, nb):
    return state.paper._on_disk_pages(nb["path"])


def test_a_retried_save_is_answered_like_the_original(state):
    """The iPad re-sends a save whose response it never got with the same
    base; that must not fork the page into a conflict with itself."""
    nb = make_notebook(state)
    page_id = "a" * 32
    first = state.paper.save_page(
        nb["path"], page_id, base_revision=0, drawing=b"X", client_id="ipad-1"
    )
    assert first["revision"] == 1

    again = state.paper.save_page(
        nb["path"], page_id, base_revision=0, drawing=b"X", client_id="ipad-1"
    )
    assert again["revision"] == 1 and again["stale"] is False
    assert again["replayed"] is True
    assert again["page"]["path"] == first["page"]["path"]
    # Nothing was written: not even a fresh seq.
    assert state.paper.read_page(nb["path"], page_id)["seq"] == first["page"]["seq"]
    assert state.paper.read_notebook(nb["path"])["pages"] == [page_id]
    assert _page_dirs(state, nb) == [page_id]

    # The same holds for an edit, not only for the create.
    state.paper.save_page(
        nb["path"], page_id, base_revision=1, drawing=b"X2", client_id="ipad-1"
    )
    replay = state.paper.save_page(
        nb["path"], page_id, base_revision=1, drawing=b"X2", client_id="ipad-1"
    )
    assert replay["revision"] == 2 and replay["replayed"] is True
    assert _page_dirs(state, nb) == [page_id]


def test_a_replay_still_lands_the_render_made_since(state):
    """A transcript that finished while the retry waited rides only on the
    retry; the ink is already there, the derived parts are not."""
    nb = make_notebook(state)
    page_id = "a" * 32
    state.paper.save_page(
        nb["path"], page_id, base_revision=0, drawing=b"X", client_id="ipad-1"
    )
    replay = state.paper.save_page(
        nb["path"],
        page_id,
        base_revision=0,
        drawing=b"X",
        render=PNG,
        text={"lines": [], "transcript": "hello"},
        for_revision=1,
        client_id="ipad-1",
    )
    assert replay["replayed"] is True and replay["revision"] == 1
    assert replay["stale"] is False
    page = state.paper.read_page(nb["path"], page_id)
    assert page["revision"] == 1
    assert page["render_revision"] == 1 and page["text_revision"] == 1
    assert _text_of(state, nb["path"], page_id)["transcript"] == "hello"


def test_a_retry_with_newer_ink_continues_instead_of_forking(state):
    nb = make_notebook(state)
    page_id = "a" * 32
    state.paper.save_page(
        nb["path"], page_id, base_revision=0, drawing=b"X", client_id="ipad-1"
    )
    # The device kept drawing before the retry went out.
    later = state.paper.save_page(
        nb["path"],
        page_id,
        base_revision=0,
        drawing=b"Y",
        render=PNG,
        for_revision=1,
        client_id="ipad-1",
    )
    assert later["revision"] == 2 and later.get("replayed") is None
    # The render came with this ink, so it describes the new revision.
    assert later["stale"] is False and later["page"]["render_revision"] == 2
    blob, _ = state.paper.read_blob(nb["path"], page_id, "drawing.pkdrawing")
    assert blob.read_bytes() == b"Y"
    assert _page_dirs(state, nb) == [page_id]

    # That response was lost too: the next retry from the same base goes on.
    third = state.paper.save_page(
        nb["path"], page_id, base_revision=0, drawing=b"Z", client_id="ipad-1"
    )
    assert third["revision"] == 3
    # Same ink, new metadata is a new revision as well, not a no-op replay.
    titled = state.paper.save_page(
        nb["path"],
        page_id,
        base_revision=0,
        page={"title": "Lecture"},
        drawing=b"Z",
        client_id="ipad-1",
    )
    assert titled["revision"] == 4 and titled["page"]["title"] == "Lecture"
    assert _page_dirs(state, nb) == [page_id]


def test_the_put_after_a_create_whose_response_was_lost_is_not_a_conflict(state):
    """POST /pages landed but its answer did not: the retry gets 409 'already
    exists' and the device, still at revision 0, sends its ink from base 0."""
    nb = make_notebook(state)
    page_id = "a" * 32
    created = state.paper.create_page(nb["path"], page_id=page_id, client_id="ipad-1")
    assert created["revision"] == 1
    with pytest.raises(NoteError) as exc:
        state.paper.create_page(nb["path"], page_id=page_id, client_id="ipad-1")
    assert exc.value.status == 409

    saved = state.paper.save_page(
        nb["path"], page_id, base_revision=0, drawing=b"ink", client_id="ipad-1"
    )
    assert saved["revision"] == 2 and saved.get("replayed") is None
    blob, _ = state.paper.read_blob(nb["path"], page_id, "drawing.pkdrawing")
    assert blob.read_bytes() == b"ink"
    assert _page_dirs(state, nb) == [page_id]
    assert state.paper.read_notebook(nb["path"])["pages"] == [page_id]

    # Somebody else's base-0 write over that page is still a conflict.
    fresh = "b" * 32
    state.paper.create_page(nb["path"], page_id=fresh, client_id="ipad-1")
    for client_id in ("ipad-2", None):
        with pytest.raises(PaperConflict):
            state.paper.save_page(
                nb["path"], fresh, base_revision=0, drawing=b"x", client_id=client_id
            )


def test_only_the_same_client_from_the_same_base_counts_as_a_retry(state):
    nb = make_notebook(state)
    page_id = "a" * 32
    state.paper.save_page(
        nb["path"], page_id, base_revision=0, drawing=b"X", client_id="ipad-1"
    )
    for client_id in ("ipad-2", None):
        with pytest.raises(PaperConflict):
            state.paper.save_page(
                nb["path"], page_id, base_revision=0, drawing=b"X", client_id=client_id
            )

    # Another device wrote on top: the first one's late retry is a conflict.
    other = make_notebook(state, "Other")
    state.paper.save_page(
        other["path"], page_id, base_revision=0, drawing=b"X", client_id="ipad-1"
    )
    state.paper.save_page(
        other["path"], page_id, base_revision=1, drawing=b"W", client_id="ipad-2"
    )
    with pytest.raises(PaperConflict) as exc:
        state.paper.save_page(
            other["path"], page_id, base_revision=0, drawing=b"X", client_id="ipad-1"
        )
    assert exc.value.conflict["original"]["revision"] == 2
    blob, _ = state.paper.read_blob(other["path"], page_id, "drawing.pkdrawing")
    assert blob.read_bytes() == b"W"


# -- text merge --------------------------------------------------------------


def _text_of(state, nb, page_id):
    return json.loads(
        (state.settings.vault_dir / nb / "pages" / page_id / "text.json").read_text()
    )


def test_transcript_ai_survives_a_text_write_at_the_same_revision(state):
    nb = make_notebook(state)
    page_id = "a" * 32
    state.paper.save_page(nb["path"], page_id, base_revision=0, drawing=b"ink")
    state.paper.set_transcript_ai(
        nb["path"], page_id, "AI reading", "test-model", made_for=1
    )

    state.paper.save_page(
        nb["path"],
        page_id,
        base_revision=1,
        text={"lines": [{"text": "on-device"}], "transcript": "on-device"},
        for_revision=1,
    )
    text = _text_of(state, nb["path"], page_id)
    assert text["transcript"] == "on-device"
    assert text["transcript_ai"]["text"] == "AI reading"


def test_transcript_ai_is_dropped_once_the_ink_moves_on(state):
    nb = make_notebook(state)
    page_id = "a" * 32
    state.paper.save_page(nb["path"], page_id, base_revision=0, drawing=b"ink")
    state.paper.set_transcript_ai(
        nb["path"], page_id, "AI reading", "test-model", made_for=1
    )
    state.paper.save_page(nb["path"], page_id, base_revision=1, drawing=b"more ink")

    state.paper.save_page(
        nb["path"],
        page_id,
        base_revision=2,
        text={"lines": [], "transcript": "fresh"},
        for_revision=2,
    )
    text = _text_of(state, nb["path"], page_id)
    assert text["transcript"] == "fresh"
    assert "transcript_ai" not in text


def _etag(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


def test_derived_writes_stamp_the_hash_of_the_bytes_they_wrote(state):
    nb = make_notebook(state)
    page_id = "a" * 32
    saved = state.paper.save_page(
        nb["path"],
        page_id,
        base_revision=0,
        drawing=b"ink",
        render=PNG,
        text={"lines": [], "transcript": "hello"},
    )["page"]
    page_dir = state.paper.dir / nb["path"] / "pages" / page_id
    assert saved["render_etag"] == _etag(PNG)
    assert saved["text_etag"] == _etag((page_dir / "text.json").read_bytes())
    assert "drawing_etag" not in saved

    # A correction at the same revision: same text_revision, new bytes, new hash.
    corrected = state.paper.save_page(
        nb["path"],
        page_id,
        base_revision=1,
        text={"lines": [], "transcript": "hello", "corrected_transcript": "hullo"},
        for_revision=1,
    )["page"]
    assert corrected["text_revision"] == saved["text_revision"] == 1
    assert corrected["text_etag"] != saved["text_etag"]
    assert corrected["text_etag"] == _etag((page_dir / "text.json").read_bytes())

    # The conflict sibling's derived parts are hashed as well.
    with pytest.raises(PaperConflict) as exc:
        state.paper.save_page(
            nb["path"],
            page_id,
            base_revision=0,
            drawing=b"theirs",
            render=PNG + b"x",
            text={"lines": [], "transcript": "theirs"},
        )
    sibling = exc.value.conflict["sibling"]
    sibling_dir = state.paper.dir / sibling["path"]
    assert sibling["render_etag"] == _etag(PNG + b"x")
    assert sibling["text_etag"] == _etag((sibling_dir / "text.json").read_bytes())


def test_an_ai_reading_is_filed_under_the_render_it_was_made_from(state):
    """The ink moved on after the render; the reading is of the old render
    and must not claim the device OCR is current either."""
    nb = make_notebook(state)
    page_id = "a" * 32
    state.paper.save_page(
        nb["path"],
        page_id,
        base_revision=0,
        drawing=b"ink",
        render=PNG,
        text={"lines": [], "transcript": "ocr"},
    )
    state.paper.save_page(nb["path"], page_id, base_revision=1, drawing=b"more ink")
    before = state.paper.read_page(nb["path"], page_id)
    assert before["revision"] == 2 and before["text_revision"] == 1

    after = state.paper.set_transcript_ai(
        nb["path"], page_id, "AI reading", "test-model", made_for=1
    )

    text = _text_of(state, nb["path"], page_id)
    assert text["transcript_ai"]["revision"] == 1
    assert text["transcript"] == "ocr"
    assert after["revision"] == 2 and after["text_revision"] == 1
    # The new hash is what moves the text URL; the new seq puts it on the feed.
    assert after["text_etag"] != before["text_etag"]
    page_dir = state.paper.dir / nb["path"] / "pages" / page_id
    assert after["text_etag"] == _etag((page_dir / "text.json").read_bytes())
    assert after["seq"] > before["seq"]

    # The OCR for the ink as it is now drops the reading of the old ink.
    state.paper.save_page(
        nb["path"],
        page_id,
        base_revision=2,
        text={"lines": [], "transcript": "fresh"},
        for_revision=2,
    )
    assert "transcript_ai" not in _text_of(state, nb["path"], page_id)


OCR = {"lines": [{"text": "recieve budget"}], "transcript": "recieve budget"}
FIX = [{"before": "recieve budget", "after": "receive budget", "start": 0, "end": 14}]
FRESH = {
    "lines": [{"text": "recieve budgte"}, {"text": "dentist tuesday"}],
    "transcript": "recieve budgte\ndentist tuesday",
}


def test_a_correction_stays_with_the_ink_it_was_made_for(state):
    """The sequence an iPad build without for_revision sends: correct, write
    more, read again."""
    nb = make_notebook(state)
    page_id = "a" * 32
    path = nb["path"]
    state.paper.save_page(path, page_id, base_revision=0, drawing=b"ink")
    corrected = {
        **OCR,
        "corrected_transcript": "receive budget",
        "corrections": FIX,
        "corrections_revision": 1,
    }
    state.paper.save_page(path, page_id, base_revision=1, text=corrected, for_revision=1)
    text = _text_of(state, path, page_id)
    assert text["corrections_revision"] == 1
    assert PaperStore.effective_transcript(text, 1) == "receive budget"

    # New ink goes up with the old text.json still attached.
    state.paper.save_page(
        path, page_id, base_revision=1, drawing=b"more ink", text=corrected, for_revision=2
    )
    text = _text_of(state, path, page_id)
    assert text["corrections_revision"] == 1
    assert PaperStore.effective_transcript(text, 2) == "recieve budget"

    # The fresh reading says nothing about corrections: the old one is set
    # aside rather than kept live or dropped.
    state.paper.save_page(path, page_id, base_revision=2, text=FRESH, for_revision=2)
    text = _text_of(state, path, page_id)
    assert "corrected_transcript" not in text and "corrections" not in text
    assert text["stale_correction"] == {
        "corrected_transcript": "receive budget",
        "corrections": FIX,
        "corrections_revision": 1,
    }
    assert text["for_revision"] == 2
    assert PaperStore.effective_transcript(text, 2) == FRESH["transcript"]
    state.paper.save_page(path, page_id, base_revision=2, text=FRESH, for_revision=2)
    assert _text_of(state, path, page_id)["stale_correction"]["corrections_revision"] == 1

    # "Use original" says so explicitly, and clears both.
    state.paper.save_page(
        path,
        page_id,
        base_revision=2,
        text={**FRESH, "corrected_transcript": None, "corrections": []},
        for_revision=2,
    )
    text = _text_of(state, path, page_id)
    for key in ("corrected_transcript", "corrections", "corrections_revision", "stale_correction"):
        assert key not in text


def test_use_original_clears_a_correction_of_the_same_ink(state):
    nb = make_notebook(state)
    page_id = "a" * 32
    state.paper.save_page(nb["path"], page_id, base_revision=0, drawing=b"ink")
    state.paper.save_page(
        nb["path"],
        page_id,
        base_revision=1,
        text={**OCR, "corrected_transcript": "receive budget", "corrections_revision": 1},
        for_revision=1,
    )
    state.paper.save_page(
        nb["path"],
        page_id,
        base_revision=1,
        text={**OCR, "corrected_transcript": None, "corrections": []},
        for_revision=1,
    )
    text = _text_of(state, nb["path"], page_id)
    assert "corrected_transcript" not in text
    assert PaperStore.effective_transcript(text, 1) == "recieve budget"


def test_a_correction_of_the_reading_it_travels_with_follows_that_reading(state):
    """What the iPad sends now: the reading names its ink, the correction is
    no older, so a metadata save that files the text at the next revision
    files the correction there too -- and a stale one rides along untouched."""
    nb = make_notebook(state)
    page_id = "a" * 32
    path = nb["path"]
    state.paper.save_page(path, page_id, base_revision=0, drawing=b"ink")
    text = {
        **OCR,
        "for_revision": 1,
        "corrected_transcript": "receive budget",
        "corrections": FIX,
        "corrections_revision": 1,
    }
    state.paper.save_page(path, page_id, base_revision=1, text=text, for_revision=1)
    state.paper.save_page(
        path,
        page_id,
        base_revision=1,
        page={"title": "Budget"},
        drawing=b"ink",
        text=text,
        for_revision=2,
    )
    stored = _text_of(state, path, page_id)
    assert stored["corrections_revision"] == 2
    assert PaperStore.effective_transcript(stored, 2) == "receive budget"

    stale = {"corrected_transcript": "receive budget", "corrections": FIX, "corrections_revision": 2}
    state.paper.save_page(
        path,
        page_id,
        base_revision=3,
        text={
            **FRESH,
            "for_revision": 3,
            "corrected_transcript": None,
            "corrections": [],
            "stale_correction": stale,
        },
        for_revision=3,
    )
    stored = _text_of(state, path, page_id)
    assert "corrected_transcript" not in stored
    assert stored["stale_correction"] == stale
    assert PaperStore.effective_transcript(stored, 3) == FRESH["transcript"]


# -- pages[] reconciliation --------------------------------------------------


def test_a_pages_patch_never_drops_a_page_that_exists_on_disk(state):
    nb = make_notebook(state)
    for letter in "abc":
        state.paper.create_page(nb["path"], page_id=letter * 32)

    # A client that never saw page "c" reorders the two it knows about.
    patched = state.paper.update_notebook(
        nb["path"], {"pages": ["b" * 32, "a" * 32, "ghost"]}
    )
    assert patched["pages"] == ["b" * 32, "a" * 32, "c" * 32]


def test_page_metadata_and_stable_url_survive_a_notebook_rename(state):
    nb = make_notebook(state, "Ideas", notebook_id="notebook-stable")
    page_id = "a" * 32
    saved = state.paper.save_page(
        nb["path"],
        page_id,
        base_revision=0,
        page={"title": "Lecture 1", "bookmarked": True},
        drawing=b"ink",
    )
    assert saved["page"]["title"] == "Lecture 1"
    assert saved["page"]["bookmarked"] is True
    assert state.paper.page_public(nb["path"], page_id)["durable_url"] == (
        "/paper/id/notebook-stable/" + page_id
    )
    moved = state.paper.rename_notebook(nb["path"], "paper/Archive/Renamed.paper")
    page = state.paper.page_public(moved["path"], page_id)
    assert page["notebook_id"] == "notebook-stable"
    assert page["durable_url"] == "/paper/id/notebook-stable/" + page_id


def test_page_url_percent_encodes_the_notebook_path():
    """Notebook directories keep spaces and parentheses (the iPad's first one
    is "Notebook 1.paper"); a raw space in a markdown image destination
    renders as literal text."""
    page_id = "a" * 32
    url = page_url(
        f"paper/School (old)/Notebook 1.paper/pages/{page_id}", "render.png", 3, "abc"
    )
    assert url == (
        f"/api/paper/blob/paper/School%20%28old%29/Notebook%201.paper/{page_id}"
        "/render.png?r=3&v=abc"
    )
    # A plain name is unchanged, so existing URLs keep their exact form.
    assert page_url(f"paper/Ideas.paper/pages/{page_id}", "render.png", None) == (
        f"/api/paper/blob/paper/Ideas.paper/{page_id}/render.png"
    )


def test_effective_transcript_prefers_corrections_but_keeps_raw_ocr():
    text = {
        "transcript": "recieve budget",
        "corrections": [{"offset": 0, "end": 7, "after": "receive"}],
    }
    assert PaperStore.effective_transcript(text) == "receive budget"
    text["corrected_transcript"] = "receive plan"
    assert PaperStore.effective_transcript(text) == "receive plan"


def test_notebook_patch_only_touches_whitelisted_fields(state):
    nb = make_notebook(state)
    patched = state.paper.update_notebook(
        nb["path"],
        {"title": "Renamed", "favorite": True, "tags": ["x"], "id": "hacked"},
    )
    assert patched["title"] == "Renamed" and patched["favorite"] is True
    assert patched["id"] == nb["id"]


# -- delete / trash ----------------------------------------------------------


def test_deleting_a_page_trashes_it_and_restore_puts_it_back_in_place(state):
    nb = make_notebook(state)
    for letter in "abc":
        state.paper.create_page(nb["path"], page_id=letter * 32)

    item = state.paper.delete_page(nb["path"], "b" * 32)
    assert item["kind"] == "page" and item["position"] == 1
    assert state.paper.read_notebook(nb["path"])["pages"] == ["a" * 32, "c" * 32]
    assert state.trash.preview(item["id"])["paths"] == []

    restored = state.paper.restore_trash(item["id"])
    assert restored["kind"] == "page"
    assert state.paper.read_notebook(nb["path"])["pages"] == [
        "a" * 32,
        "b" * 32,
        "c" * 32,
    ]


def test_deleting_the_cover_page_moves_the_cover(state):
    nb = make_notebook(state)
    state.paper.create_page(nb["path"], page_id="a" * 32)
    state.paper.create_page(nb["path"], page_id="b" * 32)
    state.paper.delete_page(nb["path"], "a" * 32)
    assert state.paper.read_notebook(nb["path"])["cover"] == "b" * 32


def test_a_restored_page_follows_its_renamed_notebook(state):
    """Delete a page, retitle the notebook, restore the page: it lands in the
    notebook it belongs to, not in a ghost directory at the old path."""
    nb = make_notebook(state, "Ideas", folder="Work")
    for letter in "abc":
        state.paper.create_page(nb["path"], page_id=letter * 32)
    item = state.paper.delete_page(nb["path"], "b" * 32)
    assert item["notebook_id"] == nb["id"]
    moved = state.paper.rename_notebook(nb["path"], "paper/Work/Brainstorm.paper")

    restored = state.paper.restore_trash(item["id"])
    assert restored["notebook"] == moved["path"]
    assert restored["path"] == f"{moved['path']}/pages/{'b' * 32}"
    assert state.paper.read_notebook(moved["path"])["pages"] == [
        "a" * 32,
        "b" * 32,
        "c" * 32,
    ]
    assert not (state.settings.vault_dir / nb["path"]).exists()
    # The old title is free again, so renaming back works.
    assert state.paper.rename_notebook(moved["path"], nb["path"])["path"] == nb["path"]


def test_a_page_whose_notebook_is_in_the_trash_waits_for_it(state):
    """Restoring the page first must refuse before anything moves, or the
    page squats on the path the notebook's own restore needs."""
    nb = make_notebook(state, "Ideas", folder="Work")
    state.paper.create_page(nb["path"], page_id="a" * 32)
    state.paper.create_page(nb["path"], page_id="b" * 32)
    page_item = state.paper.delete_page(nb["path"], "b" * 32)
    notebook_item = state.paper.delete_notebook(nb["path"])

    with pytest.raises(NoteError) as exc:
        state.paper.restore_trash(page_item["id"])
    assert exc.value.status == 409
    assert {item["id"] for item in state.trash.list()} == {
        page_item["id"],
        notebook_item["id"],
    }
    assert not (state.settings.vault_dir / nb["path"]).exists()

    state.paper.restore_trash(notebook_item["id"])
    state.paper.restore_trash(page_item["id"])
    assert state.paper.read_notebook(nb["path"])["pages"] == ["a" * 32, "b" * 32]
    assert state.trash.list() == []


def test_a_page_is_not_restored_into_a_new_notebook_at_its_old_path(state):
    """The page's own notebook is in the Trash; an unrelated notebook that
    took its name must not receive the page, or block that notebook's own
    restore."""
    nb = make_notebook(state, "Ideas")
    state.paper.create_page(nb["path"], page_id="a" * 32)
    page_item = state.paper.delete_page(nb["path"], "a" * 32)
    notebook_item = state.paper.delete_notebook(nb["path"])
    newcomer = make_notebook(state, "Ideas")
    assert newcomer["path"] == nb["path"] and newcomer["id"] != nb["id"]

    with pytest.raises(NoteError) as exc:
        state.paper.restore_trash(page_item["id"])
    assert exc.value.status == 409
    assert state.paper.read_notebook(newcomer["path"])["pages"] == []
    assert page_item["id"] in {item["id"] for item in state.trash.list()}

    # Once the newcomer moves aside, both come back where they belong.
    state.paper.rename_notebook(newcomer["path"], "paper/Other.paper")
    state.paper.restore_trash(notebook_item["id"])
    state.paper.restore_trash(page_item["id"])
    assert state.paper.read_notebook(nb["path"])["pages"] == ["a" * 32]
    assert state.paper.read_notebook("paper/Other.paper")["pages"] == []


def test_a_trash_entry_from_before_notebook_ids_still_restores(state):
    nb = make_notebook(state)
    state.paper.create_page(nb["path"], page_id="a" * 32)
    item = state.paper.delete_page(nb["path"], "a" * 32)
    manifest_file = state.trash.root / item["id"] / "manifest.json"
    legacy = json.loads(manifest_file.read_text())
    del legacy["notebook_id"]
    manifest_file.write_text(json.dumps(legacy))

    restored = state.paper.restore_trash(item["id"])
    assert restored["notebook"] == nb["path"]
    assert state.paper.read_notebook(nb["path"])["pages"] == ["a" * 32]


def test_a_restore_gives_everything_it_brings_back_a_fresh_seq(state):
    """The feed selects by seq; a restored file keeping its old one is
    invisible to every client whose cursor already passed it."""
    nb = make_notebook(state)
    state.paper.save_page(nb["path"], "a" * 32, base_revision=0, drawing=b"ink")
    state.paper.create_page(nb["path"], page_id="b" * 32)
    item = state.paper.delete_page(nb["path"], "b" * 32)
    cursor = state.paper.seq_state()["seq"]

    state.paper.restore_trash(item["id"])
    page = state.paper.read_page(nb["path"], "b" * 32)
    assert page["seq"] > cursor and page["revision"] == 1

    cursor = state.paper.seq_state()["seq"]
    notebook_item = state.paper.delete_notebook(nb["path"])
    state.paper.restore_trash(notebook_item["id"])
    assert state.paper.read_notebook(nb["path"])["seq"] > cursor
    for page_id in ("a" * 32, "b" * 32):
        assert state.paper.read_page(nb["path"], page_id)["seq"] > cursor
    # The ink's revision belongs to the ink, not to the feed.
    assert state.paper.read_page(nb["path"], "a" * 32)["revision"] == 1


def test_a_rename_restamps_the_pages_it_moved(state):
    nb = make_notebook(state)
    before = state.paper.create_page(nb["path"], page_id="a" * 32)
    cursor = state.paper.seq_state()["seq"]
    moved = state.paper.rename_notebook(nb["path"], "paper/Renamed.paper")
    page = state.paper.read_page(moved["path"], "a" * 32)
    assert page["seq"] > cursor and page["revision"] == 1
    # The device that asked for the move learns the new seqs from the answer,
    # so the feed's echo of its own move is nothing new to it.
    assert moved["moved_pages"] == [
        {"id": "a" * 32, "seq": page["seq"], "previous_seq": before["seq"]}
    ]
    assert "moved_pages" not in state.paper.read_notebook(moved["path"])


def test_a_restamp_leaves_modified_alone(state):
    """The iPad's automatic cover is the latest-modified page: a rename or a
    restore renumbering every page must not reorder them by page id."""
    nb = make_notebook(state)
    for page_id in ("b" * 32, "a" * 32):
        state.paper.save_page(nb["path"], page_id, base_revision=0, drawing=b"ink")
    pages_dir = state.settings.vault_dir / nb["path"] / "pages"
    stamps = {}
    for page_id, stamp in (("a" * 32, "2026-01-01T00:00:00Z"), ("b" * 32, "2026-02-01T00:00:00Z")):
        raw = json.loads((pages_dir / page_id / "page.json").read_text())
        raw["modified"] = stamps[page_id] = stamp
        (pages_dir / page_id / "page.json").write_text(json.dumps(raw))

    moved = state.paper.rename_notebook(nb["path"], "paper/Moved.paper")
    for page_id, stamp in stamps.items():
        assert state.paper.read_page(moved["path"], page_id)["modified"] == stamp

    item = state.paper.delete_page(moved["path"], "a" * 32)
    state.paper.restore_trash(item["id"])
    assert state.paper.read_page(moved["path"], "a" * 32)["modified"] == stamps["a" * 32]


# -- sequence + tombstones ---------------------------------------------------


def test_seq_is_monotonic_across_every_write(state):
    nb = make_notebook(state)
    page = state.paper.create_page(nb["path"], page_id="a" * 32)
    later = state.paper.save_page(
        nb["path"], "a" * 32, base_revision=1, drawing=b"ink"
    )
    assert nb["seq"] < page["seq"] < later["page"]["seq"]
    assert state.paper.seq_state()["seq"] >= later["page"]["seq"]


def test_tombstones_are_pruned_and_leave_a_watermark(state):
    nb = make_notebook(state)
    state.paper.create_page(nb["path"], page_id="a" * 32)
    state.paper.delete_page(nb["path"], "a" * 32)
    assert len(state.paper.tombstones()) == 1

    assert state.paper.prune_tombstones(days=60) == 0
    assert state.paper.prune_tombstones(days=0) == 1
    assert state.paper.tombstones() == []
    assert state.paper.seq_state()["pruned"] > 0


def test_recreating_a_deleted_page_clears_its_tombstone(state):
    nb = make_notebook(state)
    state.paper.create_page(nb["path"], page_id="a" * 32)
    state.paper.delete_page(nb["path"], "a" * 32)
    state.paper.create_page(nb["path"], page_id="a" * 32)
    assert state.paper.tombstones() == []


def test_reusing_a_notebook_path_clears_its_tombstone(state):
    nb = make_notebook(state, "Ideas")
    state.paper.delete_notebook(nb["path"])
    again = make_notebook(state, "Ideas")
    assert again["path"] == nb["path"]
    assert state.paper.tombstones() == []

    other = make_notebook(state, "Alpha")
    state.paper.rename_notebook(other["path"], "paper/Beta.paper")
    state.paper.rename_notebook("paper/Beta.paper", other["path"])
    assert [row["path"] for row in state.paper.tombstones()] == ["paper/Beta.paper"]


# -- assets ------------------------------------------------------------------


def test_assets_are_content_hashed_and_idempotent(state):
    nb = make_notebook(state)
    first = state.paper.store_asset(nb["path"], "Holiday Photo.PNG", PNG)
    second = state.paper.store_asset(nb["path"], "different-name.png", PNG)
    assert first["name"].startswith("holiday-photo-")
    assert first["name"].split("-")[-1] == second["name"].split("-")[-1]
    assert first["url"] == f"/api/paper/assets/{nb['path']}/{first['name']}"
    assert state.paper.resolve_asset(nb["path"], first["name"]).read_bytes() == PNG


def test_a_failed_asset_write_leaves_nothing_under_the_hashed_name(
    state, monkeypatch
):
    """The name is skipped once it exists, so a torn file there would be
    served forever."""
    nb = make_notebook(state)

    def no_space(_src, _dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", no_space)
    with pytest.raises(OSError):
        state.paper.store_asset(nb["path"], "scan.png", PNG)
    monkeypatch.undo()
    assets = state.settings.vault_dir / nb["path"] / "assets"
    assert list(assets.iterdir()) == []

    # A torn file left by an earlier write is replaced, not trusted.
    asset = state.paper.store_asset(nb["path"], "scan.png", PNG)
    path = state.paper.resolve_asset(nb["path"], asset["name"])
    path.write_bytes(PNG[:5])
    state.paper.store_asset(nb["path"], "scan.png", PNG)
    assert path.read_bytes() == PNG


def test_assets_reject_odd_types_and_traversal(state):
    nb = make_notebook(state)
    with pytest.raises(NoteError):
        state.paper.store_asset(nb["path"], "payload.svg", b"<svg/>")
    with pytest.raises(NoteError):
        state.paper.resolve_asset(nb["path"], "../notebook.json")


def test_size_caps_are_enforced_in_the_store_too(state, monkeypatch):
    from app import paper_store

    monkeypatch.setattr(paper_store, "MAX_DRAWING_BYTES", 8)
    nb = make_notebook(state)
    with pytest.raises(NoteError) as exc:
        state.paper.save_page(
            nb["path"], "a" * 32, base_revision=0, drawing=b"far too much ink"
        )
    assert exc.value.status == 413


# -- write durability ---------------------------------------------------------


def test_a_refused_save_writes_nothing(state, monkeypatch):
    """The merged transcript is checked last; it used to fail after the new
    ink had already replaced the old under an unchanged revision."""
    from app import paper_store

    nb = make_notebook(state)
    page_id = "a" * 32
    state.paper.save_page(nb["path"], page_id, base_revision=0, drawing=b"ink-A")
    monkeypatch.setattr(paper_store, "MAX_TEXT_BYTES", 64)

    with pytest.raises(NoteError) as exc:
        state.paper.save_page(
            nb["path"],
            page_id,
            base_revision=1,
            drawing=b"ink-B",
            render=PNG,
            text={"lines": [], "transcript": "x" * 500},
        )
    assert exc.value.status == 413
    page_dir = state.paper.resolve_page(nb["path"], page_id)
    assert (page_dir / "drawing.pkdrawing").read_bytes() == b"ink-A"
    assert state.paper.read_page(nb["path"], page_id)["revision"] == 1
    assert sorted(p.name for p in page_dir.iterdir()) == [
        "drawing.pkdrawing",
        "page.json",
    ]

    # A refused create leaves no empty page directory for a later pages
    # reconcile to list.
    with pytest.raises(NoteError):
        state.paper.save_page(
            nb["path"],
            "b" * 32,
            base_revision=0,
            drawing=b"ink",
            text={"lines": [], "transcript": "x" * 500},
        )
    assert _page_dirs(state, nb) == [page_id]
    # Nor does a refused conflict sibling.
    with pytest.raises(NoteError) as exc:
        state.paper.save_page(
            nb["path"],
            page_id,
            base_revision=0,
            page={"size_points": {"width": "wide"}},
            drawing=b"ink-C",
        )
    assert exc.value.status == 400
    assert _page_dirs(state, nb) == [page_id]
    assert state.paper.read_notebook(nb["path"])["pages"] == [page_id]


def test_a_failed_ink_write_leaves_the_old_ink_and_revision(state, monkeypatch):
    nb = make_notebook(state)
    page_id = "a" * 32
    state.paper.save_page(nb["path"], page_id, base_revision=0, drawing=b"ink-A")

    def no_space(_src, _dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", no_space)
    with pytest.raises(OSError):
        state.paper.save_page(nb["path"], page_id, base_revision=1, drawing=b"ink-B")
    monkeypatch.undo()

    page_dir = state.paper.resolve_page(nb["path"], page_id)
    assert (page_dir / "drawing.pkdrawing").read_bytes() == b"ink-A"
    assert state.paper.read_page(nb["path"], page_id)["revision"] == 1
    assert not [p.name for p in page_dir.iterdir() if p.name.endswith(".tmp")]


def test_atomic_bytes_replaces_through_a_temp_it_cleans_up(tmp_path):
    target = tmp_path / "blob.bin"
    internal.atomic_bytes(target, b"one")
    internal.atomic_bytes(target, b"two")
    assert target.read_bytes() == b"two"
    assert [p.name for p in tmp_path.iterdir()] == ["blob.bin"]
    # Same mode an ordinary write would give it, not the 0600 of a mkstemp.
    umask = os.umask(0)
    os.umask(umask)
    assert target.stat().st_mode & 0o777 == 0o666 & ~umask


def test_json_stores_are_fsynced_and_keep_their_mode(tmp_path, monkeypatch):
    """Threads, tasks, the Trash and seq.json all go through atomic_json; a
    power cut right after a write must not bring any of them back empty."""
    synced = []
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(fd), real_fsync(fd)))
    target = tmp_path / "store.json"
    internal.atomic_json(target, {"n": 1})
    assert len(synced) == 1
    os.chmod(target, 0o600)
    internal.atomic_json(target, {"n": 2})
    assert len(synced) == 2
    assert target.stat().st_mode & 0o777 == 0o600
    assert json.loads(target.read_text()) == {"n": 2}
    internal.atomic_json(target, {"n": 3}, fsync=False)
    assert len(synced) == 2


def test_two_json_writers_of_one_path_do_not_share_a_temp(tmp_path, monkeypatch):
    """With the old fixed ``<name>.tmp`` the second writer took over the first
    one's temp file, and the first one's replace then failed."""
    target = tmp_path / "notebook.json"
    temps = []
    real_replace = os.replace

    def interleave(src, dst):
        temps.append(os.path.basename(src))
        if len(temps) == 1:
            # A second writer lands while the first one's temp still exists.
            internal.atomic_json(target, {"writer": 2})
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", interleave)
    internal.atomic_json(target, {"writer": 1})
    assert len(temps) == 2 and temps[0] != temps[1]
    assert all(name.startswith(".notebook.json.") for name in temps)
    assert json.loads(target.read_text()) == {"writer": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["notebook.json"]


# -- git-facing ---------------------------------------------------------------


def test_gitattributes_appends_the_paper_lines_and_is_idempotent(state):
    path = state.settings.vault_dir / ".gitattributes"
    state.vault._ensure_gitattributes()
    first = path.read_text()
    state.vault._ensure_gitattributes()
    assert path.read_text() == first
    assert "attachments/** binary" in first
    assert "paper/**/*.pkdrawing binary" in first
    assert "paper/**/*.png binary" in first
    # notebook.json and page.json must keep their textual diffs.
    assert "paper/** binary" not in first


def test_gitattributes_upgrades_a_vault_that_predates_paper(state):
    path = state.settings.vault_dir / ".gitattributes"
    path.write_text("attachments/** binary\n", encoding="utf-8")
    state.vault._ensure_gitattributes()
    lines = path.read_text().splitlines()
    assert lines[0] == "attachments/** binary"
    assert "paper/**/*.pkdrawing binary" in lines
    assert lines.count("attachments/** binary") == 1


def test_restore_page_version_is_a_new_revision_not_a_rewrite(state):
    nb = make_notebook(state)
    page_id = "a" * 32
    state.paper.save_page(nb["path"], page_id, base_revision=0, drawing=b"first")
    asyncio.run(state.vault.commit_all("paper: first"))
    state.paper.save_page(nb["path"], page_id, base_revision=1, drawing=b"second")
    asyncio.run(state.vault.commit_all("paper: second"))

    page_dir = f"{nb['path']}/pages/{page_id}"
    versions = state.vault.dir_history_sync(page_dir)
    assert len(versions) == 2
    oldest = versions[-1]["sha"]

    restored = state.paper.restore_page_version(state.vault, nb["path"], page_id, oldest)
    assert restored["revision"] == 3
    blob, _ = state.paper.read_blob(nb["path"], page_id, "drawing.pkdrawing")
    assert blob.read_bytes() == b"first"
    # Nothing was rewritten: the two commits are still there, unchanged.
    assert [v["sha"] for v in state.vault.dir_history_sync(page_dir)] == [
        v["sha"] for v in versions
    ]


def test_paper_is_not_a_note_folder(state):
    nb = make_notebook(state, folder="Work")
    state.paper.create_page(nb["path"], page_id="a" * 32)
    assert all(not path.startswith("paper") for path in state.store.list_folders())
    assert all(not note.path.startswith("paper") for note in state.store.list_notes())


def test_size_points_round_trips_and_is_validated(state):
    nb = make_notebook(state)
    page_id = "s" * 32
    saved = state.paper.save_page(
        nb["path"], page_id, base_revision=0,
        page={"size_points": {"width": 400, "height": 400.5}},
    )
    assert saved["page"]["size_points"] == {"width": 400.0, "height": 400.5}
    assert state.paper.read_page(nb["path"], page_id)["size_points"] == {
        "width": 400.0, "height": 400.5,
    }
    with pytest.raises(NoteError):
        state.paper.save_page(
            nb["path"], page_id, base_revision=saved["revision"],
            page={"size_points": {"width": "wide"}},
        )


def test_size_name_round_trips_beside_the_extent(state):
    # A grown infinite page and a tall PDF page can measure the same, and an
    # unused infinite page measures exactly like an iPad page: only the name
    # tells a fresh pull which one it is holding.
    nb = make_notebook(state, paper_size="infinite")
    page_id = "n" * 32
    saved = state.paper.save_page(
        nb["path"], page_id, base_revision=0,
        page={
            "size": {"width": 1024, "height": 2732},
            "size_name": "infinite",
            "size_points": {"width": 1024, "height": 2732},
        },
    )
    page = state.paper.read_page(nb["path"], page_id)
    assert page["size_name"] == "infinite"
    assert page["size_points"] == {"width": 1024.0, "height": 2732.0}
    # The dimensions stay what the web viewer reads.
    assert page["size"] == {"width": 1024.0, "height": 2732.0}

    # Matched like paperSize: case-insensitively, anything unknown a 400.
    again = state.paper.save_page(
        nb["path"], page_id, base_revision=saved["revision"],
        page={"size_name": "IPAD"},
    )
    assert again["page"]["size_name"] == "iPad"
    with pytest.raises(NoteError):
        state.paper.save_page(
            nb["path"], page_id, base_revision=again["revision"],
            page={"size_name": "tabloid"},
        )
    # A string size names the preset too.
    named = state.paper.save_page(
        nb["path"], page_id, base_revision=again["revision"], page={"size": "letter"},
    )
    assert named["page"]["size_name"] == "letter"
    assert named["page"]["size"] == {"width": 612.0, "height": 792.0}


def test_create_page_records_the_size_name(state):
    nb = make_notebook(state, paper_size="infinite")
    grown = state.paper.create_page(
        nb["path"], page_id="a" * 32,
        size={"width": 1024, "height": 2000}, size_name="infinite",
    )
    assert grown["size_name"] == "infinite"
    assert grown["size"] == {"width": 1024.0, "height": 2000.0}
    assert state.paper.create_page(
        nb["path"], page_id="b" * 32, size="letter"
    )["size_name"] == "letter"
    # No size at all: the page is the notebook's preset, and says so.
    assert state.paper.create_page(nb["path"], page_id="c" * 32)["size_name"] == "infinite"
    # Only dimensions: nothing to name.
    assert "size_name" not in state.paper.create_page(
        nb["path"], page_id="d" * 32, size={"width": 300, "height": 300}
    )
    with pytest.raises(NoteError):
        state.paper.create_page(nb["path"], page_id="e" * 32, size_name="tabloid")
