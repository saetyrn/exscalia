"""The bulk operations a librarian needs. Before these, merging a tag across
30 notes was impossible inside the tool-round budget, and editing a long note
was impossible inside the output-token budget."""

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.index.db import content_hash
from app.overseer.memory import MEMORY_TEMPLATE, split_sections
from app.vault import fm
from app.overseer.tool_expansion import (
    find_unlinked_mentions,
    forget,
    patch_note,
    remember,
    rename_tag,
)
from app.overseer.tools import read_structure
from app.vault.notes import NoteError
from tests.test_omnia_knowledge_tools import edit_between_writes


def _note(body: str, title: str = "T") -> str:
    return f"---\ntitle: {title}\n---\n\n{body}\n"


def test_rename_tag_rewrites_every_note_and_leaves_lookalikes_alone(acting_state):
    acting_state.store.create("a.md", _note("Notes on #ml and #mlops here."))
    acting_state.store.create("b.md", _note("More #ml plus a # Heading line."))
    acting_state.store.create("c.md", _note("Unrelated #cooking."))
    asyncio.run(acting_state.indexer.sync_all(force=True))

    result = asyncio.run(
        rename_tag(acting_state, "t", {"from_tag": "ml", "to_tag": "machine-learning"})
    )

    assert result["note_count"] == 2
    a = acting_state.store.read("a.md")
    assert "#machine-learning" in a
    assert "#mlops" in a  # a longer tag starting with #ml is untouched
    assert "#ml " not in a
    assert "# Heading" in acting_state.store.read("b.md")  # markdown heading untouched
    assert "#cooking" in acting_state.store.read("c.md")


def test_rename_tag_dry_run_changes_nothing(acting_state):
    acting_state.store.create("a.md", _note("Tagged #draft."))
    asyncio.run(acting_state.indexer.sync_all(force=True))

    result = asyncio.run(
        rename_tag(acting_state, "t", {"from_tag": "draft", "to_tag": "wip", "dry_run": True})
    )

    assert result["dry_run"] is True
    assert result["would_change"] == [{"path": "a.md", "occurrences": 1}]
    assert "#draft" in acting_state.store.read("a.md")  # untouched


def test_rename_tag_validates_its_inputs(acting_state):
    for bad in (
        {"from_tag": "x", "to_tag": "x"},
        {"from_tag": "x", "to_tag": ""},
        {"from_tag": "x", "to_tag": "has space"},
        {"from_tag": "x", "to_tag": "1leading-digit"},
    ):
        with pytest.raises(NoteError):
            asyncio.run(rename_tag(acting_state, "t", bad))


def test_rename_tag_keeps_a_save_made_between_writes(acting_state, monkeypatch):
    acting_state.store.create("a.md", _note("First #draft."))
    acting_state.store.create("b.md", _note("Second #draft."))
    asyncio.run(acting_state.indexer.sync_all(force=True))
    edited = edit_between_writes(acting_state, monkeypatch)

    result = asyncio.run(rename_tag(acting_state, "t", {"from_tag": "draft", "to_tag": "wip"}))

    assert result["note_count"] == 2
    assert edited
    for path in ("a.md", "b.md"):
        content = acting_state.store.read(path)
        assert "#wip" in content and "#draft" not in content
    assert "saved by the user" in acting_state.store.read(edited[0])


def test_rename_tag_reports_when_nothing_matches(acting_state):
    result = asyncio.run(rename_tag(acting_state, "t", {"from_tag": "ghost", "to_tag": "gone"}))
    assert result["renamed"] == 0


def test_patch_note_edits_without_rewriting_the_file(acting_state):
    acting_state.store.create("long.md", _note("alpha\nbeta\ngamma"))
    revision = content_hash(acting_state.store.read("long.md"))

    asyncio.run(
        patch_note(
            acting_state,
            "t",
            {
                "path": "long.md",
                "base_revision": revision,
                "edits": [{"old_string": "beta", "new_string": "BETA"}],
            },
        )
    )

    content = acting_state.store.read("long.md")
    assert "BETA" in content
    assert "alpha" in content and "gamma" in content  # rest preserved


def test_patch_note_refuses_ambiguous_and_missing_targets(acting_state):
    acting_state.store.create("dup.md", _note("same\nsame"))
    revision = content_hash(acting_state.store.read("dup.md"))

    with pytest.raises(NoteError) as ambiguous:
        asyncio.run(
            patch_note(
                acting_state,
                "t",
                {
                    "path": "dup.md",
                    "base_revision": revision,
                    "edits": [{"old_string": "same", "new_string": "x"}],
                },
            )
        )
    assert ambiguous.value.status == 409

    with pytest.raises(NoteError) as missing:
        asyncio.run(
            patch_note(
                acting_state,
                "t",
                {
                    "path": "dup.md",
                    "base_revision": revision,
                    "edits": [{"old_string": "nowhere", "new_string": "x"}],
                },
            )
        )
    assert missing.value.status == 409


