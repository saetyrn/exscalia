"""Guards on approving staged OMNIA changes: a chain of proposals is only as
fresh as its first link, a proposal whose note was renamed or deleted can't
re-create it, and a save that lands mid-approval is never overwritten."""
import asyncio

import pytest

from app.vault.notes import NoteError


def approve_error(state, change_id, force=False) -> NoteError:
    with pytest.raises(NoteError) as exc:
        asyncio.run(state.pending.approve(change_id, force=force))
    assert exc.value.status == 409
    return exc.value


def described(state):
    async def run():
        changes = await state.pending.list_changes()
        return changes, [await state.pending.describe(c, changes) for c in changes]

    return asyncio.run(run())


def test_user_edit_under_a_chain_holds_back_the_later_link(state):
    state.store.create("A.md", "---\ntitle: A\n---\n\noriginal")
    asyncio.run(state.pending.stage("s", "edit", "A.md", new_content="one"))
    asyncio.run(state.pending.stage("s", "edit", "A.md", new_content="two"))
    state.store.write("A.md", "---\ntitle: A\n---\n\nuser version")

    changes, rows = described(state)
    # #2's base is #1's content, so only the chain shows the user edit.
    assert asyncio.run(state.pending.is_stale(changes[1]))
    assert rows[1]["stale"] is True
    assert rows[1]["blocked_by"] == [changes[0].id]
    assert rows[0]["blocked_by"] == []
    err = approve_error(state, changes[1].id)
    assert f"Approve #{changes[0].id} first" in str(err.detail)
    assert "user version" in state.store.read("A.md")

    # Resolving the chain in order works: force the first after review, and
    # the second now sits on exactly what it was proposed against.
    asyncio.run(state.pending.approve(changes[0].id, force=True))
    asyncio.run(state.pending.approve(changes[1].id))
    assert "two" in state.store.read("A.md")
    assert asyncio.run(state.pending.list_changes()) == []


def test_later_link_waits_for_the_earlier_one_even_when_nothing_is_stale(state):
    state.store.create("A.md", "---\ntitle: A\n---\n\noriginal")
    asyncio.run(state.pending.stage("s", "edit", "A.md", new_content="one"))
    asyncio.run(state.pending.stage("s", "edit", "A.md", new_content="two"))
    changes = asyncio.run(state.pending.list_changes())
    assert not asyncio.run(state.pending.is_stale(changes[1]))

    # Approving #2 alone would land #1's unreviewed text with it.
    err = approve_error(state, changes[1].id)
    assert f"Approve #{changes[0].id} first" in str(err.detail)
    assert "original" in state.store.read("A.md")

    asyncio.run(state.pending.approve(changes[0].id))
    asyncio.run(state.pending.approve(changes[1].id))
    assert "two" in state.store.read("A.md")


def test_chain_follows_a_staged_move(state):
    state.store.create("A.md", "---\ntitle: A\n---\n\noriginal")
    asyncio.run(state.pending.stage("s", "edit", "A.md", new_content="one"))
    asyncio.run(state.pending.stage("s", "move", "A.md", new_path="B.md"))
    asyncio.run(state.pending.stage("s", "edit", "B.md", new_content="two"))
    state.store.create("Other.md", "unrelated")
    asyncio.run(state.pending.stage("s", "edit", "Other.md", new_content="other"))

    changes, rows = described(state)
    assert rows[1]["blocked_by"] == [changes[0].id]
    assert rows[2]["blocked_by"] == [changes[0].id, changes[1].id]
    assert rows[3]["blocked_by"] == []
    assert not any(r["stale"] for r in rows)
    assert not any(r["missing"] for r in rows)

    state.store.write("A.md", "---\ntitle: A\n---\n\nuser version")
    changes, rows = described(state)
    # The move's own base is #1's content: only the chain sees the user edit.
    assert [r["stale"] for r in rows] == [True, True, True, False]


def test_create_after_a_staged_delete_waits_for_it(state):
    state.store.create("A.md", "---\ntitle: A\n---\n\noriginal")
    asyncio.run(state.pending.stage("s", "delete", "A.md"))
    asyncio.run(state.pending.stage("s", "create", "A.md", new_content="fresh"))
    changes, rows = described(state)
    assert rows[1]["blocked_by"] == [changes[0].id]
    assert rows[1]["missing"] is False
    approve_error(state, changes[1].id)
    assert "original" in state.store.read("A.md")


@pytest.mark.parametrize(
    "op, extra",
    [
        ("edit", {"new_content": "ai version"}),
        ("delete", {}),
        ("move", {"new_path": "Moved.md"}),
    ],
)
def test_renamed_note_refuses_even_forced_approval(state, op, extra):
    state.store.create("Draft.md", "---\ntitle: Draft\n---\n\nbody")
    asyncio.run(state.vault.commit_all("seed"))
    asyncio.run(state.pending.stage("s", op, "Draft.md", **extra))
    state.store.rename("Draft.md", "Final.md")
    final = state.store.read("Final.md")

    (change,), (row,) = described(state)
    assert row["missing"] is True
    assert row["stale"] is True
    err = approve_error(state, change.id, force=True)
    assert "renamed or deleted" in str(err.detail)
    assert not state.store.exists("Draft.md")
    assert not state.store.exists("Moved.md")
    assert state.store.read("Final.md") == final
    # Still in the tray, for the user to discard.
    assert [c.id for c in asyncio.run(state.pending.list_changes())] == [change.id]


