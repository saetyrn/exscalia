"""Confirmation cards say what they approve, and what the user did with a card
or an Undo reaches the model on the thread's next turn.

The card is the one gate between the model's output and a destructive or
persistent change. find_replace_across_vault, bulk_edit_tasks, merge_notes and
schedule_agent_run all rendered a bare "Find replace across vault?" and the
like, and a bulk card gave only a count. And approving, cancelling or undoing
changed the transcript only, so the model's last word on its own call stayed
"confirmation required".
"""
import asyncio
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.api import overseer as overseer_api
from app.api.overseer import _run_thread_chat
from app.index.db import content_hash
from app.overseer.actions import DETAIL_CHARS, DETAIL_LINES, ActionCoordinator, confirmation_card
from app.overseer.agent import Overseer
from app.overseer.history import user_turn_starts
from app.overseer.provider import FakeProvider, ProviderReply, ToolCall
from app.overseer.threads import ThreadStore
from app.overseer.tools import TOOLS, execute_tool, needs_confirmation
from app.vault.internal import atomic_json, iso_utc, utc_now
from app.vault.notes import NoteError


def tool_reply(calls: list[tuple[str, dict]]) -> ProviderReply:
    tool_calls = [
        ToolCall(id=f"t{i}", name=name, input=inp) for i, (name, inp) in enumerate(calls, 1)
    ]
    return ProviderReply(
        text="",
        tool_calls=tool_calls,
        stop_reason="tool_use",
        raw_content=[
            {"type": "tool_use", "id": c.id, "name": c.name, "input": c.input} for c in tool_calls
        ],
    )


def reply(text: str) -> ProviderReply:
    return ProviderReply(text=text, stop_reason="end_turn")


def card_text(card: dict) -> str:
    return "\n".join([card["summary"], *card["details"]])


# --- what a card says ---------------------------------------------------------


def test_every_confirmed_tool_names_what_it_would_act_on():
    """Required fields filled with sentinels (every field, for a tool with none
    required): the card has to show at least one of them."""
    swept = []
    for tool, _ in TOOLS:
        if tool.mode != "confirmed":
            continue
        schema = tool.input_schema
        fields = schema.get("required") or list(schema.get("properties") or {})
        inputs = {field: f"SENTINEL-{field}" for field in fields}
        summary, details = confirmation_card(tool.name, inputs)
        shown = "\n".join([summary, *details])
        assert any(value in shown for value in inputs.values()), (tool.name, shown)
        swept.append(tool.name)
    blind = {"find_replace_across_vault", "bulk_edit_tasks", "merge_notes", "schedule_agent_run"}
    assert blind <= set(swept)


def test_find_replace_shows_both_strings_and_the_real_case_flag():
    summary, _ = confirmation_card("find_replace_across_vault", {"find": "TODO", "replace": "DONE"})
    assert summary == "Replace “TODO” with “DONE” in every note (case-sensitive)?"
    summary, _ = confirmation_card(
        "find_replace_across_vault", {"find": " ", "replace": "", "case_sensitive": False}
    )
    assert summary == "Remove every “ ” in every note (case-insensitive)?"


def test_bulk_task_edit_spells_out_delete_ahead_of_the_filter():
    summary, _ = confirmation_card(
        "bulk_edit_tasks", {"text": "x" * 400, "include_completed": True, "delete": True}
    )
    assert summary.startswith("Bulk edit tasks: DELETE (")
    summary, _ = confirmation_card("bulk_edit_tasks", {"path": "Board.md", "set_due": ""})
    assert summary == "Bulk edit tasks: clear the due date (in “Board.md”, open only)?"


def test_bulk_task_card_with_a_status_does_not_claim_open_only():
    """A named status is the whole status filter; status='done' deletes done tasks."""
    summary, _ = confirmation_card(
        "bulk_edit_tasks", {"path": "Board.md", "status": "done", "delete": True}
    )
    assert summary == "Bulk edit tasks: DELETE (status done, in “Board.md”)?"


