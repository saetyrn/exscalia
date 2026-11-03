"""YAML frontmatter helpers.

Every note carries auto-managed frontmatter:
    created  — written once at creation, never changed afterwards
    modified — updated on every save
"""
from datetime import datetime, timezone
import re
from typing import Any

import frontmatter
import yaml

_BOUNDARY_RE = re.compile(r"-{3,}[ \t]*")
_YAML = frontmatter.YAMLHandler()
# PyYAML folds plain scalars longer than its default width onto a second line.
_WIDTH = 1 << 30
# Keys the app writes, in the order a missing one is added at the top.
_MANAGED = ("title", "created", "modified")
# YAML's line breaks, which its marks count; `str.splitlines` knows more.
_BREAKS = "\r\n\x85\u2028\u2029"
_BREAK = r"\r\n|[\r\n\x85\u2028\u2029]"
_LINE_RE = re.compile(rf".*?(?:{_BREAK})|.+\Z", re.S)


class Meta(dict):
    """Parsed frontmatter that remembers the YAML text it was loaded from.

    serialize() rewrites only the keys whose values changed and leaves every
    other line of that text as the user wrote it. Reloading and dumping the
    whole block would coerce `zip: 01234` to 668 and `answer: yes` to true,
    drop comments and restyle lists. A plain dict, `dict(meta)` included, has
    no text to keep and is dumped whole.

    `error` is set when a leading block did not load and a lenient parse
    degraded to no frontmatter. serialize() refuses such a Meta: writing it
    would put a fresh block above the user's and push theirs into the body.
    """

    _raw: str | None = None
    error: "FrontmatterError | None" = None

    def __init__(
        self,
        data: Any = (),
        raw: str | None = None,
        error: "FrontmatterError | None" = None,
    ):
        super().__init__(data)
        self._raw = raw
        self.error = error


class FrontmatterError(ValueError):
    """A leading ``---`` block whose YAML does not load. Only strict parsing
    raises it; ``line`` is the 1-based line in the note file, when known."""

    def __init__(self, message: str, line: int | None = None):
        self.line = line
        super().__init__(message)


def _load_error(exc: Exception) -> FrontmatterError:
    mark = getattr(exc, "problem_mark", None)
    # The YAML payload starts on the line after the opening delimiter.
    line = mark.line + 2 if mark is not None else None
    problem = getattr(exc, "problem", None) or str(exc)
    return FrontmatterError(f"{problem} (line {line})" if line else problem, line)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(content: str, *, strict: bool = False) -> tuple[Meta, str]:
    """Split note content into (metadata, body) without changing body whitespace.

    ``python-frontmatter`` calls ``strip()`` on both the complete document and
    the body. That is unsafe for an editor: a tab on the first body line,
    trailing spaces, or an indented final line disappears on every autosave.
    EXSCALIA only uses YAML frontmatter, so split that envelope directly and
    let the library parse only the YAML payload.

    Never raises by default: a block that does not load is treated as no
    frontmatter, so ({}, content) comes back and every read still works. Paths
    that re-serialize the block pass ``strict=True`` and get FrontmatterError
    instead — degrading there would write a fresh block above the user's and
    push theirs into the body. A lenient caller that serializes anyway gets
    the same error from serialize(), through the Meta's ``error``.
    """
    lines = content.splitlines(keepends=True)
    if not lines or not _BOUNDARY_RE.fullmatch(lines[0].rstrip("\r\n")):
        return Meta(), content

    closing = None
    for index in range(1, len(lines)):
        if _BOUNDARY_RE.fullmatch(lines[index].rstrip("\r\n")):
            closing = index
            break
    if closing is None:
        return Meta(), content

    raw = "".join(lines[1:closing])
    try:
        loaded = _YAML.load(raw)
    except Exception as exc:
        # PyYAML raises Scanner/Parser/ConstructorError (`title: a: b`,
        # `title: {{title}}`), and an impossible timestamp such as
        # `created: 2026-13-45` a ValueError from datetime.
        error = _load_error(exc)
        if strict:
            raise error from exc
        return Meta(error=error), content
    if loaded is None:
        loaded = {}  # an empty or comment-only block is still frontmatter
    if not isinstance(loaded, dict):
        # `---` / prose / `---` is body text between two horizontal rules, not
        # frontmatter. Returning it unchanged keeps it; a save prepends a real
        # block above it instead of discarding it.
        return Meta(), content
    metadata = Meta(loaded, raw)
    body = "".join(lines[closing + 1 :])

    # Our serialized envelope has one blank separator line after the closing
    # delimiter. Remove that structural newline only; every byte after it is
    # user-authored body content and must round-trip unchanged.
    if body.startswith("\r\n"):
        body = body[2:]
    elif body.startswith("\n"):
        body = body[1:]
    return metadata, body


def _same(a: Any, b: Any) -> bool:
    """Equal and of the same type all the way down: `1 == True == 1.0`, but
    replacing one with another is still a change to write."""
    if a is b:
        return True
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(map(_same, a, b))
    return a == b or (a != a and b != b)  # .nan never equals itself


def _is_gap(line: str, column: int) -> bool:
    """A blank line, or a comment no deeper than the keys, between entries."""
    text = line.lstrip(" \t")
    if not text.strip():
        return True
    return text.startswith("#") and len(line) - len(text) <= column