def test_patch_note_replace_all_handles_repeats(acting_state):
    acting_state.store.create("dup.md", _note("same\nsame"))
    revision = content_hash(acting_state.store.read("dup.md"))

    asyncio.run(
        patch_note(
            acting_state,
            "t",
            {
                "path": "dup.md",
                "base_revision": revision,
                "edits": [
                    {"old_string": "same", "new_string": "done", "replace_all": True}
                ],
            },
        )
    )

    assert acting_state.store.read("dup.md").count("done") == 2


def test_patch_note_enforces_the_revision_check(acting_state):
    acting_state.store.create("race.md", _note("body"))
    with pytest.raises(NoteError):
        asyncio.run(
            patch_note(
                acting_state,
                "t",
                {
                    "path": "race.md",
                    "base_revision": "stale-hash",
                    "edits": [{"old_string": "body", "new_string": "x"}],
                },
            )
        )


def test_read_structure_filters_and_pages(acting_state):
    for i in range(6):
        folder = "School" if i < 4 else "ideas"
        acting_state.store.create(f"{folder}/n{i}.md", _note(f"body {i} #tagged" if i % 2 else "plain"))
    asyncio.run(acting_state.indexer.sync_all(force=True))

    scoped = asyncio.run(read_structure(acting_state, "s", {"folder": "School"}))
    assert scoped["note_count"] == 4
    assert all(n["path"].startswith("School/") for n in scoped["notes"])

    paged = asyncio.run(read_structure(acting_state, "s", {"folder": "School", "limit": 2}))
    assert paged["returned"] == 2
    assert paged["more_available"] is True

    second = asyncio.run(
        read_structure(acting_state, "s", {"folder": "School", "limit": 2, "offset": 2})
    )
    assert second["more_available"] is False
    assert {n["path"] for n in paged["notes"]} & {n["path"] for n in second["notes"]} == set()

    untagged = asyncio.run(read_structure(acting_state, "s", {"untagged_only": True}))
    assert all(not n["tags"] for n in untagged["notes"])


def test_find_unlinked_mentions_skips_what_is_already_linked(acting_state):
    acting_state.store.create("Regression Analysis.md", _note("stats", title="Regression Analysis"))
    acting_state.store.create("lecture.md", _note("Today covered Regression Analysis in depth."))
    acting_state.store.create("linked.md", _note("See [[Regression Analysis]] for detail."))
    asyncio.run(acting_state.indexer.sync_all(force=True))

    found = asyncio.run(find_unlinked_mentions(acting_state, "t", {}))
    sources = {m["in_note"] for m in found["unlinked_mentions"]}

    assert "lecture.md" in sources
    assert "linked.md" not in sources  # already a wikilink


def test_remember_appends_without_rewriting_the_note(acting_state):
    acting_state.store.create(
        "omnia/memory.md", "---\ntitle: OMNIA Memory\n---\n\n## About the user\n\nExisting text.\n"
    )
    # Entries are stamped in the app's timezone, not the machine's.
    today = datetime.now(ZoneInfo(acting_state.timezone)).date().isoformat()

    asyncio.run(remember(acting_state, "t", {"text": "Studies microeconomics this term"}))
    asyncio.run(remember(acting_state, "t", {"text": "Prefers short summaries"}))

    content = acting_state.store.read("omnia/memory.md")
    assert "Existing text." in content  # pre-existing content preserved
    assert f"- {today} Studies microeconomics this term" in content
    assert f"- {today} Prefers short summaries" in content


def test_remember_is_idempotent_and_bounded(acting_state):
    acting_state.store.create("omnia/memory.md", "---\ntitle: OMNIA Memory\n---\n\n")

    first = asyncio.run(remember(acting_state, "t", {"text": "Uses beige theme"}))
    again = asyncio.run(remember(acting_state, "t", {"text": "Uses   beige   theme"}))

    assert first["remembered"] is True
    assert again["remembered"] is False  # whitespace-normalized duplicate
    assert acting_state.store.read("omnia/memory.md").count("Uses beige theme") == 1

    with pytest.raises(NoteError):
        asyncio.run(remember(acting_state, "t", {"text": "x" * 401}))
    with pytest.raises(NoteError):
        asyncio.run(remember(acting_state, "t", {"text": "   "}))


def test_forget_removes_only_matching_entries(acting_state):
    acting_state.store.create("omnia/memory.md", "---\ntitle: OMNIA Memory\n---\n\n")
    asyncio.run(remember(acting_state, "t", {"text": "Studies microeconomics"}))
    asyncio.run(remember(acting_state, "t", {"text": "Prefers short summaries"}))

    asyncio.run(forget(acting_state, "t", {"text": "microeconomics"}))

    content = acting_state.store.read("omnia/memory.md")
    assert "microeconomics" not in content
    assert "Prefers short summaries" in content

    with pytest.raises(NoteError):
        asyncio.run(forget(acting_state, "t", {"text": "never recorded"}))


