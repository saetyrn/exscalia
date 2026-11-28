"""OMNIA's scoped commits must hold only OMNIA's delta, so Undo is a true
inverse. The user's pending working-tree state (typing, a fresh note, a
deletion the debounced autosave has not reached yet) gets its own
user-authored "autosave" commit right before OMNIA mutates anything.

Without it, `git add -A -- <path>` swept a same-note paragraph into the OMNIA
commit (Undo then reverted the user's text too), and a path created or
removed since the last autosave was untracked, so the scoped commit failed
on its pathspec after the file operation had already run."""
import asyncio
import json

from app.index.db import content_hash
from app.overseer.actions import ActionCoordinator
from app.overseer.tools import execute_tool


def run_tool(state, name: str, inp: dict, **kwargs) -> dict:
    async def call():
        return await execute_tool(state, "thread-a", name, inp, **kwargs)

    return json.loads(asyncio.run(call()))


def undo(state, action: dict) -> dict:
    return asyncio.run(state.actions.undo(action["id"], "thread-a"))


def added_lines(repo, older, newer, path: str) -> list[str]:
    diff = repo.git.diff(older, newer, "--", path)
    return [
        line for line in diff.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]


def test_undo_keeps_unsaved_typing_in_the_same_note(acting_state):
    state = acting_state
    state.store.create("Note.md", "line one\n")
    asyncio.run(state.vault.commit_all("seed"))
    dirty = state.store.write("Note.md", "line one\n\nUSER PARAGRAPH\n")

    result = run_tool(
        state,
        "append_to_note",
        {"path": "Note.md", "text": "OMNIA TLDR", "base_revision": content_hash(dirty)},
    )
    assert "error" not in result, result
    action = result["_action"]

    repo = state.vault.repo
    head = repo.head.commit
    assert head.hexsha == action["commit"]
    assert head.author.name == "OMNIA"
    omnia_added = added_lines(repo, head.parents[0], head, "Note.md")
    assert any("OMNIA TLDR" in line for line in omnia_added)
    assert not any("USER PARAGRAPH" in line for line in omnia_added)
    autosave = head.parents[0]
    assert autosave.message.strip() == "autosave"
    assert autosave.author.name == "user"
    assert "USER PARAGRAPH" in (autosave.tree / "Note.md").data_stream.read().decode()

    undo(state, action)
    body = state.store.read("Note.md")
    assert "USER PARAGRAPH" in body
    assert "OMNIA TLDR" not in body


def test_clean_tree_adds_no_autosave_commit(acting_state):
    state = acting_state
    content = state.store.create("Clean.md", "tidy\n")
    asyncio.run(state.vault.commit_all("seed"))
    seed = state.vault.repo.head.commit

    result = run_tool(
        state,
        "append_to_note",
        {"path": "Clean.md", "text": "more", "base_revision": content_hash(content)},
    )
    assert state.vault.repo.head.commit.hexsha == result["_action"]["commit"]
    assert state.vault.repo.head.commit.parents[0] == seed


def test_delete_of_a_note_created_since_the_last_autosave_is_undoable(acting_state):
    state = acting_state
    content = state.store.create("Fresh.md", "brand new thought\n")
    asyncio.run(state.indexer.index_note("Fresh.md", content))

    result = run_tool(
        state,
        "delete_note",
        {"path": "Fresh.md", "base_revision": content_hash(content)},
        confirmed=True,
    )
    assert "error" not in result, result
    action = result["_action"]
    assert action["commit"] and action["undoable"]
    assert not state.store.exists("Fresh.md")

    undo(state, action)
    assert state.store.read("Fresh.md") == content
    assert state.trash.list() == []


def test_rename_of_a_note_created_since_the_last_autosave_is_undoable(acting_state):
    state = acting_state
    content = state.store.create("Untitled.md", "draft thoughts\n")
    asyncio.run(state.indexer.index_note("Untitled.md", content))

    result = run_tool(
        state,
        "rename_note",
        {
            "path": "Untitled.md",
            "new_title": "Proper Title",
            "base_revision": content_hash(content),
        },
    )
    assert "error" not in result, result
    action = result["_action"]
    assert action["commit"] and action["undoable"]
    assert state.store.exists("Proper Title.md")
    assert not state.store.exists("Untitled.md")

    undo(state, action)
    assert not state.store.exists("Proper Title.md")
    assert "draft thoughts" in state.store.read("Untitled.md")


def test_restore_of_a_trash_entry_made_since_the_last_autosave_is_undoable(acting_state):
    state = acting_state
    content = state.store.create("Keep.md", "keep me\n")
    asyncio.run(state.indexer.index_note("Keep.md", content))
    asyncio.run(state.vault.commit_all("seed"))
    # Deleted through the app, not yet reached by the debounced autosave.
    manifest = state.trash.trash_note("Keep.md")
    asyncio.run(state.indexer.remove_note("Keep.md"))

    result = run_tool(state, "restore_trash", {"item_id": manifest["id"]})
    assert "error" not in result, result
    action = result["_action"]
    assert action["commit"] and action["undoable"]
    assert state.store.read("Keep.md") == content
    assert state.trash.list() == []

    undo(state, action)
    assert not state.store.exists("Keep.md")
    assert [item["id"] for item in state.trash.list()] == [manifest["id"]]


def test_failed_autosave_does_not_block_the_tool(acting_state, caplog):
    state = acting_state
    content = state.store.create("Note.md", "line one\n")
    asyncio.run(state.vault.commit_all("seed"))
    (state.settings.vault_dir / "Elsewhere.md").write_text("typing", encoding="utf-8")

    async def broken(*_args, **_kwargs):
        raise RuntimeError("index.lock exists")

    state.vault.commit_all = broken
    result = run_tool(
        state,
        "append_to_note",
        {"path": "Note.md", "text": "still runs", "base_revision": content_hash(content)},
    )
    assert "error" not in result, result
    assert result["_action"]["commit"]
    assert "still runs" in state.store.read("Note.md")
    assert "autosave before OMNIA" in caplog.text


def test_approving_a_delete_of_a_note_created_since_the_last_autosave(state):
    # Holds whether or not the PendingStore is wired with Trash.
    state.actions = ActionCoordinator(state)
    state.pending.actions = state.actions

    async def run():
        content = state.store.create("Doomed.md", "goodbye\n")
        await state.indexer.index_note("Doomed.md", content)
        change_id = await state.pending.stage("s", "delete", "Doomed.md")

        result = await state.pending.approve(change_id)
        assert result["commit"]
        assert not state.store.exists("Doomed.md")
        omnia = state.vault.repo.commit(result["commit"])
        assert omnia.author.name == "OMNIA"
        autosave = omnia.parents[0]
        assert autosave.message.strip() == "autosave"
        assert (autosave.tree / "Doomed.md").data_stream.read().decode() == content

        (action,) = state.actions.list()
        await state.actions.undo(action["id"], "s")
        assert state.store.read("Doomed.md") == content

    asyncio.run(run())
