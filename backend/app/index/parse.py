"""Markdown parsing for the metadata index: wikilinks, tags, tasks."""
import json
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

WIKILINK_RE = re.compile(r"\[\[([^\]|#\n]+)(?:#[^\]|\n]*)?(?:\|[^\]\n]*)?\]\]")
# Inline #tag: an ASCII letter, then letters, digits, combining marks, '_',
# '/' and '-'; not preceded by a word char or '#' (so markdown headings like
# `# Title` and `## Sub` never match). Matched on NFC text. extractNoteTags in
# frontend/src/lib/markdown.ts mirrors it; frontend/src/lib/testing/
# tag-grammar.json pins both. `re` has no \p{M}, and `\w` stops at the marks
# of most scripts (Devanagari vowel signs, Hebrew points), so the regex only
# finds where a tag starts and _tag_end() finds where it stops.
INLINE_TAG_RE = re.compile(r"(?<![\w#])#(?=[A-Za-z])")
TASK_RE = re.compile(r"^(\s*[-*]\s+\[)( |/|x|X)(\]\s+)(.*)$")
DUE_RE = re.compile(r"📅\s*(\d{4}-\d{2}-\d{2})")
FENCE_RE = re.compile(r"^(```|~~~)")
TASK_META_RE = re.compile(r"^\s*<!--\s*exscalia-task:(\{.*\})\s*-->\s*$")


@dataclass
class Task:
    id: str
    line: int  # 1-based line number in the full note file
    meta_line: int | None
    text: str
    status: str
    created_at: str | None
    started_at: str | None
    completed_at: str | None
    reopened_at: str | None
    due: str | None
    due_visible: bool
    description: str | None = None
    # Manual board position, set by dragging a card. None means "never
    # ordered by hand", which sorts by the board's own rules instead.
    sort: float | None = None
    # ISO local date-time this task's reminder should fire, or None. Lives in
    # the task's metadata comment so it stays authoritative in markdown.
    reminder_at: str | None = None

    @property
    def done(self) -> bool:
        return self.status == "done"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def split_lines(text: str) -> list[str]:
    r"""``str.splitlines()`` with only "\n" as a line boundary.

    splitlines also breaks on \r, \x0b, \x0c, \x1c-\x1e, \x85, U+2028 and
    U+2029, so joining its output back with "\n" rewrote those characters on
    every save, and task line numbers disagreed with the editor's. A CRLF line
    keeps its "\r": match against ``line.rstrip("\r")`` and write the line
    back as it was.
    """
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    return lines


def _metadata(
    status_char: str,
    text: str,
    *,
    now: str,
) -> dict:
    due = DUE_RE.search(text)
    status = "done" if status_char.lower() == "x" else (
        "in_progress" if status_char == "/" else "todo"
    )
    return {
        "id": uuid.uuid4().hex,
        "created_at": now,
        "started_at": now if status == "in_progress" else None,
        "completed_at": now if status == "done" else None,
        "reopened_at": None,
        "due": due.group(1) if due else None,
        "due_visible": bool(due),
    }


def _sort_value(value: object) -> float | None:
    """A hand-edited note can put anything in the metadata comment, and a
    non-numeric sort must degrade to "unordered" rather than break indexing."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def task_meta_line(meta: dict) -> str:
    compact = json.dumps(meta, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return f"<!-- exscalia-task:{compact} -->"


def regenerate_task_ids(content: str) -> str:
    """Fresh ids for every task-metadata comment.

    Content copied into a *new* note (a conflict copy) would otherwise carry
    the original note's task ids, and indexing the copy then violates the
    tasks.id uniqueness the whole task system rests on.
    """
    return _reissue_task_ids(content, None)


def replace_task_ids(content: str, ids: set[str]) -> str:
    """Fresh ids for just the task-metadata comments carrying one of ``ids``.

    Tasks pasted in from another note keep that note's ids, which the index
    already holds; the note's own tasks keep their identity."""
    return _reissue_task_ids(content, ids)


