"""A task id belongs to exactly one task line.

tasks.id is the index's primary key and rows go in with INSERT OR IGNORE, so a
second line carrying an id already in use never reaches the board or the
calendar. Duplicating a line, pasting a comment+task pair, or pasting a
checklist from another note must leave the copy with an id of its own, and an
ordinary edit must not churn the ids that are there.
"""
import asyncio
from types import SimpleNamespace

from app.api import notes as notes_api
from app.index import parse
from app.vault import fm

NOW = "2026-09-20T09:00:00Z"
LATER = "2026-09-23T10:00:00Z"


def request_for(state):
    return SimpleNamespace(app=SimpleNamespace(state=state))


def create(state, path: str, content: str | None = None, template: str | None = None) -> dict:
    body = notes_api.CreateNote(path=path, content=content, template=template)
    return asyncio.run(notes_api.create_note(request_for(state), body))


def save(state, note: dict, content: str) -> dict:
    """Save over ``note`` (a create/save response) the way the editor does."""
    body = notes_api.SaveNote(content=content, base_revision=note["revision"])
    return asyncio.run(notes_api.save_note(request_for(state), note["path"], body))


def tasks_of(content: str) -> list[parse.Task]:
    _meta, body = fm.parse(content)
    return parse.extract_tasks(body, parse.frontmatter_line_count(content, body))


def checklist(content: str) -> str:
    """The task block of a note, concealed metadata comments included, as a
    formatted-editor copy carries it."""
    return content[content.index("<!-- exscalia-task:") :]


def rows(state) -> list[tuple[str, str, str]]:
    """Index rows for the notes a test made (the fresh vault seeds Welcome.md)."""
    found = asyncio.run(
        state.db.execute_fetchall(
            "SELECT id, path, text FROM tasks WHERE path != 'Welcome.md' ORDER BY path, line"
        )
    )
    return [(row["id"], row["path"], row["text"]) for row in found]


def test_line_duplicated_below_gets_its_own_id():
    prev, _ = parse.normalize_task_metadata("- [ ] Foo\n", now=NOW)
    [original] = tasks_of(prev)

    new, added = parse.normalize_task_metadata(
        prev + "- [ ] Foo\n", now=LATER, previous_content=prev
    )

    first, copy = tasks_of(new)
    assert first.id == original.id
    assert first.created_at == NOW
    assert copy.id != original.id
    assert copy.created_at == LATER
    assert added == 1


def test_line_duplicated_above_leaves_the_original_its_id():
    # The bare copy comes first; the text-keyed reuse pool must not hand it
    # the id the original line below still carries.
    prev, _ = parse.normalize_task_metadata("- [x] Foo\n", now=NOW)
    [original] = tasks_of(prev)

    new, added = parse.normalize_task_metadata(
        "- [x] Foo\n" + prev, now=LATER, previous_content=prev
    )

    copy, kept = tasks_of(new)
    assert (kept.id, kept.created_at, kept.completed_at) == (original.id, NOW, NOW)
    assert copy.id != original.id
    assert copy.created_at == LATER
    assert added == 1


def test_pasted_comment_and_task_pair_becomes_a_new_task():
    prev, _ = parse.normalize_task_metadata("- [x] Ship it 📅 2026-09-30\n", now=NOW)
    [original] = tasks_of(prev)

    new, added = parse.normalize_task_metadata(prev + prev, now=LATER, previous_content=prev)

    kept, copy = tasks_of(new)
    assert (kept.id, kept.completed_at) == (original.id, NOW)
    assert copy.id != original.id
    # A copy restarts its lifecycle, stamped to match its own checkbox, and
    # keeps what the task says.
    assert (copy.created_at, copy.completed_at, copy.status) == (LATER, LATER, "done")
    assert copy.due == "2026-09-30"
    assert added == 1
    # Normalizing the result again changes nothing.
    assert parse.normalize_task_metadata(new, now=LATER, previous_content=new) == (new, 0)


def test_pasted_pair_without_previous_content_is_split_too():
    block, _ = parse.normalize_task_metadata("- [ ] Water plants\n", now=NOW)
    new, added = parse.normalize_task_metadata(block + block + block, now=LATER)
    assert len({task.id for task in tasks_of(new)}) == 3
    assert added == 2


