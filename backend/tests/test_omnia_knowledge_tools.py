"""OMNIA Phase 4 — knowledge retrieval and power vault operations."""
import asyncio

import pytest

from app.event_store import EventStore
from app.flashcards import FlashcardStore
from app.index.db import content_hash
from app.overseer.tool_expansion import (
    ask_vault,
    bulk_tag,
    create_note,
    export_notes,
    find_replace_across_vault,
    flashcards_from_note,
    merge_notes,
    resolve_unlinked_mentions,
    set_note_followup,
    split_note,
    summarize_timespan,
)
from app.vault.internal import TrashStore
from app.vault.notes import NoteError


@pytest.fixture
def omnia_state(acting_state):
    acting_state.timezone = "UTC"
    acting_state.events = EventStore(acting_state.settings.vault_dir)
    acting_state.flashcards = FlashcardStore(acting_state.settings.vault_dir)
    acting_state.trash = TrashStore(acting_state.settings.vault_dir, acting_state.store)
    return acting_state


def add_note(state, path: str, content: str) -> str:
    created = state.store.create(path, content)
    asyncio.run(state.indexer.index_note(path, created))
    return created


def note(title: str, body: str = "") -> str:
    return f'---\ntitle: "{title}"\n---\n\n# {title}\n\n{body}\n'


def task_rows(state) -> dict[str, str]:
    """id -> path of every task with metadata (Welcome.md's are legacy)."""
    # The seeded Welcome note carries a real task; only the tests' own rows matter here.
    rows = asyncio.run(
        state.db.execute_fetchall("SELECT id, path FROM tasks WHERE id NOT LIKE 'legacy:%' AND path != 'Welcome.md'")
    )
    return {row["id"]: row["path"] for row in rows}


# --- ask_vault -------------------------------------------------------------


def test_ask_vault_retrieves_relevant_notes(omnia_state):
    add_note(omnia_state, "wildlife.md", note("Wildlife", "The quokka is a small marsupial."))

    result = asyncio.run(ask_vault(omnia_state, "t", {"query": "quokka"}))

    assert any(source["path"] == "wildlife.md" for source in result["sources"])


# --- summarize_timespan ----------------------------------------------------


def test_summarize_timespan_lists_events_in_range(omnia_state):
    # Note created/modified dates are stamped by the store, so drive the range
    # test with an event whose date we control.
    omnia_state.events.add(
        omnia_state.events.normalize(
            title="Conference",
            start="2026-03-15",
            end="2026-03-15",
            all_day=True,
            timezone_name="UTC",
            notes=None,
            note_path=None,
            recurrence=None,
            reminders=[],
        )
    )

    result = asyncio.run(
        summarize_timespan(omnia_state, "t", {"start": "2026-03-01", "end": "2026-03-31"})
    )

    assert any(e["title"] == "Conference" for e in result["events"])


def test_summarize_timespan_rejects_backwards_range(omnia_state):
    with pytest.raises(NoteError):
        asyncio.run(summarize_timespan(omnia_state, "t", {"start": "2026-03-31", "end": "2026-03-01"}))


# --- set_note_followup -----------------------------------------------------


def test_set_note_followup_creates_a_linked_reminder(omnia_state):
    add_note(omnia_state, "read-later.md", note("Read Later"))

    result = asyncio.run(
        set_note_followup(omnia_state, "t", {"path": "read-later.md", "when": "2026-06-01"})
    )

    event = result["event"]
    assert event["title"].startswith("Follow up:")
    assert event["note_path"] == "read-later.md"
    assert event["reminders"] == [0]
    # A day with no time fires that morning, not at midnight.
    assert (event["start"], event["all_day"]) == ("2026-06-01T09:00", False)


