"""Frontmatter that does not load must never 500 a read or crash-loop boot.

Reads degrade to "no frontmatter" so the note still opens; paths that write
the block back refuse with a 422 instead of burying it under a fresh one.
Line handling splits on real newlines only, so a save never restructures text.
"""
import asyncio
import json
from datetime import date
from types import SimpleNamespace

import pytest

from app.api import notes as notes_api
from app.index import parse
from app.index.db import content_hash
from app.index.indexer import Indexer
from app.overseer.tools import execute_tool
from app.task_service import create_task, delete_task
from app.vault import fm
from app.vault.notes import NoteError, NoteStore

UNLOADABLE = [
    "title: Notes: part 2",
    "title: {{title}}",
    "created: 2026-13-45",
    "title: a: b",
]


def request_for(state):
    return SimpleNamespace(app=SimpleNamespace(state=state))


def put(path: str, content: str, store: NoteStore) -> None:
    """Write a file the way an import, git restore or hand edit would."""
    p = store.dir / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


@pytest.mark.parametrize("yaml_line", UNLOADABLE)
def test_parse_degrades_on_unloadable_yaml_and_strict_refuses(yaml_line):
    content = f"---\n{yaml_line}\n---\n\nbody\n"
    assert fm.parse(content) == ({}, content)
    with pytest.raises(fm.FrontmatterError):
        fm.parse(content, strict=True)
    with pytest.raises(fm.FrontmatterError):
        fm.prepare_for_save(content, None, "Note")


def test_strict_error_names_the_file_line():
    content = "---\ntitle: fine\nsummary: a: b\n---\n\nbody\n"
    with pytest.raises(fm.FrontmatterError) as err:
        fm.parse(content, strict=True)
    assert err.value.line == 3
    assert "line 3" in str(err.value)


def test_non_mapping_block_is_body_text_in_both_modes():
    content = "---\nSome intro text\n---\n\nbody\n"
    assert fm.parse(content) == ({}, content)
    assert fm.parse(content, strict=True) == ({}, content)
    # An empty block is still frontmatter, not text to keep.
    assert fm.parse("---\n---\n\nbody\n") == ({}, "body\n")


def test_intro_between_rules_survives_create_and_write(tmp_path):
    store = NoteStore(tmp_path)
    content = "---\nSome intro text\n---\n\nbody\n"
    created = store.create("Intro.md", content)
    meta, body = fm.parse(created)
    assert meta["title"] == "Intro"
    assert body == content

    saved = store.write("Intro.md", created)
    assert "Some intro text" in saved
    assert fm.parse(saved)[1] == content


def test_saving_unloadable_yaml_is_422_and_leaves_the_file(tmp_path):
    store = NoteStore(tmp_path)
    original = store.create("Plan.md", "---\ntitle: Plan\n---\n\nbody\n")
    with pytest.raises(NoteError) as err:
        store.write("Plan.md", "---\ntitle: Notes: part 2\n---\n\nbody\n")
    assert err.value.status == 422
    assert err.value.detail.startswith("Frontmatter is not valid YAML:")
    assert store.read("Plan.md") == original
    with pytest.raises(NoteError) as err:
        store.create("Other.md", "---\ntitle: {{title}}\n---\n\nbody\n")
    assert err.value.status == 422
    assert not store.exists("Other.md")


def test_note_with_unloadable_yaml_on_disk_can_be_repaired(tmp_path):
    store = NoteStore(tmp_path)
    broken = "---\ntitle: Notes: part 2\ncreated: 2026-01-02T00:00:00Z\n---\n\nbody\n"
    put("Broken.md", broken, store)

    # Rewriting the block in place would bury it, so these refuse and leave it.
    for call in (
        lambda: store.set_pinned("Broken.md", True),
        lambda: store.set_title("Broken.md", "Broken"),
    ):
        with pytest.raises(NoteError) as err:
            call()
        assert err.value.status == 422
    assert store.read("Broken.md") == broken

    # A rename still moves the file; the title waits for the repair.
    store.rename("Broken.md", "Fixed.md")
    assert store.read("Fixed.md") == broken

    # The user's corrected text saves over the broken file.
    saved = store.write("Fixed.md", '---\ntitle: "Notes: part 2"\n---\n\nbody\n')
    meta, body = fm.parse(saved)
    assert meta["title"] == "Fixed"
    assert body == "body\n"