def _blocks(raw: str, keys: set) -> list[list] | None:
    """Split a YAML mapping into [key, text] entries and [None, text] gaps.

    An entry runs from its key's line to the next key's line, less trailing
    blank and comment lines, which stay with the gap before the next key.
    None when the text is not a plain block mapping of exactly `keys`
    (merge keys, one-line flow maps, parser disagreement).
    """
    if raw and raw[-1] not in _BREAKS:
        return None
    # The pure-Python loader: its marks count characters, as str indexes do.
    loader = yaml.SafeLoader(raw)
    try:
        root = loader.get_single_node()
        if root is None:
            return [[None, raw]] if not keys else None
        if not isinstance(root, yaml.MappingNode) or root.flow_style:
            return None
        found = [
            (loader.construct_object(key, deep=True), key.start_mark)
            for key, _value in root.value
        ]
        if {name for name, _mark in found} != keys:
            return None
    except Exception:
        return None
    finally:
        loader.dispose()

    starts = [max(raw.rfind(c, 0, mark.index) for c in _BREAKS) + 1 for _n, mark in found]
    if any(a >= b for a, b in zip(starts, starts[1:])):
        return None
    blocks: list[list] = [[None, raw[: starts[0]]]]
    for i, (name, mark) in enumerate(found):
        end = starts[i + 1] if i + 1 < len(found) else len(raw)
        lines = _LINE_RE.findall(raw[starts[i] : end])
        keep = len(lines)
        while keep > 1 and _is_gap(lines[keep - 1], mark.column):
            keep -= 1
        blocks.append([name, "".join(lines[:keep])])
        blocks.append([None, "".join(lines[keep:])])
    return blocks


def _patch(metadata: Meta, raw: str) -> str | None:
    """`raw` with only the entries that differ from `metadata` rewritten.

    Changed keys are dumped in place, removed keys lose their lines, missing
    managed keys go to the top in title/created/modified order and any other
    new key after the last entry. None when the text cannot be patched safely.
    """
    original = _YAML.load(raw) or {}
    changed = [k for k in original if k in metadata and not _same(original[k], metadata[k])]
    removed = [k for k in original if k not in metadata]
    added = [k for k in metadata if k not in original]
    if not (changed or removed or added):
        return raw
    blocks = _blocks(raw, set(original))
    if blocks is None:
        return None

    match = re.search(_BREAK, raw)
    nl = "\r\n" if match and match.group() == "\r\n" else "\n"
    # Keys indented as a block keep that indent for the lines written in.
    first = next((text for name, text in blocks if name is not None), "")
    pad = first[: len(first) - len(first.lstrip(" "))]

    def entry(key: Any) -> list:
        text = _YAML.export({key: metadata[key]}, sort_keys=False, width=_WIDTH)
        return [key, "".join(f"{pad}{line}{nl}" for line in text.split("\n"))]

    def where(key: Any) -> list[int]:
        return [i for i, (name, _text) in enumerate(blocks) if name is not None and name == key]

    for key in changed + removed:
        at = where(key)
        if len(at) != 1:
            return None  # a duplicated key: which line holds the value is ambiguous
        if key in metadata:
            blocks[at[0]] = entry(key)
        else:
            del blocks[at[0]]
    for index, key in enumerate(_MANAGED):
        if key in added:
            before = [i for prior in _MANAGED[:index] for i in where(prior)]
            blocks.insert(max(before) + 1 if before else 0, entry(key))
    entries = [i for i, (name, _text) in enumerate(blocks) if name is not None]
    last = entries[-1] if entries else len(blocks) - 1
    for key in added:
        if key not in _MANAGED:
            last += 1
            blocks.insert(last, entry(key))

    patched = "".join(text for _name, text in blocks)
    # Whatever the splitting missed, never write text that loads differently.
    try:
        reloaded = _YAML.load(patched) or {}
    except Exception:
        return None
    return patched if isinstance(reloaded, dict) and _same(reloaded, dict(metadata)) else None


def serialize(metadata: dict[str, Any], body: str) -> str:
    """Serialize metadata while preserving the body exactly.

    Metadata that parse() returned keeps its original text except for the
    entries that changed; anything else is dumped whole. Raises
    FrontmatterError for metadata a lenient parse() could not load.
    """
    if isinstance(metadata, Meta) and metadata.error is not None:
        raise FrontmatterError(str(metadata.error), metadata.error.line)
    raw = metadata._raw if isinstance(metadata, Meta) else None
    if raw is not None:
        patched = _patch(metadata, raw)
        if patched is not None:
            nl = "\r\n" if patched.endswith("\r\n") else "\n"
            return f"---{nl}{patched}---{nl}{nl}{body}"
    yaml_text = _YAML.export(dict(metadata), sort_keys=False, width=_WIDTH)
    return f"---\n{yaml_text}\n---\n\n{body}"


def build_new_note(title: str, body: str = "") -> str:
    """Tags are inline #hashtags in the body, so new notes carry no tags: field
    (frontmatter tags: remains supported for notes that have it)."""
    ts = now_iso()
    meta: dict[str, Any] = {"title": title, "created": ts, "modified": ts}
    return serialize(meta, body)


def prepare_for_save(content: str, existing_created: str | None, default_title: str) -> str:
    """Normalize frontmatter before writing to disk.

    Preserves `created` from the existing on-disk note (or stamps one if the
    note never had it), always refreshes `modified`, and guarantees a title.
    The user may freely edit any other frontmatter fields: only the lines of
    managed keys that change are rewritten. Raises FrontmatterError when a
    leading block is present but does not load.
    """
    meta, body = parse(content, strict=True)
    meta.setdefault("title", default_title)
    meta["created"] = existing_created or meta.get("created") or now_iso()
    meta["modified"] = now_iso()
    # Keep a stable, readable key order: title/created/modified first. That
    # order only applies to a block dumped whole; a patched one keeps the
    # user's order and adds missing managed keys at the top.
    ordered = Meta(raw=meta._raw)
    for key in ("title", "created", "modified", "tags"):
        if key in meta:
            ordered[key] = meta.pop(key)
    ordered.update(meta)
    return serialize(ordered, body)