def test_merge_names_sources_target_and_the_trash():
    summary, _ = confirmation_card("merge_notes", {"sources": ["A.md", "B.md"], "target": "T.md"})
    assert summary == "Merge “A.md”, “B.md” into “T.md” (both go to Trash)?"


def test_a_long_schedule_instruction_is_on_the_card_whole():
    """_summ cuts each traced input at 60 characters, so the trace never showed
    what a schedule would replay every day."""
    instruction = " ".join(f"step {i} read every note and notify me" for i in range(20))
    summary, details = confirmation_card(
        "schedule_agent_run",
        {"instruction": instruction, "frequency": "weekly", "weekday": 2, "at": "08:00"},
    )
    assert summary == "Schedule OMNIA to run every Wednesday at 08:00 with this instruction?"
    assert " ".join(details) == instruction
    assert len(details) <= DETAIL_LINES
    assert all(len(line) <= DETAIL_CHARS for line in details)


def test_bulk_card_lists_each_operation_by_its_target(acting_state):
    acting_state.store.create("A.md", "a")
    acting_state.store.create("B.md", "b")
    provider = FakeProvider(
        [
            tool_reply(
                [
                    ("delete_note", {"path": "A.md", "base_revision": "x"}),
                    ("find_replace_across_vault", {"find": "foo", "replace": "bar"}),
                ]
            ),
            reply("Waiting for approval."),
        ]
    )
    result = asyncio.run(Overseer(provider, acting_state).chat("tidy up"))

    [card] = result["confirmations"]
    assert card["tool"] == "bulk_actions"
    assert card["details"] == [
        "Delete note “A.md”",
        "Replace “foo” with “bar” in every note (case-sensitive)",
    ]
    # The same card as listed while it waits.
    [pending] = acting_state.actions.pending_confirmations()
    assert pending["details"] == card["details"]


# --- dry runs -----------------------------------------------------------------


def test_a_dry_run_needs_no_confirmation():
    call = {"find": "a", "replace": "b"}
    assert needs_confirmation("find_replace_across_vault", call)
    assert not needs_confirmation("find_replace_across_vault", {**call, "dry_run": True})
    assert needs_confirmation("find_replace_across_vault", {**call, "dry_run": False})
    rename = {"from_tag": "a", "to_tag": "b"}
    assert needs_confirmation("rename_tag", rename)
    assert not needs_confirmation("rename_tag", {**rename, "dry_run": True})
    # Only tools whose dry_run is a preview.
    merge = {"sources": ["A.md", "B.md"], "target": "T", "dry_run": True}
    assert needs_confirmation("merge_notes", merge)


def test_a_dry_run_previews_without_a_card_and_changes_nothing(acting_state):
    acting_state.store.create("Note.md", "---\ntitle: Note\n---\n\nfoo here\n")
    before = acting_state.store.read("Note.md")

    out = json.loads(
        asyncio.run(
            execute_tool(
                acting_state,
                "t",
                "find_replace_across_vault",
                {"find": "foo", "replace": "bar", "dry_run": True},
            )
        )
    )

    assert out.get("dry_run") is True and out["note_count"] == 1
    assert "confirmation_required" not in out
    assert acting_state.actions.pending_confirmations() == []
    assert acting_state.store.read("Note.md") == before


# --- outcomes reach the next turn -----------------------------------------------


@pytest.fixture
def chat_state(acting_state, tmp_path: Path):
    state = acting_state
    state.threads = ThreadStore(state.settings.vault_dir, tmp_path)
    state.auth = SimpleNamespace(
        session_for_token=lambda *a, **k: None,
        token_from_request=lambda request: None,
    )
    return state


def request_for(state):
    return SimpleNamespace(app=SimpleNamespace(state=state))


def gated_turn(state, path: str = "Old.md", system_messages: bool = True):
    """One turn that asks to delete `path`, answered with a card; then a
    provider scripted for the next turn. Returns (thread id, card, provider)."""
    state.store.create(path, "---\ntitle: Old\n---\n\nold\n")
    asyncio.run(state.vault.commit_all("seed"))
    revision = content_hash(state.store.read(path))
    provider = FakeProvider(
        [
            tool_reply([("delete_note", {"path": path, "base_revision": revision})]),
            reply("Waiting for your approval."),
            reply("Noted."),
            reply("Noted again."),
        ],
        system_messages=system_messages,
    )
    state.overseer = Overseer(provider, state)
    tid = state.threads.create()["id"]
    first = asyncio.run(_run_thread_chat(state, tid, "delete the old note", None))
    [card] = first["confirmations"]
    return tid, card, provider