def _reissue_task_ids(content: str, ids: set[str] | None) -> str:
    out: list[str] = []
    in_fence = False
    for line in split_lines(content):
        if FENCE_RE.match(line.strip()):
            in_fence = not in_fence
            out.append(line)
            continue
        bare = line.rstrip("\r")
        meta_match = None if in_fence else TASK_META_RE.match(bare)
        if meta_match:
            try:
                value = json.loads(meta_match.group(1))
            except json.JSONDecodeError:
                value = None
            if isinstance(value, dict) and (ids is None or _meta_id(value) in ids):
                value["id"] = uuid.uuid4().hex
                out.append(task_meta_line(value) + line[len(bare) :])
                continue
        out.append(line)
    trailing = "\n" if content.endswith("\n") else ""
    return "\n".join(out) + trailing


def _meta_id(meta: dict) -> str | None:
    # extract_tasks indexes str(id), so a hand-edited numeric id compares the same.
    value = meta.get("id")
    return str(value) if value else None


def _bound_task_states(content: str | None) -> dict[str, bool]:
    """Whether each task is checked off, by the id of its metadata comment:
    the comment directly above the task, outside code fences, as normalize
    and indexing pair them."""
    states: dict[str, bool] = {}
    if not content:
        return states
    in_fence = False
    pending: dict | None = None
    for line in split_lines(content):
        if FENCE_RE.match(line.strip()):
            in_fence = not in_fence
            pending = None
            continue
        if in_fence:
            continue
        bare = line.rstrip("\r")
        meta_match = TASK_META_RE.match(bare)
        if meta_match:
            try:
                value = json.loads(meta_match.group(1))
            except json.JSONDecodeError:
                value = None
            pending = value if isinstance(value, dict) else None
            continue
        task_match = TASK_RE.match(bare) if pending is not None else None
        if task_match:
            task_id = _meta_id(pending)
            if task_id:
                states.setdefault(task_id, task_match.group(2).lower() == "x")
        pending = None
    return states


def task_ids(content: str) -> set[str]:
    """Ids of the metadata comments bound to a task line: the comment directly
    above the task, outside code fences, as normalize and indexing pair them."""
    return set(_bound_task_states(content))


def _task_key(status_text: str) -> str:
    return re.sub(r"\s+", " ", DUE_RE.sub("", status_text).strip()).casefold()


def _metadata_pool(content: str | None, exclude: set[str]) -> dict[str, list[dict]]:
    """Previous metadata by task text, for task lines that lost their comment.

    Ids in ``exclude`` are still bound to their own line in the new content;
    handing one to a second line with the same text would share it."""
    pool: dict[str, list[dict]] = {}
    if not content:
        return pool
    pending: dict | None = None
    for line in split_lines(content):
        line = line.rstrip("\r")
        meta_match = TASK_META_RE.match(line)
        if meta_match:
            try:
                value = json.loads(meta_match.group(1))
                pending = value if isinstance(value, dict) else None
            except json.JSONDecodeError:
                pending = None
            continue
        task_match = TASK_RE.match(line)
        if task_match and pending and _meta_id(pending) not in exclude:
            pool.setdefault(_task_key(task_match.group(4)), []).append(pending)
        pending = None
    return pool


def _refresh_metadata(
    meta: dict,
    status_char: str,
    text: str,
    now: str,
    *,
    was_done: bool | None = None,
) -> dict:
    value = dict(meta)
    value.setdefault("id", uuid.uuid4().hex)
    value.setdefault("created_at", now)
    status = (
        "done"
        if status_char.lower() == "x"
        else "in_progress"
        if status_char == "/"
        else "todo"
    )
    if status == "in_progress" and not value.get("started_at"):
        value["started_at"] = now
    # ``was_done`` is this task's checkbox in the content the save replaces
    # (None: not there, or no previous content). A box the save checked is a
    # fresh completion and one it unchecked a reopen, as in update_task. The
    # stamps alone cannot tell a toggle from a re-save: before update_task
    # restamped every transition, a task completed, reopened and completed
    # again was left done with reopened_at after completed_at.
    completed = value.get("completed_at")
    if status == "done" and (not completed or was_done is False):
        if completed and not value.get("first_completed_at"):
            value["first_completed_at"] = completed
        value["completed_at"] = now
    elif status != "done" and was_done:
        value["reopened_at"] = now
    visible_due = DUE_RE.search(text)
    if visible_due:
        value["due"] = visible_due.group(1)
        value["due_visible"] = True
    else:
        value.setdefault("due", None)
        # Removing the portable marker in raw mode is an intentional request
        # to hide it. Keep the due date itself in metadata.
        value["due_visible"] = False
    return value