def test_approve_all_refuses_up_front_when_a_note_is_gone(state):
    keep = state.store.create("Keep.md", "keep")
    state.store.create("Draft.md", "draft")
    asyncio.run(state.pending.stage("s", "edit", "Keep.md", new_content="kept"))
    asyncio.run(state.pending.stage("s", "edit", "Draft.md", new_content="drafted"))
    state.store.rename("Draft.md", "Final.md")

    with pytest.raises(NoteError) as exc:
        asyncio.run(state.pending.approve_all(force=True))
    assert exc.value.status == 409
    # Nothing landed halfway: the valid change is still staged, not applied.
    assert state.store.read("Keep.md") == keep
    assert not state.store.exists("Draft.md")
    assert len(asyncio.run(state.pending.list_changes())) == 2


def test_save_landing_during_approval_is_not_overwritten(state, monkeypatch):
    state.store.create("B.md", "---\ntitle: B\n---\n\noriginal")
    change_id = asyncio.run(
        state.pending.stage("s", "edit", "B.md", new_content="ai version")
    )
    fetch = state.pending.list_changes

    async def fetch_then_user_saves():
        changes = await fetch()
        state.store.write("B.md", "---\ntitle: B\n---\n\nuser typed more")
        return changes

    monkeypatch.setattr(state.pending, "list_changes", fetch_then_user_saves)
    err = approve_error(state, change_id)
    assert "stale" in str(err.detail)
    assert "user typed more" in state.store.read("B.md")


def test_save_landing_between_approve_all_links_is_not_overwritten(state, monkeypatch):
    state.store.create("A.md", "---\ntitle: A\n---\n\noriginal")
    asyncio.run(state.pending.stage("s", "edit", "A.md", new_content="one"))
    asyncio.run(state.pending.stage("s", "edit", "A.md", new_content="two"))
    index_note = state.indexer.index_note

    async def index_then_user_saves(path, content):
        await index_note(path, content)
        if "one" in content:
            state.store.write("A.md", "---\ntitle: A\n---\n\nuser typed more")

    monkeypatch.setattr(state.indexer, "index_note", index_then_user_saves)
    with pytest.raises(NoteError) as exc:
        asyncio.run(state.pending.approve_all())
    assert exc.value.status == 409
    assert "user typed more" in state.store.read("A.md")


def test_clean_chain_approves_all_in_order(state):
    asyncio.run(state.pending.stage("s", "create", "New.md", new_content="first"))
    asyncio.run(state.pending.stage("s", "edit", "New.md", new_content="second"))
    state.store.create("Old.md", "---\ntitle: Old\n---\n\nbefore")
    asyncio.run(state.pending.stage("s", "edit", "Old.md", new_content="after one"))
    asyncio.run(state.pending.stage("s", "edit", "Old.md", new_content="after two"))

    changes, rows = described(state)
    # A note the chain itself creates is not "missing".
    assert [r["missing"] for r in rows] == [False, False, False, False]
    assert [r["blocked_by"] for r in rows] == [
        [],
        [changes[0].id],
        [],
        [changes[2].id],
    ]
    result = asyncio.run(state.pending.approve_all())
    assert len(result["applied"]) == 4
    assert "second" in state.store.read("New.md")
    assert "after two" in state.store.read("Old.md")
    assert asyncio.run(state.pending.list_changes()) == []


def test_describe_agrees_with_is_stale(state):
    state.store.create("A.md", "---\ntitle: A\n---\n\noriginal")
    state.store.create("Draft.md", "draft")
    state.store.create("C.md", "c")

    async def run():
        await state.pending.stage("s", "edit", "A.md", new_content="one")
        await state.pending.stage("s", "edit", "A.md", new_content="two")
        await state.pending.stage("s", "create", "N.md", new_content="n")
        await state.pending.stage("s", "edit", "N.md", new_content="n2")
        await state.pending.stage("s", "delete", "Draft.md")
        await state.pending.stage("s", "move", "C.md", new_path="D.md")
        for step in range(3):
            if step == 1:
                state.store.write("A.md", "---\ntitle: A\n---\n\nuser version")
            if step == 2:
                state.store.rename("Draft.md", "Final.md")
            changes = await state.pending.list_changes()
            for c in changes:
                row = await state.pending.describe(c, changes)
                assert row["stale"] == await state.pending.is_stale(c), (step, c)
                assert row["stale"] == await state.pending.is_stale(c, changes)
                # describe() without the prefetched list sees the same thing.
                assert await state.pending.describe(c) == row
                if row["missing"]:
                    assert row["stale"]

    asyncio.run(run())