@pytest.mark.parametrize("yaml_line", UNLOADABLE)
def test_lenient_parse_of_unloadable_yaml_cannot_be_serialized_back(yaml_line):
    # Parse leniently, edit, serialize: the fresh block would sit above the
    # user's, which would become body text, and `created` would reset.
    content = f"---\n{yaml_line}\n---\n\nbody\n"
    meta, body = fm.parse(content)
    assert meta.error is not None
    meta["status"] = "done"
    with pytest.raises(fm.FrontmatterError):
        fm.serialize(meta, body)


def test_intro_between_rules_is_not_flagged_and_still_serializes():
    content = "---\nSome intro text\n---\n\nbody\n"
    meta, body = fm.parse(content)
    assert meta.error is None
    meta["title"] = "Intro"
    assert fm.serialize(meta, body).endswith(content)


def test_omnia_rewrites_refuse_a_note_whose_frontmatter_does_not_load(acting_state):
    broken = (
        "---\ntitle: Meeting: notes\ntags: [work]\n"
        "created: 2026-01-02T00:00:00Z\n---\n\nbody\n"
    )
    put("Meeting.md", broken, acting_state.store)
    revision = content_hash(broken)
    for name, inp in (
        ("edit_frontmatter", {"path": "Meeting.md", "set": {"status": "done"}}),
        ("append_to_note", {"path": "Meeting.md", "text": "More."}),
    ):
        raw = asyncio.run(
            execute_tool(acting_state, "t", name, {**inp, "base_revision": revision})
        )
        result = json.loads(raw)
        assert result["error"].startswith("Frontmatter is not valid YAML:"), result
        assert acting_state.store.read("Meeting.md") == broken


def test_a_refused_rewrite_reaches_the_client_as_a_422():
    from app.main import frontmatter_error_handler

    response = asyncio.run(
        frontmatter_error_handler(None, fm.FrontmatterError("bad (line 2)", 2))
    )
    assert response.status_code == 422
    assert json.loads(response.body) == {"detail": "Frontmatter is not valid YAML: bad (line 2)"}


def test_scalar_tags_save_and_index(state):
    content = state.store.create("Goals.md", "---\ntags: 2024\n---\n\n#focus\n")
    asyncio.run(state.indexer.index_note("Goals.md", content))
    rows = asyncio.run(
        state.db.execute_fetchall("SELECT tag FROM tags WHERE path = ?", ("Goals.md",))
    )
    assert sorted(row["tag"] for row in rows) == ["2024", "focus"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (2024, ["2024"]),
        (1.5, ["1.5"]),
        (date(2024, 1, 1), ["2024-01-01"]),
        (True, []),
        (None, []),
        ("a, #b", ["a", "b"]),
        (["a", 7], ["a", "7"]),
    ],
)
def test_extract_tags_coerces_scalar_frontmatter(value, expected):
    assert parse.extract_tags({"tags": value}, "") == expected


def test_sync_all_indexes_around_bad_notes(state):
    put("sync/Good.md", "---\ntitle: Good\n---\n\nfine\n", state.store)
    put("sync/Year.md", "---\ntags: 2024\n---\n\nyear\n", state.store)
    put("sync/Colon.md", "---\ntitle: a: b\n---\n\ncolon\n", state.store)
    asyncio.run(state.indexer.sync_all())
    rows = asyncio.run(
        state.db.execute_fetchall(
            "SELECT path, title FROM notes WHERE path LIKE 'sync/%' ORDER BY path"
        )
    )
    assert [(row["path"], row["title"]) for row in rows] == [
        ("sync/Colon.md", "Colon"),
        ("sync/Good.md", "Good"),
        ("sync/Year.md", "Year"),
    ]


