"""Turning an FTS5 ``snippet()`` into segments a client can render safely.

Search results are never HTML. FTS5 marks the matched terms with two private-use
sentinels, and this module converts that marking into a list of
``{text, highlight}`` segments — so a note body containing ``<script>`` stays
inert text all the way to the browser.

It lives beside the index rather than in ``api/meta.py`` because two search
surfaces need it now: the unified note search and Paper's handwriting search.
"""

from __future__ import annotations

import re

HIGHLIGHT_OPEN = "\uE000"
HIGHLIGHT_CLOSE = "\uE001"


def highlight_segments(text: str, query: str, marked: bool = False) -> list[dict]:
    """Split ``text`` into highlighted and plain runs.

    ``marked`` means the text still carries the FTS5 sentinels, which is the
    accurate answer (the engine knows which tokens matched). Without them we
    fall back to a literal, case-insensitive match on the query.
    """
    if marked:
        segments: list[dict] = []
        for part in re.split(f"({HIGHLIGHT_OPEN}|{HIGHLIGHT_CLOSE})", text):
            if not part:
                continue
            if part == HIGHLIGHT_OPEN:
                segments.append({"text": "", "highlight": True, "_marker": "open"})
            elif part == HIGHLIGHT_CLOSE:
                segments.append({"text": "", "highlight": False, "_marker": "close"})
            else:
                active = bool(segments and segments[-1].get("_marker") == "open")
                if segments and segments[-1].get("_marker") in ("open", "close"):
                    marker = segments.pop()
                    if marker["_marker"] == "close":
                        active = False
                segments.append({"text": part, "highlight": active})
        return [
            {"text": segment["text"], "highlight": bool(segment["highlight"])}
            for segment in segments
            if segment.get("text")
        ]
    if not query:
        return [{"text": text, "highlight": False}]
    pattern = re.compile(re.escape(query), re.IGNORECASE)
    segments = []
    cursor = 0
    for match in pattern.finditer(text):
        if match.start() > cursor:
            segments.append({"text": text[cursor : match.start()], "highlight": False})
        segments.append({"text": match.group(0), "highlight": True})
        cursor = match.end()
    if cursor < len(text):
        segments.append({"text": text[cursor:], "highlight": False})
    return segments or [{"text": text, "highlight": False}]


def safe_result(row: dict, query: str = "") -> dict:
    """Strip the sentinels out of ``snippet`` and add ``highlights``."""
    snippet = str(row.get("snippet") or "")
    plain = snippet.replace(HIGHLIGHT_OPEN, "").replace(HIGHLIGHT_CLOSE, "")
    return {
        **row,
        "snippet": plain,
        "highlights": highlight_segments(
            snippet if HIGHLIGHT_OPEN in snippet else plain,
            query,
            marked=HIGHLIGHT_OPEN in snippet,
        ),
    }
