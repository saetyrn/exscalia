"""The open note as context, not as a path.

OMNIA used to be told only "the user has X.md open", so answering anything
about it cost a read_notes round on the one note it could have been handed.
The snapshot rides in the turn's context, after the conversation, leaving the
cached system blocks untouched.
"""
import asyncio

from app.overseer.agent import MAX_OPEN_NOTE_BLOCK, Overseer
from app.overseer.provider import FakeProvider, ProviderReply

NOTE = """\
---
title: "Organic Chemistry"
tags: [chem, school]
---

Nucleophiles attack electrophiles, which is the whole subject in one line.

## Reactions

Substitution and elimination compete.

### Substitution

SN1 and SN2 differ in their rate law.
"""


def reply(text="ok"):
    return ProviderReply(text=text, stop_reason="end_turn")


def seed(state, path: str, content: str) -> None:
    created = state.store.create(path, content)
    asyncio.run(state.indexer.index_note(path, created))


def parts(state, replies=1, **kwargs) -> tuple[str, str]:
    """The whole system prompt (every cached block) and the turn's context."""
    provider = FakeProvider([reply() for _ in range(replies)])
    asyncio.run(Overseer(provider, state).chat("what does this say?", **kwargs))
    return "\n\n".join(provider.system_parts[-1]), provider.contexts[-1]


def block_of(context: str) -> str:
    return context[context.index("\nOpen note") :]


# --- the block ---------------------------------------------------------------


def test_the_open_note_arrives_as_a_block_not_just_a_path(state):
    seed(state, "School/Organic Chemistry.md", NOTE)

    stable, context = parts(state, context_note="School/Organic Chemistry.md")

    assert "Open note (as of the start of this turn" in context
    assert "School/Organic Chemistry.md" in context
    assert "Title: Organic Chemistry" in context
    assert "#chem" in context and "#school" in context
    assert "## Reactions" in context and "### Substitution" in context
    assert "Nucleophiles attack electrophiles" in context
    # read_notes stays advertised — this is a snapshot, not the live note.
    assert "read_notes" in context
    assert "Open note" not in stable


def test_the_block_never_grows_past_its_cap(state):
    headings = "\n\n".join(f"## Section {n}\n\nsomething about it" for n in range(60))
    seed(state, "Long.md", f"---\ntitle: Long\n---\n\n{'padding words ' * 400}\n\n{headings}")

    _stable, context = parts(state, context_note="Long.md")
    block = block_of(context)

    assert len(block) <= MAX_OPEN_NOTE_BLOCK
    assert "…" in block  # the trim announces itself rather than reading as an end


def test_a_short_note_is_not_trimmed(state):
    seed(state, "Tiny.md", "---\ntitle: Tiny\n---\n\nJust the one line.")

    _stable, context = parts(state, context_note="Tiny.md")

    assert "Just the one line." in context
    assert "…" not in block_of(context)


def test_staged_edits_are_part_of_what_it_sees(state):
    """Read through `pending`: the model must see the draft it just proposed,
    not the version on disk."""
    seed(state, "Draft.md", "---\ntitle: Draft\n---\n\nthe original sentence")
    asyncio.run(
        state.pending.stage(
            "s", "edit", "Draft.md", new_content="---\ntitle: Draft\n---\n\nthe staged sentence"
        )
    )

    _stable, context = parts(state, context_note="Draft.md")

    assert "the staged sentence" in context
    assert "the original sentence" not in context


# --- when it steps aside -----------------------------------------------------


def test_a_selection_keeps_the_path_only_line(state):
    """The selection is the stronger signal about what "this" means, and it is
    quoted in full — burying it under the note it came from helps nobody."""
    seed(state, "School/Organic Chemistry.md", NOTE)

    _stable, context = parts(
        state,
        context_note="School/Organic Chemistry.md",
        ui_context={"selection": "SN1 and SN2 differ in their rate law."},
    )

    assert "School/Organic Chemistry.md" in context
    assert "read it with read_notes when relevant" in context
    assert "Open note (as of" not in context
    assert "Title: Organic Chemistry" not in context
    assert "SN1 and SN2 differ" in context


def test_a_note_that_is_not_there_degrades_to_the_path(state):
    _stable, context = parts(state, context_note="Ghost.md")

    assert "Ghost.md" in context
    assert "read it with read_notes when relevant" in context
    assert "Open note (as of" not in context


def test_a_failure_degrades_instead_of_breaking_the_turn(state):
    seed(state, "School/Organic Chemistry.md", NOTE)

    real = state.pending.effective_content

    async def boom(path):
        if path.startswith("School/"):
            raise RuntimeError("the index fell over")
        return await real(path)

    state.pending.effective_content = boom

    _stable, context = parts(state, context_note="School/Organic Chemistry.md")

    assert "School/Organic Chemistry.md" in context
    assert "read it with read_notes when relevant" in context


# --- the cache split ---------------------------------------------------------


def test_the_system_prompt_is_byte_identical_with_and_without_a_note(state):
    seed(state, "School/Organic Chemistry.md", NOTE)

    without, _ = parts(state)
    with_note, _ = parts(state, context_note="School/Organic Chemistry.md")
    with_selection, _ = parts(
        state, context_note="School/Organic Chemistry.md", ui_context={"selection": "SN2"}
    )

    assert without == with_note == with_selection