def test_sync_all_skips_a_note_that_fails_to_index(state, monkeypatch):
    put("sync/Good.md", "---\ntitle: Good\n---\n\nfine\n", state.store)
    put("sync/Bad.md", "---\ntitle: Bad\n---\n\nboom\n", state.store)
    real = Indexer.index_note

    async def flaky(self, rel_path, content, mtime=None, **kwargs):
        if rel_path == "sync/Bad.md":
            raise RuntimeError("index failure")
        await real(self, rel_path, content, mtime, **kwargs)

    monkeypatch.setattr(Indexer, "index_note", flaky)
    asyncio.run(state.indexer.sync_all(force=True))
    rows = asyncio.run(
        state.db.execute_fetchall("SELECT path FROM notes WHERE path LIKE 'sync/%'")
    )
    assert [row["path"] for row in rows] == ["sync/Good.md"]


def test_api_reads_unloadable_note_and_refuses_to_save_it(state):
    broken = "---\ntitle: Notes: part 2\n---\n\nbody\n"
    put("Broken.md", broken, state.store)
    request = request_for(state)

    read = asyncio.run(notes_api.read_note(request, "Broken.md"))
    assert read["meta"] == {}
    assert read["content"] == broken

    with pytest.raises(NoteError) as err:
        asyncio.run(
            notes_api.save_note(
                request, "Broken.md", notes_api.SaveNote(content=broken + "more\n")
            )
        )
    assert err.value.status == 422
    assert state.store.read("Broken.md") == broken

    saved = asyncio.run(
        notes_api.save_note(
            request,
            "Broken.md",
            notes_api.SaveNote(content='---\ntitle: "x"\ntags: 2024\n---\n\nbody\n'),
        )
    )
    assert saved["status"] == "saved"
    assert saved["meta"]["tags"] == 2024


@pytest.mark.parametrize(
    "content",
    [
        "one\u2028two\nthree\n",
        "page\x0cbreak\n",
        "line one\r\nline two\r\n",
        "vt\x0bfs\x1cnel\x85ps\u2029end",
    ],
)
def test_normalize_leaves_task_free_text_byte_exact(content):
    assert parse.normalize_task_metadata(content)[0] == content
    assert parse.regenerate_task_ids(content) == content


def test_crlf_note_with_a_task_stays_crlf():
    content = "---\r\ntitle: T\r\n---\r\n\r\nIntro\r\n- [ ] ship it\r\nOutro\r\n"
    normalized, added = parse.normalize_task_metadata(content, now="2026-01-01T00:00:00Z")
    assert added == 1
    lines = normalized.split("\n")
    assert lines[-1] == ""
    assert all(line.endswith("\r") for line in lines[:-1])
    assert normalized.replace(lines[5] + "\n", "") == content

    meta, body = fm.parse(normalized)
    offset = parse.frontmatter_line_count(normalized, body)
    [task] = parse.extract_tasks(body, offset)
    assert task.text == "ship it"
    assert (task.meta_line, task.line) == (6, 7)
    # Re-saving is a no-op, including the regenerated comment's line ending.
    assert parse.normalize_task_metadata(normalized, previous_content=normalized)[0] == normalized


def test_task_lines_count_only_real_newlines(state):
    content = (
        "---\ntitle: Board\n---\n\n"
        "first\u2028still first\n- [ ] keep\n- [ ] drop\n"
    )
    content, _ = parse.normalize_task_metadata(content)
    state.store.create("Board.md", content)
    written = state.store.read("Board.md")
    asyncio.run(state.indexer.index_note("Board.md", written))
    _meta, body = fm.parse(written)
    tasks = parse.extract_tasks(body, parse.frontmatter_line_count(written, body))
    by_text = {task.text: task for task in tasks}
    lines = written.split("\n")
    assert lines[by_text["drop"].line - 1] == "- [ ] drop"

    asyncio.run(delete_task(state, by_text["drop"].id))
    after = state.store.read("Board.md")
    assert "first\u2028still first" in after
    assert "- [ ] keep" in after
    assert "drop" not in after
    assert after.count("exscalia-task:") == 1