def test_set_note_followup_in_days_fires_at_nine_on_the_users_day(omnia_state, monkeypatch):
    from datetime import datetime as real_datetime, timezone

    from app import tz as tz_module

    class Frozen(real_datetime):
        @classmethod
        def now(cls, tz=None):
            # 01:30 UTC on the 22nd is still the evening of the 21st in New York.
            return real_datetime(2026, 9, 22, 1, 30, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(tz_module, "datetime", Frozen)
    omnia_state.timezone = "America/New_York"
    add_note(omnia_state, "read-later.md", note("Read Later"))

    event = asyncio.run(
        set_note_followup(omnia_state, "t", {"path": "read-later.md", "in_days": 14})
    )["event"]

    assert (event["start"], event["end"]) == ("2026-10-05T09:00", "2026-10-05T09:15")
    assert event["all_day"] is False


# --- flashcards_from_note --------------------------------------------------


def test_flashcards_from_note_builds_a_deck(omnia_state):
    add_note(omnia_state, "bio.md", note("Biology"))

    result = asyncio.run(
        flashcards_from_note(
            omnia_state,
            "t",
            {"path": "bio.md", "cards": [{"front": "Powerhouse of the cell?", "back": "Mitochondria"}]},
        )
    )

    assert result["card_count"] == 1
    assert result["name"]  # named after the note


# --- bulk_tag --------------------------------------------------------------


def test_bulk_tag_adds_a_frontmatter_tag_across_a_folder(omnia_state):
    omnia_state.store.create_folder("recipes")
    add_note(omnia_state, "recipes/soup.md", note("Soup"))
    add_note(omnia_state, "recipes/stew.md", note("Stew"))
    add_note(omnia_state, "other.md", note("Other"))

    result = asyncio.run(
        bulk_tag(omnia_state, "t", {"tag": "cooking", "operation": "add", "folder": "recipes"})
    )

    assert result["changed"] == 2
    rows = asyncio.run(
        omnia_state.db.execute_fetchall("SELECT DISTINCT path FROM tags WHERE tag = 'cooking'")
    )
    assert {r["path"] for r in rows} == {"recipes/soup.md", "recipes/stew.md"}


def test_bulk_tag_removes_by_has_tag(omnia_state):
    add_note(omnia_state, "tagged.md", '---\ntitle: "Tagged"\ntags:\n  - draft\n---\n\n# Tagged\n')

    result = asyncio.run(
        bulk_tag(omnia_state, "t", {"tag": "draft", "operation": "remove", "has_tag": "draft"})
    )

    assert result["changed"] == 1
    rows = asyncio.run(omnia_state.db.execute_fetchall("SELECT path FROM tags WHERE tag = 'draft'"))
    assert rows == []


# --- resolve_unlinked_mentions ---------------------------------------------


def test_resolve_unlinked_mentions_links_a_title(omnia_state):
    # A note's title is its filename stem, so name the file to match the prose.
    add_note(omnia_state, "Project Zephyr.md", note("Project Zephyr"))
    add_note(omnia_state, "mentions.md", note("Mentions", "I was reading about Project Zephyr today."))

    result = asyncio.run(resolve_unlinked_mentions(omnia_state, "t", {}))

    assert result["linked"] >= 1
    assert "[[Project Zephyr]]" in omnia_state.store.read("mentions.md")


def test_resolve_unlinked_mentions_links_a_backslash_title_literally(omnia_state):
    # re read the link as a replacement template, so "[[Notes on \alpha]]"
    # was written with a BEL character in place of "\a". Media notes keep
    # their own titles rather than their file names.
    add_note(omnia_state, "media/Greek.md", "---\ntitle: 'Notes on \\alpha'\n---\n\n# Greek\n")
    add_note(omnia_state, "mentions.md", note("Mentions", "I keep Notes on \\alpha nearby."))

    result = asyncio.run(resolve_unlinked_mentions(omnia_state, "t", {}))

    assert result["linked"] == 1
    content = omnia_state.store.read("mentions.md")
    assert "I keep [[Notes on \\alpha]] nearby." in content
    assert "\x07" not in content


# --- find_replace_across_vault ---------------------------------------------


def test_find_replace_dry_run_previews(omnia_state):
    add_note(omnia_state, "a.md", note("A", "colour and colour"))
    add_note(omnia_state, "b.md", note("B", "one colour"))

    preview = asyncio.run(
        find_replace_across_vault(
            omnia_state, "t", {"find": "colour", "replace": "color", "dry_run": True}
        )
    )

    assert preview["dry_run"] is True
    assert preview["note_count"] == 2
    assert preview["total_occurrences"] == 3
    assert "colour" in omnia_state.store.read("a.md")  # unchanged in preview


def test_find_replace_applies(omnia_state):
    add_note(omnia_state, "a.md", note("A", "colour"))

    result = asyncio.run(
        find_replace_across_vault(omnia_state, "t", {"find": "colour", "replace": "color"})
    )

    assert result["note_count"] == 1
    assert "color" in omnia_state.store.read("a.md")
    assert "colour" not in omnia_state.store.read("a.md")


@pytest.mark.parametrize("case_sensitive", [True, False])
@pytest.mark.parametrize("replace", ["\\alpha", "C:\\Users", "\\1", "a\\nb"])
def test_find_replace_writes_the_replacement_literally(omnia_state, case_sensitive, replace):
    # The case-insensitive branch passed `replace` to re as a template: "\alpha"
    # wrote a BEL, "C:\Users" and "\1" raised, "\n" became a newline.
    add_note(omnia_state, "a.md", note("A", "see ALPHA here"))

    result = asyncio.run(
        find_replace_across_vault(
            omnia_state,
            "t",
            {
                "find": "ALPHA" if case_sensitive else "alpha",
                "replace": replace,
                "case_sensitive": case_sensitive,
            },
        )
    )

    assert result["note_count"] == 1
    assert f"see {replace} here" in omnia_state.store.read("a.md")


def edit_between_writes(state, monkeypatch) -> list[str]:
    """Stand in for an editor save that lands while a bulk rewrite awaits the
    first note's indexing: it appends to whichever matching note is still
    unwritten. Returns [path it edited]."""
    index_note = state.indexer.index_note
    edited: list[str] = []

    async def index_then_user_saves(path, content):
        await index_note(path, content)
        if edited:
            return
        other = "b.md" if path == "a.md" else "a.md"
        edited.append(other)
        state.store.write(other, state.store.read(other) + "\nsaved by the user\n")

    monkeypatch.setattr(state.indexer, "index_note", index_then_user_saves)
    return edited


def test_find_replace_keeps_a_save_made_between_writes(omnia_state, monkeypatch):
    add_note(omnia_state, "a.md", note("A", "colour one"))
    add_note(omnia_state, "b.md", note("B", "colour two"))
    edited = edit_between_writes(omnia_state, monkeypatch)

    result = asyncio.run(
        find_replace_across_vault(omnia_state, "t", {"find": "colour", "replace": "color"})
    )

    assert result["note_count"] == 2
    assert result["total_occurrences"] == 2
    assert edited
    for path in ("a.md", "b.md"):
        content = omnia_state.store.read(path)
        assert "color" in content and "colour" not in content
    assert "saved by the user" in omnia_state.store.read(edited[0])


# --- split_note ------------------------------------------------------------


def test_split_note_creates_a_note_per_section(omnia_state):
    body = "Intro line.\n\n## First\n\nFirst content.\n\n## Second\n\nSecond content."
    content = add_note(omnia_state, "big.md", note("Big", body))

    result = asyncio.run(
        split_note(omnia_state, "t", {"path": "big.md", "base_revision": content_hash(content)})
    )

    assert len(result["created"]) == 2
    parent = omnia_state.store.read("big.md")
    assert "[[First]]" in parent and "[[Second]]" in parent
    assert omnia_state.store.exists("First.md")


def test_split_note_links_the_files_its_headings_became(omnia_state):
    add_note(omnia_state, "Plans.md", note("Plans", "taken"))
    body = "## Q&A: Setup\n\nHow to install.\n\n## Plans\n\nNext steps."
    content = add_note(omnia_state, "big.md", note("Big", body))

    result = asyncio.run(
        split_note(omnia_state, "t", {"path": "big.md", "base_revision": content_hash(content)})
    )

    assert result["created"] == ["QA Setup.md", "Plans 2.md"]
    parent = omnia_state.store.read("big.md")
    assert "- [[QA Setup|Q&A: Setup]]" in parent
    assert "- [[Plans 2|Plans]]" in parent
    assert "taken" in omnia_state.store.read("Plans.md")


def test_split_note_ignores_headings_inside_fenced_code(omnia_state):
    body = "## First\n\n```md\n## not a section\n```\n\n## Second\n\nSecond content."
    content = add_note(omnia_state, "big.md", note("Big", body))

    result = asyncio.run(
        split_note(omnia_state, "t", {"path": "big.md", "base_revision": content_hash(content)})
    )

    assert result["created"] == ["First.md", "Second.md"]
    assert "## not a section" in omnia_state.store.read("First.md")


def test_split_note_keeps_each_task_indexed_in_its_new_note(omnia_state):
    # Task ids are unique in the index. A child indexed while the original
    # still held its tasks lost them, and the original's rewrite then
    # recorded them as deleted.
    body = "## Alpha\n\n- [ ] First task\n\n## Beta\n\n- [ ] Second task"
    created = asyncio.run(
        create_note(omnia_state, "t", {"path": "Big.md", "content": note("Big", body)})
    )
    before = task_rows(omnia_state)
    assert sorted(before.values()) == ["Big.md", "Big.md"]

    asyncio.run(
        split_note(omnia_state, "t", {"path": "Big.md", "base_revision": created["revision"]})
    )

    after = task_rows(omnia_state)
    assert set(after) == set(before)
    assert sorted(after.values()) == ["Alpha.md", "Beta.md"]
    assert omnia_state.task_tombstones.all() == {}


def test_split_note_names_repeated_headings_apart(omnia_state):
    body = "## Notes\n\nOne.\n\n## Notes\n\nTwo."
    created = asyncio.run(
        create_note(omnia_state, "t", {"path": "Big.md", "content": note("Big", body)})
    )

    result = asyncio.run(
        split_note(omnia_state, "t", {"path": "Big.md", "base_revision": created["revision"]})
    )

    assert result["created"] == ["Notes.md", "Notes 2.md"]
    assert "One." in omnia_state.store.read("Notes.md")
    assert "Two." in omnia_state.store.read("Notes 2.md")


def test_split_note_refuses_a_section_it_cannot_name_before_writing(omnia_state):
    long_heading = "Overlong " * 30
    body = f"## Short\n\nKept.\n\n## {long_heading}\n\nAlso kept."
    created = asyncio.run(
        create_note(omnia_state, "t", {"path": "Big.md", "content": note("Big", body)})
    )

    with pytest.raises(NoteError):
        asyncio.run(
            split_note(omnia_state, "t", {"path": "Big.md", "base_revision": created["revision"]})
        )

    # The original is rewritten before its children are created, so a name
    # that cannot be created must stop the split before anything is cut.
    assert "Also kept." in omnia_state.store.read("Big.md")
    assert not omnia_state.store.exists("Short.md")


def test_split_note_keeps_an_edit_made_while_it_works(omnia_state, monkeypatch):
    state = omnia_state
    body = "Intro line.\n\n## First\n\nFirst content.\n\n## Second\n\nSecond content."
    content = add_note(state, "big.md", note("Big", body))
    real = state.indexer.index_note
    typed: list[str] = []

    async def index_during_autosave(rel_path, text, *args, **kwargs):
        await real(rel_path, text, *args, **kwargs)
        if not typed:
            # The editor's autosave lands while OMNIA awaits the first index.
            typed.append(rel_path)
            state.store.write("big.md", state.store.read("big.md") + "typed meanwhile\n")

    monkeypatch.setattr(state.indexer, "index_note", index_during_autosave)
    try:
        asyncio.run(split_note(state, "t", {"path": "big.md", "base_revision": content_hash(content)}))
    except NoteError as err:
        # The edit landed between reading the original and rewriting it;
        # the rewrite would have replaced it with the text read at the start.
        assert err.status == 409
    assert "typed meanwhile" in state.store.read("big.md")


# --- merge_notes -----------------------------------------------------------


def test_merge_notes_combines_rewrites_links_and_trashes(omnia_state):
    # Sources are trashed, so they must be git-tracked first — create them
    # through the tool (which commits) rather than writing raw files.
    asyncio.run(create_note(omnia_state, "t", {"path": "Alpha.md", "content": note("Alpha", "Alpha body.")}))
    asyncio.run(create_note(omnia_state, "t", {"path": "Beta.md", "content": note("Beta", "Beta body.")}))
    add_note(omnia_state, "linker.md", note("Linker", "See [[Alpha]] for details."))

    result = asyncio.run(
        merge_notes(omnia_state, "t", {"sources": ["Alpha.md", "Beta.md"], "target": "Combined.md"})
    )

    assert result["target"] == "Combined.md"
    combined = omnia_state.store.read("Combined.md")
    assert "Alpha body." in combined and "Beta body." in combined
    # inbound link repointed to the survivor, sources gone
    assert "[[Combined]]" in omnia_state.store.read("linker.md")
    assert not omnia_state.store.exists("Alpha.md")
    assert not omnia_state.store.exists("Beta.md")


def test_merge_notes_keeps_the_merged_tasks_indexed(omnia_state):
    asyncio.run(create_note(omnia_state, "t", {"path": "M1.md", "content": note("M1", "- [ ] One")}))
    asyncio.run(create_note(omnia_state, "t", {"path": "M2.md", "content": note("M2", "- [ ] Two")}))
    before = task_rows(omnia_state)

    asyncio.run(merge_notes(omnia_state, "t", {"sources": ["M1.md", "M2.md"], "target": "Merged.md"}))

    after = task_rows(omnia_state)
    assert set(after) == set(before)
    assert set(after.values()) == {"Merged.md"}
    assert omnia_state.task_tombstones.all() == {}


def test_merge_notes_into_an_existing_note_keeps_the_merged_tasks_indexed(omnia_state):
    asyncio.run(create_note(omnia_state, "t", {"path": "M1.md", "content": note("M1", "- [ ] One")}))
    asyncio.run(create_note(omnia_state, "t", {"path": "M2.md", "content": note("M2", "- [ ] Two")}))
    asyncio.run(create_note(omnia_state, "t", {"path": "Hub.md", "content": note("Hub", "- [ ] Own")}))
    before = task_rows(omnia_state)

    asyncio.run(merge_notes(omnia_state, "t", {"sources": ["M1.md", "M2.md"], "target": "Hub.md"}))

    after = task_rows(omnia_state)
    assert set(after) == set(before)
    assert set(after.values()) == {"Hub.md"}
    assert omnia_state.task_tombstones.all() == {}


def test_merge_notes_failing_to_write_the_target_leaves_the_sources_indexed(omnia_state):
    asyncio.run(create_note(omnia_state, "t", {"path": "M1.md", "content": note("M1", "- [ ] One")}))
    asyncio.run(create_note(omnia_state, "t", {"path": "M2.md", "content": note("M2", "- [ ] Two")}))
    before = task_rows(omnia_state)

    with pytest.raises(NoteError):
        asyncio.run(
            merge_notes(omnia_state, "t", {"sources": ["M1.md", "M2.md"], "target": "Bad|Name.md"})
        )

    assert omnia_state.store.exists("M1.md") and omnia_state.store.exists("M2.md")
    assert task_rows(omnia_state) == before
    assert omnia_state.task_tombstones.all() == {}
    assert omnia_state.trash.list() == []


def test_merge_notes_undo_takes_the_sources_back_out_of_trash(omnia_state):
    asyncio.run(create_note(omnia_state, "t", {"path": "Alpha.md", "content": note("Alpha", "Alpha body.")}))
    asyncio.run(create_note(omnia_state, "t", {"path": "Beta.md", "content": note("Beta", "Beta body.")}))

    result = asyncio.run(
        merge_notes(omnia_state, "t", {"sources": ["Alpha.md", "Beta.md"], "target": "Combined.md"})
    )
    assert len(omnia_state.trash.list()) == 2

    asyncio.run(omnia_state.actions.undo(result["_action"]["id"], "t"))

    assert omnia_state.store.exists("Alpha.md") and omnia_state.store.exists("Beta.md")
    assert not omnia_state.store.exists("Combined.md")
    # The Trash entries were committed with the merge, so the revert removes
    # them; before, they lingered and restoring one hit "already exists".
    assert omnia_state.trash.list() == []


def test_merge_notes_rewrites_links_by_file_name_not_title(omnia_state):
    # Outside the title-following folders a note's title can differ from its
    # file name, and links resolve by file name.
    asyncio.run(create_note(omnia_state, "t", {"path": "media/dune-film.md", "content": note("Dune", "Film.")}))
    asyncio.run(create_note(omnia_state, "t", {"path": "media/dune-book.md", "content": note("Dune Novel", "Book.")}))
    add_note(omnia_state, "Dune.md", note("Dune", "Unrelated."))
    add_note(
        omnia_state,
        "linker.md",
        note(
            "Linker",
            "Saw [[dune-film|the film]], read [[Dune-Book#Plot]] in"
            " [[media/dune-book|the book]], see [[Dune]].",
        ),
    )

    asyncio.run(
        merge_notes(
            omnia_state,
            "t",
            {"sources": ["media/dune-film.md", "media/dune-book.md"], "target": "media/dune-all.md"},
        )
    )

    linker = omnia_state.store.read("linker.md")
    assert "[[dune-all|the film]]" in linker
    assert "[[dune-all#Plot]]" in linker
    # A link written as a path stays a path.
    assert "[[media/dune-all|the book]]" in linker
    # [[Dune]] opens Dune.md, never the merged-away film note titled Dune.
    assert "[[Dune]]" in linker


def test_merge_notes_leaves_links_that_open_a_same_name_note_elsewhere(omnia_state):
    asyncio.run(create_note(omnia_state, "t", {"path": "Archive/Plan.md", "content": note("Plan", "Old.")}))
    asyncio.run(create_note(omnia_state, "t", {"path": "Archive/Draft.md", "content": note("Draft", "Older.")}))
    add_note(omnia_state, "Plan.md", note("Plan", "Current."))
    add_note(omnia_state, "linker.md", note("Linker", "Now [[Plan]], then [[Archive/Plan]]."))

    asyncio.run(
        merge_notes(
            omnia_state,
            "t",
            {"sources": ["Archive/Plan.md", "Archive/Draft.md"], "target": "Archive/History.md"},
        )
    )

    # [[Plan]] opened the shallower Plan.md, which the merge never touched.
    assert omnia_state.store.read("linker.md").count("[[Plan]]") == 1
    assert "[[Archive/History]]" in omnia_state.store.read("linker.md")


def test_merge_notes_relinks_a_linker_whose_frontmatter_does_not_load(omnia_state):
    state = omnia_state
    asyncio.run(create_note(state, "t", {"path": "M1.md", "content": note("M1", "- [ ] One")}))
    asyncio.run(create_note(state, "t", {"path": "M2.md", "content": note("M2", "- [ ] Two")}))
    # A template: its frontmatter is not YAML, so the strict write refuses it.
    template = "---\ntitle: {{title}}\n---\n\nSee [[M1]].\n"
    state.store.write_raw("Template.md", template)
    asyncio.run(state.indexer.index_note("Template.md", template))
    before = task_rows(state)

    result = asyncio.run(
        merge_notes(state, "t", {"sources": ["M1.md", "M2.md"], "target": "Merged.md"})
    )

    assert "error" not in result
    assert state.store.read("Template.md") == "---\ntitle: {{title}}\n---\n\nSee [[Merged]].\n"
    assert not state.store.exists("M1.md") and not state.store.exists("M2.md")
    assert set(task_rows(state)) == set(before)
    assert "Template.md" in result["_action"]["paths"]


def test_merge_notes_failing_after_the_target_keeps_the_sources_and_records(
    omnia_state, monkeypatch
):
    state = omnia_state
    asyncio.run(create_note(state, "t", {"path": "M1.md", "content": note("M1", "- [ ] One")}))
    asyncio.run(create_note(state, "t", {"path": "M2.md", "content": note("M2", "- [ ] Two")}))
    add_note(state, "linker.md", note("Linker", "See [[M1]]."))
    before = task_rows(state)
    real = state.store.write

    def failing_write(rel_path, *args, **kwargs):
        if rel_path == "linker.md":
            raise OSError("disk full")
        return real(rel_path, *args, **kwargs)

    monkeypatch.setattr(state.store, "write", failing_write)
    result = asyncio.run(
        merge_notes(state, "t", {"sources": ["M1.md", "M2.md"], "target": "Merged.md"})
    )

    assert "disk full" in result["error"]
    assert state.store.exists("M1.md") and state.store.exists("M2.md")
    # The sources went back into the index; their task rows are the target's.
    indexed = {row["path"] for row in asyncio.run(state.db.execute_fetchall("SELECT path FROM notes"))}
    assert {"M1.md", "M2.md", "Merged.md"} <= indexed
    assert set(task_rows(state)) == set(before)
    assert state.trash.list() == []
    assert result["_action"]["undoable"]


def test_merge_notes_trashes_nothing_when_a_source_changes_meanwhile(omnia_state, monkeypatch):
    state = omnia_state
    asyncio.run(create_note(state, "t", {"path": "Alpha.md", "content": note("Alpha", "Alpha body.")}))
    asyncio.run(create_note(state, "t", {"path": "Beta.md", "content": note("Beta", "Beta body.")}))
    real = state.indexer.index_note

    async def index_during_autosave(rel_path, text, *args, **kwargs):
        await real(rel_path, text, *args, **kwargs)
        if rel_path == "Combined.md":
            # An edit to a source lands while OMNIA awaits the target's index.
            state.store.write("Alpha.md", state.store.read("Alpha.md") + "typed meanwhile\n")

    monkeypatch.setattr(state.indexer, "index_note", index_during_autosave)
    result = asyncio.run(
        merge_notes(state, "t", {"sources": ["Alpha.md", "Beta.md"], "target": "Combined.md"})
    )

    assert "Alpha.md changed while OMNIA was working" in result["error"]
    assert "typed meanwhile" in state.store.read("Alpha.md")
    assert state.store.exists("Beta.md")
    assert state.trash.list() == []
    assert result["_action"]["undoable"]


# --- export_notes ----------------------------------------------------------


def test_export_notes_bundles_into_one_note(omnia_state):
    add_note(omnia_state, "one.md", note("One", "First."))
    add_note(omnia_state, "two.md", note("Two", "Second."))

    result = asyncio.run(
        export_notes(omnia_state, "t", {"paths": ["one.md", "two.md"], "name": "bundle"})
    )

    assert result["note_count"] == 2
    bundle = omnia_state.store.read(result["path"])
    assert "First." in bundle and "Second." in bundle
