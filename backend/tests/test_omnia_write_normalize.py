"""Every OMNIA note write mints task metadata, like an editor save does.

A `- [ ]` line saved without its `<!-- exscalia-task:... -->` comment is
indexed as `legacy:<path>:<line>`, and every task operation then fails with
"Task moved or changed; refresh and try again" — refreshing never repairs it.
"""
import asyncio
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.index.db import content_hash
from app.overseer import tool_expansion as te
from app.task_service import migrate_task_metadata, update_task
from app.vault.notes import NoteStore

from tests.test_paper_omnia import make_notebook, make_page

NOTE = '---\ntitle: "N"\n---\n\n# N\n\n'


def setup_note(state, path: str, content: str) -> str:
    """A note written bare, as the tools used to leave it."""
    state.store.create(path, content)
    stored = state.store.read(path)
    asyncio.run(state.indexer.index_note(path, stored))
    return stored


def select(state, path: str, stored: str, frm: int, to: int) -> None:
    message = {
        "role": "user",
        "selection_range": {"from": frm, "to": to, "head": to, "doc_len": len(stored), "path": path},
    }
    state.threads = SimpleNamespace(get=lambda _tid: {"messages": [message]})


def task_ids(state, path: str) -> list[str]:
    async def fetch():
        rows = await state.db.execute_fetchall(
            "SELECT id FROM tasks WHERE path = ? ORDER BY line", (path,)
        )
        return [row["id"] for row in rows]

    return asyncio.run(fetch())


# Each case writes '- [ ] x' through one tool and returns the note to check.


def via_append_to_note(state):
    stored = setup_note(state, "a.md", NOTE)
    asyncio.run(
        te.append_to_note(
            state, "t", {"path": "a.md", "text": "- [ ] x", "base_revision": content_hash(stored)}
        )
    )
    return "a.md"


def via_patch_note(state):
    stored = setup_note(state, "a.md", NOTE + "HERE\n")
    asyncio.run(
        te.patch_note(
            state,
            "t",
            {
                "path": "a.md",
                "edits": [{"old_string": "HERE", "new_string": "- [ ] x"}],
                "base_revision": content_hash(stored),
            },
        )
    )
    return "a.md"


def via_edit_selection(state):
    stored = setup_note(state, "a.md", NOTE + "HERE\n")
    frm = stored.index("HERE")
    select(state, "a.md", stored, frm, frm + len("HERE"))
    asyncio.run(te.edit_selection(state, "t", {"new_string": "- [ ] x"}))
    return "a.md"


def via_format_selection_task(state):
    stored = setup_note(state, "a.md", NOTE + "x\ny\n")
    frm = stored.index("x\ny")
    select(state, "a.md", stored, frm, frm + len("x\ny"))
    asyncio.run(te.format_selection(state, "t", {"style": "task"}))
    return "a.md"


def via_extract_to_note(state):
    stored = setup_note(state, "a.md", NOTE + "- [ ] x\n\n- [ ] kept\n")
    frm = stored.index("- [ ] x")
    select(state, "a.md", stored, frm, frm + len("- [ ] x"))
    out = asyncio.run(te.extract_to_note(state, "t", {"title": "Extracted"}))
    # The source's own bare task is repaired by the same write.
    assert task_ids(state, "a.md")
    assert not any(i.startswith("legacy:") for i in task_ids(state, "a.md"))
    return out["path"]


def via_note_from_page(state):
    nb = make_notebook(state, "Ideas")
    path, _ = make_page(state, nb, render=True, ai="Page\n- [ ] x")
    out = asyncio.run(te.note_from_page(state, "t", {"path": path}))
    return out["path"]


def via_split_note(state):
    stored = setup_note(state, "a.md", NOTE + "## One\n\n- [ ] x\n\n## Two\n\n- [ ] y\n")
    out = asyncio.run(
        te.split_note(state, "t", {"path": "a.md", "base_revision": content_hash(stored)})
    )
    return out["created"][0]