def test_create_task_titles_a_new_note_with_a_quote_in_its_name(state):
    asyncio.run(create_task(state, text="call", path='Say "hi".md'))
    meta, body = fm.parse(state.store.read('Say "hi".md'))
    assert meta["title"] == 'Say "hi"'
    assert "- [ ] call" in body


# -- saving keeps the user's YAML --------------------------------------------
# Each value below changes if the block is loaded and dumped whole (YAML 1.1
# octal, sexagesimal and yes/no/on/NO booleans, dropped comments, restyled
# collections, unquoted timestamps, folded long lines). A save may only touch
# the managed lines.

NOW = "2026-09-23T12:00:00Z"
STAMPS = f"created: '{NOW}'\nmodified: '{NOW}'\n"
USER_YAML = [
    "zip: 01234\n",
    "phone: 0123\n",
    "time: 12:30\n",
    "version: 1.10\n",
    "answer: yes\n",
    "ok: on\n",
    "country: NO\n",
    "# a comment about the next key\nkey: value\n",
    "aliases: [alpha, beta]\n",
    "coords: {lat: 1, lon: 2}\n",
    "summary: |\n  line one\n\n  line two\n",
    "published: 2026-08-01T10:00:00Z\n",
    f"long: {'x' * 100}\n",
    "name: Zoë 東京\n",
    "tags: [a, b]\n",
    "tags:\n  - a\n  - b\n",
]
ALL_USER_YAML = "".join(USER_YAML[:-1])  # one tags: key


@pytest.fixture
def frozen(monkeypatch):
    monkeypatch.setattr(fm, "now_iso", lambda: NOW)


@pytest.mark.parametrize("user_yaml", USER_YAML)
def test_save_adds_managed_keys_above_user_yaml_it_leaves_alone(tmp_path, frozen, user_yaml):
    store = NoteStore(tmp_path)
    content = f"---\n{user_yaml}---\n\nbody\n"
    put("Note.md", content, store)
    saved = store.write("Note.md", content)
    assert saved == f"---\ntitle: Note\n{STAMPS}{user_yaml}---\n\nbody\n"
    assert store.read("Note.md") == saved
    assert store.write("Note.md", saved) == saved


def test_save_rewrites_only_the_modified_line(tmp_path, frozen):
    store = NoteStore(tmp_path)
    head = "---\ntitle: Note\ncreated: 2026-08-01T10:00:00Z\n"
    content = f"{head}modified: '2026-08-02T00:00:00Z'\n{ALL_USER_YAML}---\n\nbody\n"
    put("Note.md", content, store)
    saved = store.write("Note.md", content)
    assert saved == f"{head}modified: '{NOW}'\n{ALL_USER_YAML}---\n\nbody\n"
    assert store.write("Note.md", saved) == saved

    # Reads stay YAML-typed; only writing changed.
    meta, _ = fm.parse(saved)
    assert (meta["zip"], meta["answer"], meta["country"]) == (668, True, False)
    assert meta["aliases"] == ["alpha", "beta"]


def test_save_keeps_crlf_frontmatter(tmp_path, frozen):
    store = NoteStore(tmp_path)
    lines = ["---", "title: Note", f"created: '{NOW}'", "modified: old", "zip: 01234", "---", ""]
    content = "\r\n".join(lines) + "\r\nbody\r\n"
    put("Note.md", content, store)
    saved = store.write("Note.md", content)
    assert saved == content.replace("modified: old", f"modified: '{NOW}'")


