"""Staged proofreading suggestions for a note.

OMNIA proposes spelling, grammar and clarity fixes as *text*: the phrase it
wants to replace and what to replace it with. It is not asked for character
offsets, because a model asked for offsets will confidently invent them. This
module turns those text pairs into real positions in the note, and refuses the
ones that do not point at anything.

Positions go out as UTF-16 code units over the (LF) text that was anchored,
because both consumers use them as CodeMirror positions. Python indexes by
code point, so every emoji or other astral character before a span would
otherwise shift it one unit left. All the checks below run in code points;
`anchor` converts once, when it stores `offset` and `end`.

Suggestions live in a sidecar under `data_dir`, not in the vault: a
proofreading pass is transient review state, and committing it would put a
half-finished critique of the user's writing into their git history and
off-site backup.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .vault.internal import atomic_json, iso_utc, read_json

CATEGORIES = ("spelling", "grammar", "clarity")

# One pass should be reviewable in a sitting. Past this the panel stops being a
# review queue and becomes a wall.
MAX_SUGGESTIONS = 50


class Rejected(Exception):
    """A suggestion that could not be anchored, with a reason for the model."""


def _normalize(
    raw: dict[str, Any], index: int, categories: tuple[str, ...] = CATEGORIES
) -> tuple[str, str, str, str, int | None]:
    category = str(raw.get("category") or "").strip().lower()
    if category not in categories:
        raise Rejected(
            f"suggestion {index}: category must be one of {', '.join(categories)}"
        )
    before = str(raw.get("before") or "")
    after = str(raw.get("after") or "")
    if not before:
        raise Rejected(f"suggestion {index}: `before` is required")
    if before == after:
        raise Rejected(f"suggestion {index}: `before` and `after` are identical")
    reason = str(raw.get("reason") or "").strip()
    occurrence = raw.get("occurrence")
    if occurrence is not None:
        try:
            occurrence = int(occurrence)
        except (TypeError, ValueError):
            raise Rejected(f"suggestion {index}: `occurrence` must be a number") from None
        if occurrence < 0:
            raise Rejected(f"suggestion {index}: `occurrence` must be 0 or greater")
    return category, before, after, reason, occurrence


def _is_word_char(ch: str) -> bool:
    return ch.isalnum() or ch in "'’"


def _whole_word_at(content: str, before: str, at: int) -> bool:
    """True unless the hit at `at` starts or ends in the middle of a word."""
    end = at + len(before)
    if _is_word_char(before[0]) and at > 0 and _is_word_char(content[at - 1]):
        return False
    if _is_word_char(before[-1]) and end < len(content) and _is_word_char(content[end]):
        return False
    return True


def _utf16(content: str, index: int) -> int:
    """A code-point offset into `content` as a UTF-16 (CodeMirror) position."""
    return len(content[:index].encode("utf-16-le")) // 2


def _find(
    content: str,
    before: str,
    occurrence: int | None,
    index: int,
    *,
    whole_words: bool = False,
) -> int:
    """Resolve `before` to a single offset, or explain why it can't be.

    With `whole_words`, hits inside a longer word do not count, so `its` is
    not found inside `limits` and `occurrence` indexes word matches only.
    """
    positions: list[int] = []
    at = content.find(before)
    while at != -1:
        positions.append(at)
        at = content.find(before, at + 1)

    if whole_words and positions:
        positions = [p for p in positions if _whole_word_at(content, before, p)]
        if not positions:
            raise Rejected(
                f"suggestion {index}: {before!r} only appears inside a longer "
                "word — `before` must be whole words"
            )
    if not positions:
        raise Rejected(
            f"suggestion {index}: {before!r} does not appear in the note — "
            "quote the text exactly as written, including punctuation"
        )
    if occurrence is not None:
        if occurrence >= len(positions):
            raise Rejected(
                f"suggestion {index}: occurrence {occurrence} of {before!r} "
                f"does not exist (found {len(positions)})"
            )
        return positions[occurrence]
    if len(positions) > 1:
        raise Rejected(
            f"suggestion {index}: {before!r} appears {len(positions)} times — "
            "add `occurrence` (0-based) to say which one, or quote more "
            "surrounding text so it is unique"
        )
    return positions[0]


def anchor(
    content: str,
    raw_suggestions: list[dict[str, Any]],
    categories: tuple[str, ...] = CATEGORIES,
    *,
    whole_words: bool = False,
    protected: Sequence[tuple[int, int]] = (),
) -> tuple[list[dict], list[str]]:
    """Turn model-supplied text pairs into positioned suggestions.

    Returns `(accepted, rejections)`. Rejections are phrased for the model, so a
    bad batch teaches it what to send instead of failing silently. `protected`
    holds code-point ranges no suggestion may touch. Accepted `offset`/`end`
    are UTF-16 code units over `content` (see the module docstring).
    """
    accepted: list[dict] = []
    rejections: list[str] = []
    taken: list[tuple[int, int]] = []

    for index, raw in enumerate(raw_suggestions):
        if len(accepted) >= MAX_SUGGESTIONS:
            rejections.append(
                f"stopped at {MAX_SUGGESTIONS} suggestions; "
                f"{len(raw_suggestions) - index} more were not staged"
            )
            break
        if not isinstance(raw, dict):
            rejections.append(f"suggestion {index}: expected an object")
            continue
        try:
            category, before, after, reason, occurrence = _normalize(raw, index, categories)
            offset = _find(content, before, occurrence, index, whole_words=whole_words)
        except Rejected as exc:
            rejections.append(str(exc))
            continue

        end = offset + len(before)
        if any(offset < b and a < end for a, b in protected):
            rejections.append(
                f"suggestion {index}: {before!r} is inside code, a link, a tag "
                "or frontmatter — leave it"
            )
            continue
        # Overlapping spans cannot both be applied — accepting one invalidates
        # the other's anchor — so keep the first and explain the drop.
        clash = next((s for s in taken if offset < s[1] and s[0] < end), None)
        if clash is not None:
            rejections.append(
                f"suggestion {index}: overlaps an earlier suggestion at "
                f"{clash[0]}–{clash[1]}; combine them into one"
            )
            continue
        taken.append((offset, end))
        accepted.append(
            {
                "id": uuid.uuid4().hex[:12],
                "category": category,
                "before": before,
                "after": after,
                "reason": reason,
                "offset": _utf16(content, offset),
                "end": _utf16(content, end),
                "status": "open",
            }
        )

    accepted.sort(key=lambda s: s["offset"])
    return accepted, rejections


class ProofreadStore:
    def __init__(self, data_dir: Path):
        self.root = data_dir / "proofread"

    def _file(self, note_path: str) -> Path:
        # Note paths carry slashes and spaces; hashing sidesteps both filename
        # escaping and any chance of a path escaping this directory.
        key = hashlib.sha256(note_path.encode("utf-8")).hexdigest()[:32]
        return self.root / f"{key}.json"

    def get(self, note_path: str) -> dict[str, Any]:
        stored = read_json(self._file(note_path), None, skip_corrupt=True)
        if not isinstance(stored, dict) or stored.get("path") != note_path:
            return {"path": note_path, "revision": None, "suggestions": []}
        stored.setdefault("suggestions", [])
        return stored

    def open_suggestions(self, note_path: str) -> list[dict[str, Any]]:
        return [
            s for s in self.get(note_path).get("suggestions") or []
            if s.get("status") == "open"
        ]

    def save(self, note_path: str, revision: str | None, suggestions: list[dict]) -> dict:
        record = {
            "path": note_path,
            "revision": revision,
            "created_at": iso_utc(),
            "suggestions": suggestions,
        }
        atomic_json(self._file(note_path), record)
        return record

    def dismiss(self, note_path: str, suggestion_id: str) -> bool:
        record = self.get(note_path)
        found = False
        for suggestion in record.get("suggestions") or []:
            if suggestion.get("id") == suggestion_id and suggestion.get("status") == "open":
                suggestion["status"] = "dismissed"
                found = True
        if found:
            atomic_json(self._file(note_path), record)
        return found

    def clear(self, note_path: str) -> None:
        self._file(note_path).unlink(missing_ok=True)

    def rename(self, old_path: str, new_path: str) -> None:
        """Carry a review across a rename, so it isn't orphaned."""
        record = self.get(old_path)
        if not record.get("suggestions"):
            return
        self.save(new_path, record.get("revision"), record["suggestions"])
        self.clear(old_path)
