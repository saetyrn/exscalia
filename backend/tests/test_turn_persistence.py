"""A card confirmed while its reply is still being written stays confirmed.

The agent loop carries on after a gated call, so a confirmation card is live —
and approvable — for the rest of the turn. The approve endpoint writes the
card's status and the change it made onto the stored message; the turn's own
writes (the throttled partial flush and the final save) used to put their
older copies straight back over it.
"""
import asyncio
import json
from types import SimpleNamespace

from app.api import overseer as overseer_api
from app.index.db import content_hash
from app.overseer.actions import ActionCoordinator
from app.overseer.agent import Overseer
from app.overseer.provider import FakeProvider, ProviderReply, ToolCall
from app.overseer.threads import ThreadStore


def tool_use_reply(name: str, input: dict, call_id: str = "t1") -> ProviderReply:
    return ProviderReply(
        text="",
        tool_calls=[ToolCall(id=call_id, name=name, input=input)],
        stop_reason="tool_use",
        raw_content=[{"type": "tool_use", "id": call_id, "name": name, "input": input}],
    )


def request_for(state):
    return SimpleNamespace(app=SimpleNamespace(state=state))


def omnia_state(state, tmp_path, script: list[ProviderReply]):
    state.threads = ThreadStore(state.settings.vault_dir, tmp_path / "data")
    state.actions = ActionCoordinator(state)
    # No signed-in device: the card is requested and approved with no session.
    state.auth = SimpleNamespace(
        token_from_request=lambda request: None,
        session_for_token=lambda token, touch=False: None,
    )
    state.overseer = Overseer(FakeProvider(script), state)
    return state


def fixture_note(state, path: str) -> str:
    content = state.store.create(path, "keep until approved")
    asyncio.run(state.indexer.index_note(path, content))
    asyncio.run(state.vault.commit_paths("Add confirmation fixture", [path]))
    return content_hash(state.store.read(path))


def stored_reply(state, thread_id: str) -> dict:
    return state.threads.get(thread_id)["messages"][-1]


def test_mid_turn_approval_survives_the_rest_of_the_turn(state, tmp_path, monkeypatch):
    revision = fixture_note(state, "Confirm delete.md")
    state = omnia_state(
        state,
        tmp_path,
        [
            tool_use_reply(
                "delete_note", {"path": "Confirm delete.md", "base_revision": revision}
            ),
            ProviderReply(text="Asked you to confirm that.", stop_reason="end_turn"),
        ],
    )
    # Every delta flushes, so the partial write runs after the approval too.
    monkeypatch.setattr(overseer_api, "PERSIST_INTERVAL", 0)
    thread_id = state.threads.create()["id"]
    approved: list[dict] = []
    # The stored reply as each post-approval delta left it — before the final
    # save, so the partial flush is checked on its own.
    after_flush: list[dict] = []

    async def on_event(event: str, value: dict):
        if event == "confirmation_required":
            # What the Confirm button does, while the turn is still running.
            approved.append(
                await overseer_api.approve_omnia_confirmation(
                    request_for(state),
                    value["token"],
                    overseer_api.OmniaActionBody(thread_id=thread_id),
                )
            )
        elif event == "delta" and approved:
            after_flush.append(json.loads(json.dumps(stored_reply(state, thread_id))))

    result = asyncio.run(
        overseer_api._run_thread_chat(
            state, thread_id, "delete it", None, event_callback=on_event
        )
    )

    assert approved and not state.store.exists("Confirm delete.md")
    action_id = approved[0]["actions"][0]["id"]
    assert after_flush
    for reply in after_flush:
        assert reply["confirmations"][0]["status"] == "approved"
        assert [action["id"] for action in reply["actions"]] == [action_id]

    reply = stored_reply(state, thread_id)
    assert reply["status"] == "complete"
    assert reply["confirmations"][0]["status"] == "approved"
    assert [action["id"] for action in reply["actions"]] == [action_id]
    assert reply["actions"][0]["undoable"]
    # The `done` event carries this copy, so it must agree with the disk.
    assert result["assistant_message"]["confirmations"][0]["status"] == "approved"
    assert result["user_message"]["id"] == state.threads.get(thread_id)["messages"][0]["id"]


def test_mid_turn_undo_survives_the_rest_of_the_turn(state, tmp_path, monkeypatch):
    state = omnia_state(
        state,
        tmp_path,
        [
            tool_use_reply(
                "create_note", {"path": "Made by OMNIA.md", "content": "hello"}
            ),
            ProviderReply(text="Made it.", stop_reason="end_turn"),
        ],
    )
    monkeypatch.setattr(overseer_api, "PERSIST_INTERVAL", 0)
    thread_id = state.threads.create()["id"]
    undone: list[str] = []

    async def on_event(event: str, value: dict):
        if event == "action":
            await overseer_api.undo_omnia_action(
                request_for(state),
                value["id"],
                overseer_api.OmniaActionBody(thread_id=thread_id),
            )
            undone.append(value["id"])

    asyncio.run(
        overseer_api._run_thread_chat(
            state, thread_id, "make a note", None, event_callback=on_event
        )
    )

    assert undone and not state.store.exists("Made by OMNIA.md")
    [action] = stored_reply(state, thread_id)["actions"]
    assert action["id"] == undone[0]
    assert action["undone"] is True and action["undoable"] is False


def test_done_event_carries_the_stored_question(state, tmp_path):
    state = omnia_state(
        state, tmp_path, [ProviderReply(text="Hello.", stop_reason="end_turn")]
    )
    thread_id = state.threads.create()["id"]

    async def frames():
        response = await overseer_api.stream_thread_message(
            request_for(state), thread_id, overseer_api.ThreadMessage(message="hi")
        )
        return [frame async for frame in response.body_iterator]

    done = next(frame for frame in asyncio.run(frames()) if frame.startswith("event: done\n"))
    payload = json.loads(done.split("data: ", 1)[1])
    question, answer = state.threads.get(thread_id)["messages"]
    assert payload["user_message"]["id"] == question["id"]
    assert payload["user_message"]["text"] == "hi"
    assert payload["message"]["id"] == answer["id"]
