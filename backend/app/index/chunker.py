"""Chunk notes by markdown heading section (not fixed token windows), so each
chunk keeps its heading context and wikilinks intact."""
import re
from dataclasses import dataclass

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
FENCE_RE = re.compile(r"^(```|~~~)")

MAX_CHUNK_CHARS = 4000  # safety split for very long sections


@dataclass
class Chunk:
    heading: str  # breadcrumb like "How it works > Details" ("" for preamble)
    text: str
    index: int


def chunk_note(title: str, body: str) -> list[Chunk]:
    """Split a note body into heading-scoped sections.

    Each chunk's text is prefixed with the note title and heading trail so the
    embedding captures where the content lives.
    """
    sections: list[tuple[str, list[str]]] = []  # (breadcrumb, lines)
    trail: list[tuple[int, str]] = []  # (level, heading text)
    current: list[str] = []
    breadcrumb = ""
    in_fence = False

    def flush():
        nonlocal current
        if any(line.strip() for line in current):
            sections.append((breadcrumb, current))
        current = []

    for line in body.splitlines():
        if FENCE_RE.match(line.strip()):
            in_fence = not in_fence
            current.append(line)
            continue
        m = None if in_fence else HEADING_RE.match(line)
        if m:
            flush()
            level = len(m.group(1))
            text = m.group(2).strip()
            trail[:] = [t for t in trail if t[0] < level]
            trail.append((level, text))
            breadcrumb = " > ".join(t[1] for t in trail)
        else:
            current.append(line)
    flush()

    chunks: list[Chunk] = []
    for heading, lines in sections:
        text = "\n".join(lines).strip()
        prefix = f"{title}" + (f" — {heading}" if heading else "")
        # Split oversized sections on paragraph boundaries.
        parts = _split_long(text) if len(text) > MAX_CHUNK_CHARS else [text]
        for part in parts:
            chunks.append(Chunk(heading=heading, text=f"{prefix}\n\n{part}", index=len(chunks)))
    return chunks


def _split_long(text: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    size = 0
    for para in text.split("\n\n"):
        if size + len(para) > MAX_CHUNK_CHARS and buf:
            parts.append("\n\n".join(buf))
            buf, size = [], 0
        buf.append(para)
        size += len(para) + 2
    if buf:
        parts.append("\n\n".join(buf))
    return parts
