"""Anchoring proofreading suggestions.

The model supplies text, not offsets, because a model asked for offsets invents
them. Everything here is about refusing a suggestion that does not point at
real text, and saying why clearly enough that the model can fix it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.proofread import MAX_SUGGESTIONS, ProofreadStore, anchor
from app.vault.notes import NoteError

NOTE = "The quick brown fox jumps over the lazy dog.\nThe fox was complimentary.\n"


def test_a_suggestion_is_anchored_to_a_real_offset():
    staged, rejected = anchor(NOTE, [
        {"category": "spelling", "before": "complimentary", "after": "complementary",
         "reason": "wrong word"},
    ])
    assert rejected == []
    assert len(staged) == 1
    at = staged[0]["offset"]
    assert NOTE[at : staged[0]["end"]] == "complimentary"


def test_offsets_are_utf16_units_past_an_astral_character():
    """The editor positions by UTF-16 unit and a dated task's calendar glyph is
    two of them, so a code-point offset would land every later highlight one
    character short."""
    note = "- [ ] Call \U0001F4C5 2026-09-22\nThe fox was complimentary.\n"
    staged, rejected = anchor(note, [
        {"category": "spelling", "before": "complimentary", "after": "complementary"},
    ])
    assert rejected == []
    offset, end = staged[0]["offset"], staged[0]["end"]
    # One extra unit for the glyph's surrogate pair, at both ends.
    start = note.index("complimentary") + 1
    assert (offset, end) == (start, start + len("complimentary"))
    units = note.encode("utf-16-le")
    assert units[2 * offset : 2 * end].decode("utf-16-le") == "complimentary"


def test_text_that_is_not_in_the_note_is_rejected_with_a_reason():
    """The failure this whole design exists to prevent."""
    staged, rejected = anchor(NOTE, [
        {"category": "spelling", "before": "elephant", "after": "giraffe"},
    ])
    assert staged == []
    assert len(rejected) == 1
    assert "does not appear" in rejected[0]
    assert "elephant" in rejected[0]


def test_ambiguous_text_needs_an_occurrence_index():
    staged, rejected = anchor(NOTE, [
        {"category": "clarity", "before": "The", "after": "A"},
    ])
    assert staged == []
    assert "appears 2 times" in rejected[0]
    assert "occurrence" in rejected[0]


def test_an_occurrence_index_disambiguates():
    staged, rejected = anchor(NOTE, [
        {"category": "clarity", "before": "The", "after": "A", "occurrence": 1},
    ])
    assert rejected == []
    assert staged[0]["offset"] == NOTE.index("The", 1)


def test_an_out_of_range_occurrence_is_rejected():
    staged, rejected = anchor(NOTE, [
        {"category": "clarity", "before": "The", "after": "A", "occurrence": 9},
    ])
    assert staged == []
    assert "does not exist" in rejected[0]


def test_a_no_op_suggestion_is_rejected():
    staged, rejected = anchor(NOTE, [
        {"category": "grammar", "before": "fox", "after": "fox", "occurrence": 0},
    ])
    assert staged == []
    assert "identical" in rejected[0]


def test_an_unknown_category_is_rejected():
    staged, rejected = anchor(NOTE, [
        {"category": "vibes", "before": "complimentary", "after": "complementary"},
    ])
    assert staged == []
    assert "category must be one of" in rejected[0]


def test_deleting_text_is_allowed():
    staged, rejected = anchor("The glasses will actually have geotagging.\n", [
        {"category": "clarity", "before": " actually", "after": ""},
    ])
    assert rejected == []
    assert staged[0]["after"] == ""


def test_overlapping_suggestions_keep_the_first_and_explain_the_drop():
    """Accepting one would move the other's anchor, so they cannot coexist."""
    staged, rejected = anchor(NOTE, [
        {"category": "clarity", "before": "quick brown fox", "after": "fox"},
        {"category": "spelling", "before": "brown", "after": "red"},
    ])
    assert len(staged) == 1
    assert staged[0]["before"] == "quick brown fox"
    assert "overlaps" in rejected[0]


def test_good_suggestions_survive_a_bad_one():
    staged, rejected = anchor(NOTE, [
        {"category": "spelling", "before": "nowhere", "after": "x"},
        {"category": "spelling", "before": "complimentary", "after": "complementary"},
    ])
    assert len(staged) == 1 and len(rejected) == 1


