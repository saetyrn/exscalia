"""User-definable note templates, stored as markdown files in vault/templates/.

Placeholders substituted at instantiation:
    {{title}} — note title
    {{date}}  — YYYY-MM-DD (today in the given timezone unless overridden)
    {{now}}   — current UTC timestamp
"""
import json
from datetime import date as date_cls, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..index import parse
from . import fm

TEMPLATE_DIR = "templates"


def list_templates(vault_dir: Path) -> list[str]:
    tpl_dir = vault_dir / TEMPLATE_DIR
    if not tpl_dir.is_dir():
        return []
    return sorted(p.stem for p in tpl_dir.glob("*.md"))


def _resolve(vault_dir: Path, name: str) -> Path | None:
    """Template names are plain file stems, never paths.

    Without this, a name like "../../../../etc/hosts" read an arbitrary file
    off the host and wrote it into the vault as a note. NoteStore.resolve
    guards note paths the same way; templates had no equivalent.
    """
    if not name or Path(name).name != name or name in (".", ".."):
        return None
    tpl_dir = (vault_dir / TEMPLATE_DIR).resolve()
    tpl_path = (tpl_dir / f"{name}.md").resolve()
    if not tpl_path.is_relative_to(tpl_dir):
        return None
    return tpl_path


# Frontmatter keys the app writes and maintains itself.
_MANAGED_KEYS = ("title", "created", "modified", "pinned")


def strip_managed_frontmatter(content: str) -> str:
    """Drop frontmatter the app owns, wherever it sits at the top of a template.

    A template is a *body*: NoteStore.create writes the real frontmatter for the
    new note. Anything the template carries is at best redundant and at worst
    ends up as visible text.

    Two shapes are removed. A proper leading `---` block is one. The other is an
    orphan — a bare `title:/created:/modified:` run closed by `---`, with no
    opening fence — which is what a template turns into when its own
    frontmatter is not recognized on save and a fresh block is prepended above
    it. That orphan is body content as far as every parser is concerned, so it
    was being copied verbatim into every note made from the template and shown
    as a heading in Read mode.
    """
    _, body = fm.parse(content)
    lines = body.splitlines(keepends=True)
    start = 0
    while start < len(lines) and not lines[start].strip():
        start += 1

    managed = 0
    index = start
    while index < len(lines):
        stripped = lines[index].strip()
        if stripped == "---":
            # Only treat it as an orphan if everything above it was ours.
            return "".join(lines[index + 1 :]).lstrip("\n") if managed else body
        key = stripped.split(":", 1)[0].strip() if ":" in stripped else ""
        if key not in _MANAGED_KEYS:
            return body
        managed += 1
        index += 1
    return body


def _strip_task_metadata(content: str) -> str:
    """Drop task-metadata comments outside fenced code, keeping only a task's
    description.

    Once a template is saved in the editor its tasks carry these comments, and
    every note made from it would inherit the same ids. The indexer's INSERT OR
    IGNORE then drops the copies, so their tasks never reach the board. Fresh
    ids alone are not enough: a template's reminder_at, recurrence, sort or
    completed_at must not be copied into every note either.
    """
    out: list[str] = []
    in_fence = False
    for line in parse.split_lines(content):
        if parse.FENCE_RE.match(line.strip()):
            in_fence = not in_fence
            out.append(line)
            continue
        bare = line.rstrip("\r")
        meta_match = None if in_fence else parse.TASK_META_RE.match(bare)
        if meta_match:
            try:
                value = json.loads(meta_match.group(1))
            except json.JSONDecodeError:
                value = None
            if isinstance(value, dict) and value.get("description"):
                description = {"description": value["description"]}
                out.append(parse.task_meta_line(description) + line[len(bare) :])
            continue
        out.append(line)
    trailing = "\n" if content.endswith("\n") else ""
    return "\n".join(out) + trailing


def _today(timezone: str | None) -> date_cls:
    if timezone:
        try:
            return datetime.now(ZoneInfo(timezone)).date()
        except ZoneInfoNotFoundError:
            pass
    return date_cls.today()


def render(
    vault_dir: Path,
    name: str,
    title: str,
    date: str | None = None,
    timezone: str | None = None,
) -> str | None:
    """Instantiate a template by name. Returns None if it doesn't exist.

    Every task in the result gets fresh metadata, so callers that write it
    straight to disk (daily notes, capture) index its tasks like any other.
    """
    tpl_path = _resolve(vault_dir, name)
    if tpl_path is None or not tpl_path.is_file():
        return None
    raw = strip_managed_frontmatter(tpl_path.read_text(encoding="utf-8"))
    rendered = (
        raw.replace("{{title}}", title)
        .replace("{{date}}", date or _today(timezone).isoformat())
        .replace("{{now}}", fm.now_iso())
    )
    return parse.normalize_task_metadata(_strip_task_metadata(rendered))[0]
