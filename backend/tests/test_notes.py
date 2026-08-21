import pytest

from app.vault import fm
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