def _as_new_task(meta: dict, now: str) -> dict:
    """A copy of a task is a new task: fresh id and a restarted lifecycle.
    _refresh_metadata then stamps started/completed from the checkbox."""
    return {
        **meta,
        "id": uuid.uuid4().hex,
        "created_at": now,
        "started_at": None,
        "completed_at": None,
        "reopened_at": None,
    }


def normalize_task_metadata(
    content: str,
    now: str | None = None,
    *,
    previous_content: str | None = None,
) -> tuple[str, int]:
    """Attach stable metadata comments to task lines and remove orphan comments.

    The comment immediately precedes its task. This keeps the visible Markdown
    task portable while allowing the formatted editor to conceal lifecycle
    bookkeeping.
    """
    now = now or _now_iso()
    lines = split_lines(content)
    trailing = "\n" if content.endswith("\n") else ""
    out: list[str] = []
    added = 0
    in_fence = False
    pending_meta: str | None = None
    previous = _metadata_pool(previous_content, exclude=task_ids(content))
    previous_done = _bound_task_states(previous_content)
    # Ids emitted so far. A duplicated line or a pasted comment+task pair
    # repeats one; the first occurrence keeps it and the copy is a new task,
    # since the index keeps only one row per id.
    seen: set[str] = set()

    for line in lines:
        stripped = line.strip()
        if FENCE_RE.match(stripped):
            if pending_meta is not None:
                # Orphaned task metadata should not accumulate after raw edits.
                pending_meta = None
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue

        bare = line.rstrip("\r")
        meta_match = TASK_META_RE.match(bare)
        if meta_match:
            # Hold it until we know the next line is actually a task.
            pending_meta = line
            continue

        task_match = TASK_RE.match(bare)
        if task_match:
            # A rewritten comment ends like its task line, so CRLF stays CRLF.
            eol = line[len(bare) :]
            if pending_meta is not None:
                meta_match = TASK_META_RE.match(pending_meta.rstrip("\r"))
                if meta_match:
                    try:
                        existing = json.loads(meta_match.group(1))
                    except json.JSONDecodeError:
                        existing = {}
                    metadata = _refresh_metadata(
                        existing,
                        task_match.group(2),
                        task_match.group(4),
                        now,
                        was_done=previous_done.get(_meta_id(existing) or ""),
                    )
                    if _meta_id(metadata) in seen:
                        metadata = _refresh_metadata(
                            _as_new_task(existing, now),
                            task_match.group(2),
                            task_match.group(4),
                            now,
                        )
                        added += 1
                    task_id = _meta_id(metadata)
                    if task_id:
                        seen.add(task_id)
                    out.append(task_meta_line(metadata) + eol)
                else:
                    out.append(pending_meta)
                pending_meta = None
            else:
                key = _task_key(task_match.group(4))
                reused = previous.get(key, [])
                metadata = None
                if reused:
                    entry = reused.pop(0)
                    metadata = _refresh_metadata(
                        entry,
                        task_match.group(2),
                        task_match.group(4),
                        now,
                        was_done=previous_done.get(_meta_id(entry) or ""),
                    )
                    if _meta_id(metadata) in seen:
                        metadata = None
                if metadata is None:
                    metadata = _metadata(task_match.group(2), task_match.group(4), now=now)
                    added += 1
                task_id = _meta_id(metadata)
                if task_id:
                    seen.add(task_id)
                out.append(
                    task_meta_line(metadata) + eol
                )
            out.append(line)
            continue

        pending_meta = None
        out.append(line)

    normalized = "\n".join(out) + trailing
    return normalized, added


def _body_lines_outside_fences(body: str):
    """Yield (line_no_offset, line) skipping fenced code blocks."""
    in_fence = False
    for i, line in enumerate(split_lines(body)):
        if FENCE_RE.match(line.strip()):
            in_fence = not in_fence
            continue
        if not in_fence:
            yield i, line.rstrip("\r")


