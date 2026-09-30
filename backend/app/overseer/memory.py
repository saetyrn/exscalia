"""OMNIA's memory note: its structure, and how it reaches the prompt.

The note is an ordinary markdown file the user can open and edit, so everything
here works on text rather than a schema. Writing and reading live together
because they have to agree: `remember`/`forget` file entries under a heading,
and the agent renders the same headings back into the system prompt under a
character budget.

Two properties are load-bearing:

* Entries are stored **newest-first** under their heading. That is what makes
  the budgeted rendering able to drop the OLDEST first — it keeps a prefix of
  each section — without parsing dates.
* The rendering never writes. What is on disk stays exactly as the user (or
  OMNIA) left it, including entries the prompt had no room for.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import PurePosixPath
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..vault import fm
from ..vault.notes import NoteError

MEMORY_PATH = "omnia/memory.md"

# category → heading, in the order the template lays them out. `remember` with
# no category files under the catch-all, which always comes last.
MEMORY_CATEGORIES: dict[str, str] = {
    "about": "About the user",
    "preference": "Preferences",
    "project": "Ongoing work & projects",
    "convention": "Vault conventions",
}
CATCH_ALL = "Notes to self"
SECTION_ORDER: tuple[str, ...] = (*MEMORY_CATEGORIES.values(), CATCH_ALL)
PLACEHOLDER = "(nothing recorded yet)"

# The lead paragraph is boilerplate the note carries for the reader's benefit.
# It costs the same tokens every single turn, so the injected copy is capped and
# the budget goes to what was actually learned.
MAX_LEAD_CHARS = 600

_DATE_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}\s+")

MEMORY_TEMPLATE = """\
---
title: OMNIA Memory
---

#omnia

This is OMNIA's working memory about you. OMNIA reads it at the start of every
conversation and records things here as it learns them. Those writes happen
immediately rather than waiting for approval — each one is listed in the OMNIA
history, where it can be undone, and you can edit this note directly yourself.

## About the user

(nothing recorded yet)

## Preferences

(nothing recorded yet)

## Ongoing work & projects

(nothing recorded yet)

## Vault conventions

(nothing recorded yet)

## Notes to self

(nothing recorded yet)
"""


def is_memory_path(path) -> bool:
    """Whether a tool argument names the memory note — normalized, so
    './omnia/memory.md' counts as well."""
    if not isinstance(path, str) or not path:
        return False
    return PurePosixPath(path) == PurePosixPath(MEMORY_PATH)


def local_today(state) -> str:
    """Today's date in the app's timezone. Memory entries are dated for the
    user's benefit, so they follow the user's clock, not UTC."""
    tz_name = getattr(state, "timezone", "America/New_York")
    try:
        return datetime.now(ZoneInfo(tz_name)).date().isoformat()
    except (ZoneInfoNotFoundError, ValueError):
        return datetime.now().date().isoformat()


def strip_date(text: str) -> str:
    """An entry without its `YYYY-MM-DD ` stamp.

    Entries written before dates existed carry none, so every comparison —
    duplicate detection, forget() matching — goes through here and the two
    forms compare equal.
    """
    return _DATE_PREFIX.sub("", text.strip(), count=1)


def normalized(text: str) -> str:
    """The comparison form of an entry: no date, no case, no stray spacing."""
    return " ".join(strip_date(text).split()).lower()


def heading_for(category) -> str:
    value = str(category or "").strip().lower()
    if not value:
        return CATCH_ALL
    if value not in MEMORY_CATEGORIES:
        raise NoteError(
            400,
            f"Unknown memory category '{value}'. Use one of: "
            + ", ".join(MEMORY_CATEGORIES)
            + " — or omit it.",
        )
    return MEMORY_CATEGORIES[value]


def split_sections(body: str) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """(lines before the first heading, [(heading title, its lines)])."""
    lead: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    for line in body.split("\n"):
        if line.startswith("## "):
            sections.append((line[3:].strip(), []))
        elif sections:
            sections[-1][1].append(line)
        else:
            lead.append(line)
    return lead, sections


def _rank(title: str) -> int:
    return SECTION_ORDER.index(title) if title in SECTION_ORDER else len(SECTION_ORDER)


def _insertion_index(titles: list[str], heading: str) -> int:
    """Where a missing heading belongs among the ones the note already has.

    In template order, so a note that only has "Preferences" gets "About the
    user" above it rather than a stray section bolted onto the very end.
    """
    rank = _rank(heading)
    for index, title in enumerate(titles):
        if _rank(title) > rank:
            return index
    return len(titles)


def _lines_with(entry: str, lines: list[str]) -> list[str]:
    """A section's lines with `entry` first, one blank line under the heading."""
    rest = [line for line in lines if line.strip() != PLACEHOLDER]
    while rest and not rest[0].strip():
        rest.pop(0)
    return ["", entry, *rest]