def test_pin_adds_and_removes_one_line(state, frozen):
    content = f"---\ntitle: Pins\n{STAMPS}{ALL_USER_YAML}---\n\nbody\n"
    put("Pins.md", content, state.store)
    pinned = state.store.set_pinned("Pins.md", True)
    assert pinned == content.replace("\n---\n\nbody", "\npinned: true\n---\n\nbody")

    asyncio.run(state.indexer.index_note("Pins.md", pinned))
    rows = asyncio.run(
        state.db.execute_fetchall("SELECT pinned FROM notes WHERE path = ?", ("Pins.md",))
    )
    assert rows[0]["pinned"] == 1

    assert state.store.set_pinned("Pins.md", False) == content


def test_rename_realigns_only_the_title_line(tmp_path, frozen):
    store = NoteStore(tmp_path)
    content = f"---\ntitle: Old\n{STAMPS}{ALL_USER_YAML}---\n\nbody\n"
    put("Old.md", content, store)
    assert store.align_title("New.md", content) == content.replace("title: Old", "title: New")
    store.rename("Old.md", "New.md")
    assert store.read("New.md") == content.replace("title: Old", "title: New")


def test_serialize_patches_changed_removed_and_added_keys_only():
    content = (
        "---\ntitle: T\nsummary: |\n  line one\n\n  line two\n\n"
        "# about zip\nzip: 01234\nanswer: yes\n---\n\nbody\n"
    )
    meta, body = fm.parse(content)
    meta["summary"] = "short"
    del meta["answer"]
    meta["rating"] = 8
    assert fm.serialize(meta, body) == (
        "---\ntitle: T\nsummary: short\n\n# about zip\nzip: 01234\nrating: 8\n---\n\nbody\n"
    )
    # Re-typing a value counts as a change even though `1 == True`.
    meta, body = fm.parse("---\nflag: 1\n---\n\nbody\n")
    meta["flag"] = True
    assert fm.serialize(meta, body) == "---\nflag: true\n---\n\nbody\n"


def test_an_indented_block_keeps_its_indent(frozen):
    content = "---\n  title: T\n  # about zip\n  zip: 01234\n---\n\nbody\n"
    assert fm.prepare_for_save(content, None, "T") == (
        f"---\n  title: T\n  created: '{NOW}'\n  modified: '{NOW}'\n"
        "  # about zip\n  zip: 01234\n---\n\nbody\n"
    )


def test_duplicate_keys_patch_around_and_fall_back_when_touched(tmp_path, frozen):
    store = NoteStore(tmp_path)
    content = f"---\ntitle: Dup\ncreated: '{NOW}'\nmodified: old\ndup: 1\ndup: 2\n---\n\nbody\n"
    put("Dup.md", content, store)
    assert store.write("Dup.md", content) == content.replace("modified: old", f"modified: '{NOW}'")

    # Which duplicate to rewrite is ambiguous, so the block is dumped whole.
    twice = f"---\ntitle: Dup\ncreated: '{NOW}'\nmodified: a\nmodified: b\n---\n\nbody\n"
    saved = store.write("Dup.md", twice)
    assert saved == f"---\ntitle: Dup\n{STAMPS}---\n\nbody\n"


def test_whole_dumps_never_fold_long_values():
    title = "A title that is well past eighty characters long, which PyYAML would fold in two"
    assert f"\ntitle: {title}\n" in fm.build_new_note(title)


@pytest.mark.parametrize(
    "user_yaml",
    [
        "title: Odd\n...\n",  # an explicit document end; a key after it is a second document
        "{title: Odd, zip: 01234}\n",  # a one-line flow mapping has no lines to patch
    ],
)
def test_blocks_that_cannot_be_patched_are_dumped_whole(tmp_path, frozen, user_yaml):
    store = NoteStore(tmp_path)
    content = f"---\n{user_yaml}---\n\nbody\n"
    put("Odd.md", content, store)
    meta, body = fm.parse(store.write("Odd.md", content))
    assert (meta["title"], meta["created"], meta["modified"]) == ("Odd", NOW, NOW)
    assert body == "body\n"