def test_note_already_sharing_an_id_heals_when_its_comments_are_stripped():
    # A note saved before this fix can hold the same id twice. If an edit then
    # drops the comments, the text-keyed pool offers that id twice.
    block, _ = parse.normalize_task_metadata("- [ ] Foo\n", now=NOW)
    broken = block + block
    [shared] = {task.id for task in tasks_of(broken)}

    new, added = parse.normalize_task_metadata(
        "- [ ] Foo\n- [ ] Foo\n", now=LATER, previous_content=broken
    )

    first, second = tasks_of(new)
    assert first.id == shared
    assert second.id != shared
    assert added == 1


def test_task_ids_counts_only_comments_bound_to_a_task():
    content = (
        '<!-- exscalia-task:{"id":"bound"} -->\n- [ ] kept\n'
        '<!-- exscalia-task:{"id":"orphan"} -->\nplain text\n'
        '```\n<!-- exscalia-task:{"id":"fenced"} -->\n- [ ] code\n```\n'
        '<!-- exscalia-task:{"id":7} -->\n- [ ] numeric\n'
    )
    assert parse.task_ids(content) == {"bound", "7"}
    replaced = parse.replace_task_ids(content, {"bound"})
    fresh = parse.task_ids(replaced) - {"7"}
    assert len(fresh) == 1 and fresh != {"bound"}
    assert '"id":"orphan"' in replaced and '"id":"fenced"' in replaced
    assert replaced.replace(f'"id":"{fresh.pop()}"', '"id":"bound"') == content


def test_checklist_pasted_from_another_note_gets_new_ids(state):
    a = create(state, "A.md", "# A\n\n- [ ] Pack\n- [ ] Book train\n")
    a_ids = [task.id for task in tasks_of(a["content"])]
    b = create(state, "B.md", "# B\n")

    saved = save(state, b, b["content"] + "\n" + checklist(a["content"]))

    b_ids = [task.id for task in tasks_of(saved["content"])]
    assert len(set(b_ids)) == 2 and not set(b_ids) & set(a_ids)
    assert state.store.read("B.md") == saved["content"]
    assert state.store.read("A.md") == a["content"]
    assert rows(state) == [
        (a_ids[0], "A.md", "Pack"),
        (a_ids[1], "A.md", "Book train"),
        (b_ids[0], "B.md", "Pack"),
        (b_ids[1], "B.md", "Book train"),
    ]


def test_note_created_with_another_notes_tasks_gets_new_ids(state):
    source = create(state, "templates/Weekly.md", "- [ ] Review inbox\n")
    [template_task] = tasks_of(source["content"])

    # Templates are notes too, so an instantiated template carries their ids.
    made = create(state, "Week 39.md", template="Weekly")

    [task] = tasks_of(made["content"])
    assert task.id != template_task.id
    assert state.store.read("Week 39.md") == made["content"]
    assert {(path, text) for _id, path, text in rows(state)} == {
        ("templates/Weekly.md", "Review inbox"),
        ("Week 39.md", "Review inbox"),
    }


def test_ordinary_edits_keep_their_ids(state):
    a = create(state, "A.md", "# A\n\n- [ ] Pack\n- [/] Book train\n")
    ids = [task.id for task in tasks_of(a["content"])]

    edited = save(state, a, a["content"] + "\nA line below.\n")
    assert [task.id for task in tasks_of(edited["content"])] == ids

    again = save(state, edited, edited["content"])
    assert again["content"] == edited["content"]
    assert [row[0] for row in rows(state)] == ids


def test_task_cut_from_one_note_and_pasted_into_another_keeps_its_id(state):
    a = create(state, "A.md", "# A\n\n- [ ] Move me\n")
    [task] = tasks_of(a["content"])
    b = create(state, "B.md", "# B\n")

    save(state, a, a["content"].replace(checklist(a["content"]), ""))
    save(state, b, b["content"] + "\n" + checklist(a["content"]))

    assert rows(state) == [(task.id, "B.md", "Move me")]