# --- memory structure: categories, dates, and what the prompt gets -----------


def test_a_category_files_the_entry_under_its_heading(acting_state):
    acting_state.store.create("omnia/memory.md", MEMORY_TEMPLATE)

    asyncio.run(
        remember(acting_state, "t", {"text": "Studies economics", "category": "about"})
    )
    asyncio.run(
        remember(
            acting_state, "t", {"text": "Wants short replies", "category": "preference"}
        )
    )
    asyncio.run(remember(acting_state, "t", {"text": "Something else entirely"}))

    _meta, body = fm.parse(acting_state.store.read("omnia/memory.md"))
    _lead, sections = split_sections(body)
    filed = {title: "\n".join(lines) for title, lines in sections}

    assert "Studies economics" in filed["About the user"]
    assert "Wants short replies" in filed["Preferences"]
    # No category is not "About the user" — it is OMNIA's own catch-all.
    assert "Something else entirely" in filed["Notes to self"]
    assert "Studies economics" not in filed["Notes to self"]


def test_a_missing_heading_is_created_in_template_order(acting_state):
    """Not appended to the very end: a note holding only Preferences must get
    "About the user" above it, or the file drifts into arbitrary order."""
    acting_state.store.create(
        "omnia/memory.md",
        "---\ntitle: OMNIA Memory\n---\n\n## Preferences\n\n- Likes dark mode\n",
    )

    asyncio.run(
        remember(acting_state, "t", {"text": "Lives in Berlin", "category": "about"})
    )
    asyncio.run(
        remember(
            acting_state, "t", {"text": "Uses YAML frontmatter", "category": "convention"}
        )
    )

    _meta, body = fm.parse(acting_state.store.read("omnia/memory.md"))
    titles = [title for title, _ in split_sections(body)[1]]
    assert titles == ["About the user", "Preferences", "Vault conventions"]


def test_the_newest_entry_goes_first_and_the_placeholder_goes(acting_state):
    acting_state.store.create("omnia/memory.md", MEMORY_TEMPLATE)

    asyncio.run(remember(acting_state, "t", {"text": "First", "category": "about"}))
    asyncio.run(remember(acting_state, "t", {"text": "Second", "category": "about"}))

    _meta, body = fm.parse(acting_state.store.read("omnia/memory.md"))
    about = dict(split_sections(body)[1])["About the user"]
    entries = [line for line in about if line.strip()]
    assert entries[0].endswith("Second")
    assert entries[1].endswith("First")
    # The placeholder leaves with the first real entry…
    assert "(nothing recorded yet)" not in "\n".join(about)
    # …but only in the section that was written to.
    assert "(nothing recorded yet)" in body


def test_an_unknown_category_is_refused(acting_state):
    acting_state.store.create("omnia/memory.md", MEMORY_TEMPLATE)
    with pytest.raises(NoteError):
        asyncio.run(remember(acting_state, "t", {"text": "x", "category": "vibes"}))


def test_forget_matches_dated_and_legacy_entries_alike(acting_state):
    """The model forgets a fact by quoting the fact; it has no reason to know
    which day the entry was stamped, and entries predate stamping at all."""
    acting_state.store.create(
        "omnia/memory.md",
        "---\ntitle: OMNIA Memory\n---\n\n## Notes to self\n\n"
        "- 2024-01-05 Studies microeconomics\n"
        "- Prefers short summaries\n",
    )

    asyncio.run(forget(acting_state, "t", {"text": "Studies microeconomics"}))
    asyncio.run(forget(acting_state, "t", {"text": "prefers short summaries"}))

    content = acting_state.store.read("omnia/memory.md")
    assert "microeconomics" not in content
    assert "short summaries" not in content


def test_remembering_the_same_fact_on_another_day_is_still_a_duplicate(acting_state):
    acting_state.store.create(
        "omnia/memory.md",
        "---\ntitle: OMNIA Memory\n---\n\n## Notes to self\n\n"
        "- 2024-01-05 Uses beige theme\n",
    )

    again = asyncio.run(remember(acting_state, "t", {"text": "Uses beige theme"}))

    assert again["remembered"] is False


def test_habits_are_injected_into_the_system_prompt(state):
    """They used to be reachable only by spending a tool round, so in an
    ordinary turn the model had no idea they existed."""
    from app.overseer.agent import Overseer
    from app.overseer.provider import FakeProvider, ProviderReply

    asyncio.run(
        state.db.execute(
            "INSERT INTO habits(key, value, confidence, times_confirmed) VALUES (?,?,?,?)",
            ("daily_note.logs_hours", "yes", 0.95, 3),
        )
    )
    asyncio.run(state.db.commit())

    provider = FakeProvider([ProviderReply(text="ok", stop_reason="end_turn")])
    asyncio.run(Overseer(provider, state).chat("hi"))

    system = provider.systems[-1]
    assert "daily_note.logs_hours: yes" in system
    assert "confidence 0.95" in system