def _assemble(lead: list[str], sections: list[tuple[str, list[str]]]) -> str:
    out = list(lead)
    for title, lines in sections:
        while out and not out[-1].strip():
            out.pop()
        out.extend(["", f"## {title}", *lines])
    return "\n".join(out).strip("\n") + "\n"


def add_entry(content: str, heading: str, entry: str) -> str:
    """Return `content` with `entry` filed newest-first under `heading`.

    A heading the note is missing is created in template order rather than
    appended at the end, and the "(nothing recorded yet)" placeholder in the
    section being written to goes with the first real entry.
    """
    meta, body = fm.parse(content)
    lead, sections = split_sections(body)
    if heading not in [title for title, _ in sections]:
        sections.insert(
            _insertion_index([title for title, _ in sections], heading), (heading, [])
        )
    rebuilt = [
        (title, _lines_with(entry, lines) if title == heading else lines)
        for title, lines in sections
    ]
    return fm.serialize(meta, _assemble(lead, rebuilt))


def _ellipsize(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def render(content: str, limit: int) -> str:
    """The memory note as the prompt sees it.

    Frontmatter, placeholder lines and empty sections are dropped, the lead
    paragraph is capped, and what remains is budgeted so that the OLDEST
    entries are the ones cut: entries sit newest-first, so keeping a prefix of
    each section keeps the newest of it. The budget is spent round-robin across
    sections — filling them in order would spend it all on the first heading
    and leave the last with nothing.

    Nothing here touches the file; a cut entry is still on disk.
    """
    _meta, body = fm.parse(content)
    lead_lines, sections = split_sections(body)
    lead = _ellipsize("\n".join(lead_lines).strip(), MAX_LEAD_CHARS)

    kept: list[tuple[str, list[str]]] = []
    for title, lines in sections:
        entries = [
            line.rstrip()
            for line in lines
            if line.strip() and line.strip() != PLACEHOLDER
        ]
        if entries:
            kept.append((title, entries))
    if not kept:
        return lead

    # The skeleton is paid for first: a heading is cheap, and it tells the model
    # what the note is organized around even when its entries were cut.
    budget = limit - len(lead) - sum(len(title) + 5 for title, _ in kept)
    taken: list[list[str]] = [[] for _ in kept]
    closed: set[int] = set()
    index = 0
    while len(closed) < len(kept):
        for slot, (_title, entries) in enumerate(kept):
            if slot in closed:
                continue
            # A section is closed at its first entry that doesn't fit, so what
            # survives is always a prefix — never a hole in the middle.
            if index >= len(entries) or len(entries[index]) + 1 > budget:
                closed.add(slot)
                continue
            budget -= len(entries[index]) + 1
            taken[slot].append(entries[index])
        index += 1

    blocks = [lead] if lead else []
    for (title, _entries), entries in zip(kept, taken):
        block = f"## {title}"
        if entries:
            block += "\n" + "\n".join(entries)
        blocks.append(block)
    text = "\n\n".join(blocks)
    return text if len(text) <= limit else text[:limit]
