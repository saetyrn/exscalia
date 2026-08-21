"""YAML frontmatter helpers.

Every note carries auto-managed frontmatter:
    created  — written once at creation, never changed afterwards
    modified — updated on every save
"""
from datetime import datetime, timezone
import re
from typing import Any

import frontmatter

_BOUNDARY_RE = re.compile(r"-{3,}[ \t]*")
_YAML = frontmatter.YAMLHandler()


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(content: str) -> tuple[dict[str, Any], str]:
    """Split note content into (metadata, body) without changing body whitespace.

    ``python-frontmatter`` calls ``strip()`` on both the complete document and
    the body. That is unsafe for an editor: a tab on the first body line,
    trailing spaces, or an indented final line disappears on every autosave.
    EXSCALIA only uses YAML frontmatter, so split that envelope directly and
    let the library parse only the YAML payload.
    """
    lines = content.splitlines(keepends=True)
    if not lines or not _BOUNDARY_RE.fullmatch(lines[0].rstrip("\r\n")):
        return {}, content

    closing = None
    for index in range(1, len(lines)):
        if _BOUNDARY_RE.fullmatch(lines[index].rstrip("\r\n")):
            closing = index
            break
    if closing is None:
        return {}, content

    loaded = _YAML.load("".join(lines[1:closing]))
    metadata = dict(loaded) if isinstance(loaded, dict) else {}
    body = "".join(lines[closing + 1 :])

    # Our serialized envelope has one blank separator line after the closing
    # delimiter. Remove that structural newline only; every byte after it is
    # user-authored body content and must round-trip unchanged.
    if body.startswith("\r\n"):
        body = body[2:]
    elif body.startswith("\n"):
        body = body[1:]
    return metadata, body


def serialize(metadata: dict[str, Any], body: str) -> str:
    """Serialize metadata while preserving the body exactly."""
    yaml = _YAML.export(metadata, sort_keys=False)
    return f"---\n{yaml}\n---\n\n{body}"


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
    The user may freely edit any other frontmatter fields.
    """
    meta, body = parse(content)
    meta.setdefault("title", default_title)
    meta["created"] = existing_created or meta.get("created") or now_iso()
    meta["modified"] = now_iso()
    # Keep a stable, readable key order: title/created/modified first.
    ordered: dict[str, Any] = {}
    for key in ("title", "created", "modified", "tags"):
        if key in meta:
            ordered[key] = meta.pop(key)
    ordered.update(meta)
    return serialize(ordered, body)