def via_merge_notes(state):
    setup_note(state, "a.md", NOTE + "- [ ] x\n")
    setup_note(state, "b.md", NOTE + "- [ ] y\n")
    # Sources go to Trash, which commits them, so git has to know them first.
    asyncio.run(state.vault.commit_all("seed"))
    asyncio.run(te.merge_notes(state, "t", {"sources": ["a.md", "b.md"], "target": "M.md"}))
    return "M.md"


def via_export_notes(state):
    setup_note(state, "a.md", NOTE + "- [ ] x\n")
    out = asyncio.run(te.export_notes(state, "t", {"paths": ["a.md"], "name": "out"}))
    return out["path"]


def via_capture_task(state):
    from app.event_store import EventStore

    state.timezone = "UTC"
    state.events = EventStore(state.settings.vault_dir)
    template = state.settings.vault_dir / "templates" / "daily.md"
    template.parent.mkdir(parents=True, exist_ok=True)
    template.write_text("# {{title}}\n\n- [ ] x\n", encoding="utf-8")
    asyncio.run(te.capture(state, "t", {"text": "call the bank", "kind": "task"}))
    today = datetime.now(ZoneInfo("UTC")).date().isoformat()
    path = f"daily/{today}.md"
    # The template's task and the captured one, both real.
    assert len(task_ids(state, path)) == 2
    return path


WRITERS = [
    via_append_to_note,
    via_patch_note,
    via_edit_selection,
    via_format_selection_task,
    via_extract_to_note,
    via_note_from_page,
    via_split_note,
    via_merge_notes,
    via_export_notes,
    via_capture_task,
]


@pytest.mark.parametrize("write", WRITERS, ids=lambda f: f.__name__.removeprefix("via_"))
def test_tool_written_tasks_are_real_tasks(acting_state, write):
    path = write(acting_state)

    ids = task_ids(acting_state, path)
    assert ids, f"no task indexed in {path}"
    assert not [i for i in ids if i.startswith("legacy:")]
    assert "exscalia-task:" in acting_state.store.read(path)
    # The operation that used to 409 with "Task moved or changed".
    done = asyncio.run(update_task(acting_state, ids[0], status="done"))
    assert done["status"] == "done"


def test_an_edit_that_drops_a_comment_keeps_the_task_id(acting_state):
    state = acting_state
    asyncio.run(te.create_note(state, "t", {"path": "a.md", "content": NOTE + "- [ ] keep me\n"}))
    [task_id] = task_ids(state, "a.md")
    current = state.store.read("a.md")
    comment = next(line for line in current.splitlines() if "exscalia-task:" in line)

    # The model rewrites the task and loses its comment on the way.
    asyncio.run(
        te.patch_note(
            state,
            "t",
            {
                "path": "a.md",
                "edits": [{"old_string": f"{comment}\n- [ ] keep me", "new_string": "- [ ] keep me"}],
                "base_revision": content_hash(current),
            },
        )
    )

    assert task_ids(state, "a.md") == [task_id]


def test_revision_is_the_hash_of_what_was_written(acting_state):
    state = acting_state
    stored = setup_note(state, "a.md", NOTE)
    out = asyncio.run(
        te.append_to_note(
            state, "t", {"path": "a.md", "text": "- [ ] x", "base_revision": content_hash(stored)}
        )
    )
    # Minting changed the text, so a revision of the input would be stale at once.
    assert out["revision"] == content_hash(state.store.read("a.md"))


def test_migration_reruns_for_notes_the_tools_left_bare(state):
    vault_dir = state.settings.vault_dir
    state.store.create("Bare.md", NOTE + "- [ ] written by OMNIA\n")
    marker = vault_dir / NoteStore.INTERNAL_DIR / "migrations" / "tasks-v2.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text('{"version": 2}', encoding="utf-8")

    changed = migrate_task_metadata(state.store, vault_dir)

    assert "Bare.md" in changed
    assert "exscalia-task:" in state.store.read("Bare.md")
    assert migrate_task_metadata(state.store, vault_dir) == []
