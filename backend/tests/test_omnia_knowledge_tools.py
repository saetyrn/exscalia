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
