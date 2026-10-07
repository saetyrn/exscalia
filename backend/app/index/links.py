"""Wikilink resolution, shared by the index, the API and OMNIA's tools.

Mirrors the editor's resolver (frontend/src/lib/wikilinks.ts) rule for rule,
so a backlink, a graph edge and a broken-link report all agree with what a
click opens. A target names a note by exact path (with or without `.md`),
else by file name; when several notes share the name the shallowest path
wins, then plain code-point order. Matching folds Unicode normalization (NFC)
and case explicitly — macOS folds case in the filesystem, Linux prod does not
— but a match spelled exactly as typed beats one that only folds to it:
Linux can hold `a/Ideas.md` beside `b/ideas.md`, and [[ideas]] means the second.

The two resolvers share one table of test vectors (tests/test_links.py and
frontend/src/lib/wikilinks.test.ts): change them together.
"""
import logging
import re
import unicodedata
from typing import Callable, Iterable, Sequence

from ..vault import fm
from ..vault.notes import NoteError
from .db import content_hash
from .parse import FENCE_RE

# parse.WIKILINK_RE with the anchor and alias captured, so a rewrite keeps
# them: 1 = target, 2 = "#heading", 3 = "|alias".
_LINK_RE = re.compile(r"\[\[([^\]|#\n]+)(#[^\]|\n]*)?(\|[^\]\n]*)?\]\]")
_BACKTICKS_RE = re.compile(r"`+")

log = logging.getLogger(__name__)


def fold(text: str) -> str:
    # lower(), not casefold(): it has to match the editor's toLowerCase().
    return unicodedata.normalize("NFC", text).lower()


def _stem(path: str) -> str:
    """Path(path).stem — the note's name as the editor lists it."""
    name = path.rsplit("/", 1)[-1]
    return name[:-3] if name.endswith(".md") and len(name) > 3 else name


def _rank(path: str) -> tuple[int, str]:
    return path.count("/"), path


def _needles(target: str, *, folded: bool = True) -> tuple[str, str]:
    """(exact path to look for, bare name to look for), folded, or only
    NFC-normalized with `folded=False` for the exact-spelling lookups."""
    needle = unicodedata.normalize("NFC", target.strip())
    if folded:
        needle = needle.lower()
    if needle[-3:].lower() == ".md":
        return needle, needle[:-3]
    return needle + ".md", needle


class LinkResolver:
    """resolve_link over one note list, indexed once for many lookups."""

    def __init__(self, paths: Iterable[str]):
        self._by_path: dict[str, str] = {}
        self._by_stem: dict[str, str] = {}
        # The same, keyed by the spelling itself (NFC only).
        self._exact_path: dict[str, str] = {}
        self._exact_stem: dict[str, str] = {}
        for path in paths:
            spelled = unicodedata.normalize("NFC", path)
            for table, key in (
                (self._by_path, fold(path)),
                (self._by_stem, fold(_stem(path))),
                (self._exact_path, spelled),
                (self._exact_stem, _stem(spelled)),
            ):
                best = table.get(key)
                if best is None or _rank(path) < _rank(best):
                    table[key] = path

    def resolve(self, target: str) -> str | None:
        if not target.strip():
            return None
        with_ext, bare = _needles(target)
        spelled_ext, spelled_bare = _needles(target, folded=False)
        # A path match beats a name match; within each, the exact spelling
        # beats the case-folded ones.
        if with_ext in self._by_path:
            return self._exact_path.get(spelled_ext) or self._by_path[with_ext]
        return self._exact_stem.get(spelled_bare) or self._by_stem.get(bare)


def resolve_link(target: str, paths: Iterable[str]) -> str | None:
    """The note path a [[target]] opens, or None when nothing matches."""
    return LinkResolver(paths).resolve(target)


def names_note(target: str, path: str) -> bool:
    """Whether `target` names `path` at all, tie-breaks aside — the links whose
    resolution can change when `path` appears or disappears."""
    if not target.strip():
        return False
    with_ext, bare = _needles(target)
    return fold(path) == with_ext or fold(_stem(path)) == bare


def qualified_target(path: str, paths: Iterable[str]) -> str:
    """The text to put inside [[ ]] so the link opens `path`: its bare name
    when no other note shares it, else the folder-qualified path."""
    pool = {*paths, path}
    resolver = LinkResolver(pool)
    stem = _stem(path)
    key = fold(stem)
    no_ext = path[:-3] if path.endswith(".md") else path
    if sum(1 for p in pool if fold(_stem(p)) == key) == 1 and resolver.resolve(stem) == path:
        return stem
    if resolver.resolve(no_ext) == path:
        return no_ext
    # A name that itself ends in ".md" only resolves spelled out in full.
    return path if resolver.resolve(path) == path else no_ext


def relink(target: str, path: str, paths: Iterable[str]) -> str:
    """New text for a link written as `target`, so that it opens `path`, in
    the same form: a path stays a path, a name stays a name (qualified when
    the name is shared), and a written `.md` stays."""
    ext = target[-3:] if target.lower().endswith(".md") else ""
    if "/" in target:
        return path[:-3] + ext
    return qualified_target(path, paths) + ext