def test_the_batch_is_capped():
    content = " ".join(f"word{i}" for i in range(MAX_SUGGESTIONS + 10))
    staged, rejected = anchor(content, [
        {"category": "spelling", "before": f"word{i}", "after": f"WORD{i}"}
        for i in range(MAX_SUGGESTIONS + 10)
    ])
    assert len(staged) == MAX_SUGGESTIONS
    assert any("stopped at" in r for r in rejected)


def test_staged_suggestions_are_ordered_by_position():
    staged, _ = anchor(NOTE, [
        {"category": "spelling", "before": "complimentary", "after": "complementary"},
        {"category": "clarity", "before": "quick", "after": "fast"},
    ])
    assert [s["offset"] for s in staged] == sorted(s["offset"] for s in staged)


# --- the store -------------------------------------------------------------


def test_the_store_keeps_suggestions_out_of_the_vault(tmp_path: Path):
    """A half-finished critique of the user's writing must not reach git."""
    vault, data = tmp_path / "vault", tmp_path / "data"
    vault.mkdir()
    store = ProofreadStore(data)
    staged, _ = anchor(NOTE, [
        {"category": "spelling", "before": "complimentary", "after": "complementary"},
    ])
    store.save("ideas/Notes.md", "rev1", staged)

    assert store._file("ideas/Notes.md").is_file()
    assert data in store._file("ideas/Notes.md").parents
    assert not list(vault.rglob("*.json"))


def test_the_store_round_trips_and_dismisses(tmp_path: Path):
    store = ProofreadStore(tmp_path)
    staged, _ = anchor(NOTE, [
        {"category": "spelling", "before": "complimentary", "after": "complementary"},
        {"category": "clarity", "before": "quick", "after": "fast"},
    ])
    store.save("A.md", "rev1", staged)
    assert len(store.open_suggestions("A.md")) == 2

    assert store.dismiss("A.md", staged[0]["id"]) is True
    assert len(store.open_suggestions("A.md")) == 1
    # Dismissing twice is not an error the caller has to special-case, but it
    # reports that nothing changed.
    assert store.dismiss("A.md", staged[0]["id"]) is False


def test_a_review_follows_a_renamed_note(tmp_path: Path):
    store = ProofreadStore(tmp_path)
    staged, _ = anchor(NOTE, [
        {"category": "spelling", "before": "complimentary", "after": "complementary"},
    ])
    store.save("Old.md", "rev1", staged)
    store.rename("Old.md", "New.md")
    assert store.open_suggestions("Old.md") == []
    assert len(store.open_suggestions("New.md")) == 1


def test_notes_do_not_share_a_review(tmp_path: Path):
    store = ProofreadStore(tmp_path)
    staged, _ = anchor(NOTE, [
        {"category": "spelling", "before": "complimentary", "after": "complementary"},
    ])
    store.save("A.md", "rev1", staged)
    assert store.open_suggestions("B.md") == []


# --- the tool --------------------------------------------------------------


@pytest.fixture
def proof_state(acting_state, tmp_path):
    acting_state.proofread = ProofreadStore(tmp_path / "proof")
    return acting_state


def test_the_tool_stages_and_reports_rejections(proof_state):
    from app.overseer.tool_expansion import propose_edits

    state = proof_state
    state.store.create("Draft.md", NOTE)

    result = asyncio.run(propose_edits(state, "t", {
        "path": "Draft.md",
        "suggestions": [
            {"category": "spelling", "before": "complimentary", "after": "complementary",
             "reason": "wrong word"},
            {"category": "spelling", "before": "not in the note", "after": "x"},
        ],
    }))

    assert result["staged"] == 1
    assert result["by_category"] == {"spelling": 1}
    assert len(result["rejected"]) == 1
    assert len(state.proofread.open_suggestions("Draft.md")) == 1
    # The note itself is untouched — staging is not editing.
    assert state.store.read("Draft.md").count("complimentary") == 1


def test_the_tool_does_not_write_the_note(proof_state):
    from app.overseer.tool_expansion import propose_edits

    state = proof_state
    state.store.create("Draft.md", NOTE)
    before = state.store.read("Draft.md")
    asyncio.run(propose_edits(state, "t", {
        "path": "Draft.md",
        "suggestions": [
            {"category": "clarity", "before": "quick", "after": "fast"},
        ],
    }))
    assert state.store.read("Draft.md") == before


def test_the_tool_refuses_an_empty_batch(proof_state):
    from app.overseer.tool_expansion import propose_edits

    proof_state.store.create("Draft.md", NOTE)
    with pytest.raises(NoteError):
        asyncio.run(propose_edits(proof_state, "t", {"path": "Draft.md", "suggestions": []}))