def extract_wikilinks(body: str) -> list[str]:
    targets = []
    for _, line in _body_lines_outside_fences(body):
        for m in WIKILINK_RE.finditer(line):
            target = m.group(1).strip()
            if target and target not in targets:
                targets.append(target)
    return targets


def _tag_end(text: str, pos: int) -> int:
    """The end of the #tag whose name starts at `pos`: letters, digits and
    '_' (what the regex calls a word char), any combining mark, '/' and '-'."""
    while pos < len(text):
        c = text[pos]
        if not (c.isalnum() or c in "_/-" or unicodedata.category(c).startswith("M")):
            break
        pos += 1
    return pos


def extract_inline_tags(body: str) -> list[str]:
    tags = []
    for _, line in _body_lines_outside_fences(body):
        # NFC first, as the frontend does: decomposed 'résumé' (e + U+0301)
        # is the same tag as the precomposed one.
        text = unicodedata.normalize("NFC", line)
        for m in INLINE_TAG_RE.finditer(text):
            tag = text[m.end():_tag_end(text, m.end())]
            if tag not in tags:
                tags.append(tag)
    return tags


def extract_tags(meta: dict, body: str) -> list[str]:
    """Frontmatter `tags:` plus inline #tags, deduplicated, order preserved."""
    tags: list[str] = []
    fm_tags = meta.get("tags")
    if isinstance(fm_tags, str):
        fm_tags = [t.strip() for t in fm_tags.split(",") if t.strip()]
    elif fm_tags is None or isinstance(fm_tags, bool):
        fm_tags = []
    elif not isinstance(fm_tags, (list, tuple, set, dict)):
        # `tags: 2024` is valid YAML for an int (`tags: 2024-01-01` a date):
        # one tag, not something to iterate.
        fm_tags = [fm_tags]
    for t in fm_tags:
        t = str(t).strip().lstrip("#")
        if t and t not in tags:
            tags.append(t)
    for t in extract_inline_tags(body):
        if t not in tags:
            tags.append(t)
    return tags


def extract_tasks(body: str, line_offset: int = 0) -> list[Task]:
    """Parse task lines and their immediately preceding metadata comments.

    line_offset is the number of lines the frontmatter block occupies, so
    reported line numbers index into the complete note file.
    """
    tasks: list[Task] = []
    pending_meta: tuple[int, dict] | None = None
    for i, line in _body_lines_outside_fences(body):
        meta_match = TASK_META_RE.match(line)
        if meta_match:
            try:
                value = json.loads(meta_match.group(1))
                pending_meta = (i, value) if isinstance(value, dict) else None
            except json.JSONDecodeError:
                pending_meta = None
            continue
        m = TASK_RE.match(line)
        if not m:
            pending_meta = None
            continue
        text = m.group(4).strip()
        if not text or text == "📅":
            pending_meta = None
            continue
        visible_due = DUE_RE.search(text)
        status = "done" if m.group(2).lower() == "x" else (
            "in_progress" if m.group(2) == "/" else "todo"
        )
        meta_line = None
        meta: dict = {}
        if pending_meta and pending_meta[0] == i - 1:
            meta_line = line_offset + pending_meta[0] + 1
            meta = pending_meta[1]
        task_id = str(meta.get("id") or f"legacy:{line_offset + i + 1}")
        tasks.append(
            Task(
                id=task_id,
                line=line_offset + i + 1,
                meta_line=meta_line,
                text=DUE_RE.sub("", text).strip(),
                status=status,
                created_at=meta.get("created_at"),
                started_at=meta.get("started_at"),
                completed_at=meta.get("completed_at"),
                reopened_at=meta.get("reopened_at"),
                due=str(meta.get("due") or (visible_due.group(1) if visible_due else ""))
                or None,
                due_visible=bool(
                    meta.get("due_visible", bool(visible_due))
                ),
                description=(str(meta["description"]) if meta.get("description") else None),
                sort=_sort_value(meta.get("sort")),
                reminder_at=(str(meta["reminder_at"]) if meta.get("reminder_at") else None),
            )
        )
        pending_meta = None
    return tasks


def frontmatter_line_count(content: str, body: str) -> int:
    """Lines occupied by the frontmatter block (content minus body)."""
    return max(0, len(split_lines(content)) - len(split_lines(body)))