def last_user_text(provider: FakeProvider) -> str:
    """The newest real user turn the provider was sent (tool results ride in
    user turns too, as lists of tool_result blocks)."""
    messages = provider.seen[-1]
    turn = messages[user_turn_starts(messages)[-1]]
    content = turn["content"]
    return content if isinstance(content, str) else " ".join(
        str(block.get("text") or "") for block in content if isinstance(block, dict)
    )


def public_user_texts(state, tid: str) -> list[str]:
    return [m["text"] for m in state.threads.get(tid)["messages"] if m["role"] == "user"]


def test_an_approval_reaches_the_next_turn_and_not_the_transcript(chat_state):
    state = chat_state
    tid, card, provider = gated_turn(state)
    summary = card["summary"].removesuffix("?")

    asyncio.run(
        overseer_api.approve_omnia_confirmation(
            request_for(state), card["token"], SimpleNamespace(thread_id=tid)
        )
    )
    assert not state.store.exists("Old.md")
    asyncio.run(_run_thread_chat(state, tid, "now clean up the links", None))

    sent = last_user_text(provider)
    assert sent.startswith("now clean up the links\n\n[system] Since your last reply: ")
    assert f'the user approved "{summary}" (applied)' in sent
    assert public_user_texts(state, tid) == ["delete the old note", "now clean up the links"]
    # One provider turn per question: the note is inside the message.
    assert len(user_turn_starts(state.threads.provider_history(tid))) == 2

    # Delivered once.
    asyncio.run(_run_thread_chat(state, tid, "thanks", None))
    assert last_user_text(provider) == "thanks"


def test_a_cancel_reaches_the_next_turn(chat_state):
    state = chat_state
    tid, card, provider = gated_turn(state)

    asyncio.run(
        overseer_api.cancel_omnia_confirmation(
            request_for(state), card["token"], SimpleNamespace(thread_id=tid)
        )
    )
    asyncio.run(_run_thread_chat(state, tid, "never mind", None))

    assert state.store.exists("Old.md")
    assert f'the user cancelled "{card["summary"].removesuffix("?")}"' in last_user_text(provider)
    assert public_user_texts(state, tid)[-1] == "never mind"


def test_an_undo_reaches_the_next_turn(chat_state):
    state = chat_state
    tid, card, provider = gated_turn(state)
    approved = asyncio.run(
        overseer_api.approve_omnia_confirmation(
            request_for(state), card["token"], SimpleNamespace(thread_id=tid)
        )
    )
    action = approved["action"]

    asyncio.run(
        overseer_api.undo_omnia_action(
            request_for(state), action["id"], SimpleNamespace(thread_id=tid)
        )
    )
    asyncio.run(_run_thread_chat(state, tid, "what now", None))

    sent = last_user_text(provider)
    assert "the user approved" in sent
    assert f'the user undid "{action["summary"]}"' in sent
    assert sent.index("the user approved") < sent.index("the user undid")
    assert public_user_texts(state, tid)[-1] == "what now"


def test_a_failed_approval_says_it_failed(chat_state):
    state = chat_state
    tid, card, provider = gated_turn(state)
    state.store.write("Old.md", "---\ntitle: Old\n---\n\nchanged since\n")

    with pytest.raises(NoteError) as failed:
        asyncio.run(
            overseer_api.approve_omnia_confirmation(
                request_for(state), card["token"], SimpleNamespace(thread_id=tid)
            )
        )
    assert failed.value.status == 409
    asyncio.run(_run_thread_chat(state, tid, "did it work?", None))

    sent = last_user_text(provider)
    assert f'the user approved "{card["summary"].removesuffix("?")}" but it failed: ' in sent
    assert "(applied)" not in sent