def _code_spans(line: str) -> list[tuple[int, int]]:
    """Inline code spans on one line: a backtick run closed by the next run of
    the same length (CommonMark); an unclosed run is literal text."""
    runs = list(_BACKTICKS_RE.finditer(line))
    spans = []
    i = 0
    while i < len(runs):
        width = len(runs[i].group())
        for j in range(i + 1, len(runs)):
            if len(runs[j].group()) == width:
                spans.append((runs[i].start(), runs[j].end()))
                i = j
                break
        i += 1
    return spans


def rewrite_links(content: str, replace: Callable[[str], str | None]) -> tuple[str, int]:
    """Rewrite wikilink targets in a note's body.

    `replace` gets each link's target (trimmed) and returns the new target
    text, or None to leave that link alone. Embeds (`![[...]]`), `#anchors`,
    `|aliases` and the padding inside the brackets survive; the frontmatter,
    fenced blocks and inline code spans are never touched — the same body
    the indexer reads links from, minus code. Returns (content, links changed).
    """
    _meta, body = fm.parse(content)
    head = content[: len(content) - len(body)]
    changed = 0

    def swap(m: re.Match) -> str:
        nonlocal changed
        raw = m.group(1)
        target = raw.strip()
        new = replace(target) if target else None
        if new is None or new == target:
            return m.group(0)
        changed += 1
        lead = raw[: len(raw) - len(raw.lstrip())]
        trail = raw[len(raw.rstrip()) :]
        return f"[[{lead}{new}{trail}{m.group(2) or ''}{m.group(3) or ''}]]"

    out = []
    in_fence = False
    for line in body.splitlines(keepends=True):
        if FENCE_RE.match(line.strip()):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        pos = 0
        for start, end in _code_spans(line):
            out.append(_LINK_RE.sub(swap, line[pos:start]))
            out.append(line[start:end])
            pos = end
        out.append(_LINK_RE.sub(swap, line[pos:]))
    return head + "".join(out), changed


async def rewrite_inbound_links(
    state, pairs: Sequence[tuple[str, str]], *, announce: bool = True
) -> dict:
    """Keep every link opening the note it opened before a rename.

    Call once the notes have moved on disk and in the index; `pairs` are
    (old, new) note paths. What a link opened is judged against the note
    list from before the move. A link that opened a moved note is pointed at
    its new path, unless it still opens it as written (a folder move keeps
    bare names working); a [[Plan]] that opened a same-name note elsewhere
    stays put. A link that opened a note that did not move, and would now
    open a moved one (a retitle onto a name another note already has), is
    qualified so it keeps opening the note it did. Each rewrite keeps the
    link's form (relink).

    Best effort per linker: one that cannot be read or written is logged and
    skipped, never failing a rename whose notes have already moved. A linker
    whose frontmatter does not load (a template's `title: {{title}}`) gets
    its body rewrite written as is, since only the body changed.

    A linker that moved itself is rewritten at its new path and left for the
    caller to announce along with the move. Every other linker is announced
    with no origin: the renaming device may have it open in another tab, and
    that tab has to pull the rewrite too. `announce=False` leaves all of it
    to the caller (OMNIA's action record announces what it commits).
    """
    moved = {old: new for old, new in pairs if old != new}
    if not moved:
        return {"links_updated": 0, "linkers": []}
    paths_after = [n.path for n in state.store.list_notes()]
    back = {new: old for old, new in moved.items()}
    before = LinkResolver(back.get(p, p) for p in paths_after)
    after = LinkResolver(paths_after)

    def lost(target: str) -> str | None:
        """Where the note `target` opened is now, when `target` no longer
        opens it as written; None when it still does (or never opened one)."""
        was = before.resolve(target)
        if was is None:
            return None
        now = moved.get(was, was)
        return None if after.resolve(target) == now else now

    def retarget(target: str) -> str | None:
        now = lost(target)
        return None if now is None else relink(target, now, paths_after)

    # The index already re-resolved these rows against the new note list,
    # but each keeps its target text, and the old list resolves that again.
    stale: dict[str, bool] = {}
    linkers: set[str] = set()
    for row in await state.db.execute_fetchall("SELECT DISTINCT source, target FROM links"):
        target = row["target"]
        if target not in stale:
            stale[target] = lost(target) is not None
        if stale[target]:
            linkers.add(row["source"])

    updated = 0
    rewritten: list[str] = []
    for linker in sorted(linkers):
        try:
            content = state.store.read(linker)
            text, changed = rewrite_links(content, retarget)
            if not changed:
                continue
            try:
                normalized = state.store.write(linker, text)
            except NoteError as exc:
                if exc.status != 422:
                    raise
                state.store.write_raw(linker, text)
                normalized = text
            await state.indexer.index_note(linker, normalized)
        except NoteError:
            log.warning("could not rewrite the links in %s", linker, exc_info=True)
            continue
        updated += changed
        rewritten.append(linker)
        if announce and linker not in back:
            state.live.note_changed(linker, content_hash(normalized))
    return {"links_updated": updated, "linkers": rewritten}
