import asyncio

import pytest

from app.vault import fm
from app.vault.internal import TrashStore
from app.vault.notes import NoteError, NoteStore


def test_ordinary_note_title_always_matches_filename(tmp_path):
    store = NoteStore(tmp_path)
    original = store.create(
        "old name.md",
        "---\ntitle: mismatched\npinned: true\n---\n\nKeep this body.\n",
    )
    original_meta, _ = fm.parse(original)
    assert original_meta["title"] == "old name"

    updated = store.write(
        "old name.md",
        "---\ntitle: another mismatch\npinned: true\n---\n\nKeep this body.\n",
    )
    meta, body = fm.parse(updated)

    assert meta["title"] == "old name"
    assert meta["pinned"] is True
    assert meta["created"] == original_meta["created"]
    assert body == "Keep this body.\n"

    store.rename("old name.md", "New name.md")
    renamed_meta, renamed_body = fm.parse(store.read("New name.md"))
    assert renamed_meta["title"] == "New name"
    assert renamed_body == "Keep this body.\n"

    with pytest.raises(NoteError):
        store.set_title("New name.md", "Different title")


def test_app_managed_titles_keep_placeholders(tmp_path):
    store = NoteStore(tmp_path)
    content = store.create(
        "templates/note.md",
        '---\ntitle: "{{title}}"\n---\n\nTemplate body.\n',
    )
    meta, _ = fm.parse(content)
    assert meta["title"] == "{{title}}"


@pytest.mark.parametrize(
    "body",
    [
        "\tindented first line\n\t\tindented second line\n",
        "line before\n\t\n\tindented after blank line\n",
        "trailing spaces stay   \nfinal tab stays\t",
        "\n\tintentional leading blank line\n",
        "\t",
    ],
)
def test_note_save_preserves_body_whitespace_exactly(tmp_path, body):
    store = NoteStore(tmp_path)
    created = store.create(
        "Whitespace.md",
        f"---\ntitle: Whitespace\n---\n\n{body}",
    )
    assert fm.parse(created)[1] == body

    saved = store.write("Whitespace.md", created)
    assert fm.parse(saved)[1] == body


@pytest.mark.parametrize(
    "bad",
    [
        "../x.md",
        "a/../../x.md",
        "/etc/x.md",
        ".git/hooks/x.md",
        "a/.git/x.md",
        ".exscalia/config.md",
        "a/.exscalia/x.md",
        "x.txt",
        "x",
        "paper/x.md",
    ],
)
def test_note_paths_reject_escapes_and_internal_dirs(tmp_path, bad):
    # resolve() guards every note route and every OMNIA tool path argument,
    # and tool arguments are model-controlled.
    store = NoteStore(tmp_path)
    with pytest.raises(NoteError) as exc:
        store.resolve(bad)
    assert exc.value.status == 400


def test_note_paths_cannot_escape_through_a_symlink(tmp_path):
    # resolve() follows links before its containment check, so a link inside
    # the vault that points out of it is refused, file or directory alike.
    vault = tmp_path / "vault"
    vault.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.md").write_text("outside", encoding="utf-8")
    (vault / "link").symlink_to(outside, target_is_directory=True)
    (vault / "note.md").symlink_to(outside / "x.md")
    store = NoteStore(vault)
    for linked in ("link/x.md", "note.md"):
        with pytest.raises(NoteError) as exc:
            store.resolve(linked)
        assert exc.value.status == 400


@pytest.mark.parametrize("good", ["a/b c.md", "Café.md", "School/paper/x.md"])
def test_note_paths_accept_ordinary_names(tmp_path, good):
    store = NoteStore(tmp_path)
    assert store.resolve(good) == (tmp_path / good).resolve()