def test_a_retried_question_keeps_the_outcomes_its_turn_carried(chat_state):
    """Retry truncates the question's provider turn away with its answer; the
    resend has to carry the approval again."""
    state = chat_state
    tid, card, provider = gated_turn(state)
    asyncio.run(
        overseer_api.approve_omnia_confirmation(
            request_for(state), card["token"], SimpleNamespace(thread_id=tid)
        )
    )
    turn = asyncio.run(_run_thread_chat(state, tid, "now clean up the links", None))
    assert "the user approved" in last_user_text(provider)

    asyncio.run(overseer_api.truncate_thread(request_for(state), tid, turn["user_message"]["id"]))
    asyncio.run(_run_thread_chat(state, tid, "now clean up the links", None))

    sent = last_user_text(provider)
    assert sent.count("the user approved") == 1
    assert sent.startswith("now clean up the links\n\n[system] Since your last reply: ")
    assert len(user_turn_starts(state.threads.provider_history(tid))) == 2


def test_outcomes_ride_beside_a_persisted_turn_context(chat_state):
    """Sonnet, Haiku, OpenAI and Gemini get the turn's context written into the
    user message itself (provider.with_context_block), as a block after the
    typed text. The note stays in the text block: Retry pairs the turn on the
    typed text and re-queues the note without the clock."""
    state = chat_state
    tid, card, provider = gated_turn(state, system_messages=False)
    note = f'the user approved "{card["summary"].removesuffix("?")}" (applied)'
    asyncio.run(
        overseer_api.approve_omnia_confirmation(
            request_for(state), card["token"], SimpleNamespace(thread_id=tid)
        )
    )

    def sent_blocks() -> list[dict]:
        messages = provider.seen[-1]
        return messages[user_turn_starts(messages)[-1]]["content"]

    turn = asyncio.run(_run_thread_chat(state, tid, "now clean up the links", None))
    blocks = sent_blocks()
    assert provider.contexts[-1] is None
    assert blocks[0]["text"] == f"now clean up the links\n\n[system] Since your last reply: {note}."
    assert blocks[-1]["text"].startswith("\n\n## Right now")

    asyncio.run(overseer_api.truncate_thread(request_for(state), tid, turn["user_message"]["id"]))
    asyncio.run(_run_thread_chat(state, tid, "now clean up the links", None))
    blocks = sent_blocks()
    assert blocks[0]["text"] == f"now clean up the links\n\n[system] Since your last reply: {note}."
    assert len(user_turn_starts(state.threads.provider_history(tid))) == 2
    assert public_user_texts(state, tid) == ["delete the old note", "now clean up the links"]


# --- the queue itself -------------------------------------------------------------


def test_outcomes_survive_a_restart_and_expire(acting_state):
    first = acting_state.actions
    note = 'the user approved "Delete note “A.md”" (applied)'
    first.note_outcome("thread-a", note)
    first.note_outcome("thread-b", "for another thread")

    restarted = ActionCoordinator(acting_state)
    assert restarted.drain_outcomes("thread-a") == [note]
    assert restarted.drain_outcomes("thread-a") == []
    # Drained is drained on disk too.
    assert ActionCoordinator(acting_state).drain_outcomes("thread-a") == []

    path = acting_state.settings.data_dir / "omnia-outcomes.json"
    atomic_json(
        path,
        {
            "thread-c": [
                {
                    "text": "a day and more ago",
                    "created_at": iso_utc(utc_now() - timedelta(hours=25)),
                },
                {"text": "just now", "created_at": iso_utc()},
            ]
        },
    )
    assert ActionCoordinator(acting_state).drain_outcomes("thread-c") == ["just now"]


def test_outcomes_are_bounded_per_thread(acting_state):
    actions = acting_state.actions
    for index in range(ActionCoordinator.OUTCOMES_PER_THREAD + 5):
        actions.note_outcome("t", f"outcome {index}")
    drained = actions.drain_outcomes("t")
    assert len(drained) == ActionCoordinator.OUTCOMES_PER_THREAD
    assert drained[-1] == f"outcome {ActionCoordinator.OUTCOMES_PER_THREAD + 4}"