def test_attachment_paths_reject_traversal_and_bad_types(tmp_path):
    store = NoteStore(tmp_path)

    # Happy path resolves inside the attachments folder.
    resolved = store.resolve_attachment("attachments/diagram-abc123.png")
    assert resolved.is_relative_to(tmp_path / "attachments")

    # Escaping the attachments folder, in any form, is rejected.
    for bad in (
        "attachments/../../etc/passwd.png",
        "../outside.png",
        "/absolute/path.png",
        ".git/config.png",
        ".exscalia/secret.png",
    ):
        with pytest.raises(NoteError):
            store.resolve_attachment(bad)

    # Must live under attachments/, and only known media types are allowed.
    with pytest.raises(NoteError):
        store.resolve_attachment("notes/image.png")
    for bad_type in ("attachments/payload.exe", "attachments/script.js", "attachments/noext"):
        with pytest.raises(NoteError):
            store.resolve_attachment(bad_type)


def test_attachments_never_appear_as_notes_or_folders(tmp_path):
    store = NoteStore(tmp_path)
    store.create("Real Note.md", "---\ntitle: Real Note\n---\n\nBody.\n")
    target = store.resolve_attachment("attachments/pasted-deadbeef.png")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\x89PNG\r\n\x1a\n")

    assert [n.path for n in store.list_notes()] == ["Real Note.md"]
    assert "attachments" not in store.list_folders()


def test_paper_is_pruned_only_at_the_vault_root(tmp_path):
    store = NoteStore(tmp_path)
    store.create("School/paper/thesis.md", "---\ntitle: thesis\n---\n\nDraft.\n")
    store.create("School/other.md", "---\ntitle: other\n---\n\nBody.\n")
    # The root paper/ holds Paper packages, never notes or note folders.
    package = tmp_path / "paper" / "Loose.paper"
    package.mkdir(parents=True)
    (package / "page.json").write_text("{}", encoding="utf-8")
    (package / "Stray.md").write_text("stray", encoding="utf-8")
    # .git and the internal dir stay pruned at every depth.
    for hidden in (".git", ".exscalia"):
        (tmp_path / "School" / hidden).mkdir()
        (tmp_path / "School" / hidden / "x.md").write_text("x", encoding="utf-8")

    assert [n.path for n in store.list_notes()] == [
        "School/other.md",
        "School/paper/thesis.md",
    ]
    assert store.list_folders() == ["School", "School/paper"]


def test_root_paper_is_reserved_for_notes_and_folders(tmp_path):
    store = NoteStore(tmp_path)
    for attempt in (
        lambda: store.create("paper/x.md"),
        lambda: store.create("paper/Work/x.md"),
        lambda: store.resolve_folder("paper"),
        lambda: store.create_folder("paper/Work"),
    ):
        with pytest.raises(NoteError) as exc:
            attempt()
        assert exc.value.status == 400
    assert not (tmp_path / "paper").exists()
    assert not store.exists("paper/x.md")

    # Only the exact root segment is reserved.
    store.create("School/paper/x.md")
    store.create_folder("School/paper/Drafts")
    assert "School/paper/x.md" in [n.path for n in store.list_notes()]
    assert "School/paper/Drafts" in store.list_folders()

    # Moves and Trash restores into the root paper/ are refused too, instead of
    # landing where no listing would ever show them.
    with pytest.raises(NoteError):
        store.rename("School/paper/x.md", "paper/x.md")
    with pytest.raises(NoteError):
        store.rename_folder("School/paper", "paper/School")
    trash = TrashStore(tmp_path, store)
    item = trash.trash_note("School/paper/x.md")
    with pytest.raises(NoteError):
        trash.restore(item["id"], target="paper/x.md")
    assert trash.restore(item["id"])["path"] == "School/paper/x.md"


def test_boot_sync_keeps_notes_in_a_nested_paper_folder(state):
    state.store.create("School/paper/thesis.md", "---\ntitle: thesis\n---\n\nDraft.\n")

    async def run():
        await state.indexer.sync_all()
        # A second boot must not drop it as "gone from disk".
        await state.indexer.sync_all()
        return await state.db.execute_fetchall(
            "SELECT path FROM notes WHERE path = ?", ("School/paper/thesis.md",)
        )

    assert len(asyncio.run(run())) == 1
