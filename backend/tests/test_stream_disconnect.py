"""An OMNIA turn belongs to the server, not to the SSE reader that started it.

Every chat surface aborts its fetch when it unmounts mid-turn, and a closed
popover used to kill a multi-tool request half-applied. Only Stop may end a
run. Starlette's TestClient delivers http.disconnect only after the response
has finished, so these drive the endpoint's generator directly and close it
the way a disconnect does.
"""
import asyncio
from types import SimpleNamespace

from app.api import overseer as overseer_api
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


class SlowProvider(FakeProvider):
    """The second round takes long enough to hang up in the middle of it."""

    def __init__(self, script: list[ProviderReply]):
        super().__init__(script)
        self.rounds = 0

    async def chat(self, *args, **kwargs):
        self.rounds += 1
        if self.rounds > 1:
            await asyncio.sleep(0.3)
        return await super().chat(*args, **kwargs)


def omnia_state(state, tmp_path):
    state.threads = ThreadStore(state.settings.vault_dir, tmp_path / "data")
    state.actions = ActionCoordinator(state)
    state.auth = SimpleNamespace(
        token_from_request=lambda request: None,
        session_for_token=lambda token, touch=False: None,
    )
    state.overseer = Overseer(
        SlowProvider(
            [
                tool_use_reply("read_structure", {}),
                ProviderReply(text="All done.", stop_reason="end_turn"),
            ]
        ),
        state,
    )
    return state


def request_for(state):
    return SimpleNamespace(app=SimpleNamespace(state=state))


async def open_stream(state):
    thread_id = state.threads.create()["id"]
    response = await overseer_api.stream_thread_message(
        request_for(state), thread_id, overseer_api.ThreadMessage(message="go")
    )
    return thread_id, response.body_iterator


async def read_through_tool(frames) -> list[str]:
    seen = []
    async for frame in frames:
        seen.append(frame)
        if frame.startswith("event: tool\n"):
            return seen
    raise AssertionError(f"stream ended before the tool finished: {seen}")


async def wait_idle(state, thread_id: str) -> None:
    for _ in range(150):
        if not state.threads.is_running(thread_id):
            await asyncio.sleep(0)  # let the done callbacks run
            return
        await asyncio.sleep(0.02)
    raise AssertionError("the run never finished")


def test_turn_outlives_its_reader(state, tmp_path):
    state = omnia_state(state, tmp_path)

    async def scenario():
        thread_id, frames = await open_stream(state)
        await read_through_tool(frames)
        # One live task per turn; ThreadStore keeps them as a list per thread.
        (run,) = state.threads._runs[thread_id]
        # What a closed chat surface does to the stream.
        await frames.aclose()

        assert state.threads.is_running(thread_id)
        assert run in overseer_api._detached_runs
        await wait_idle(state, thread_id)
        assert run not in overseer_api._detached_runs
        assert thread_id not in state.threads._runs
        return state.threads.get(thread_id)["messages"][-1]

    reply = asyncio.run(scenario())
    assert reply["status"] == "complete"
    assert reply["text"] == "All done."
    assert [row["tool"] for row in reply["trace"]] == ["read_structure"]


def test_stop_still_interrupts_the_turn(state, tmp_path):
    state = omnia_state(state, tmp_path)

    async def scenario():
        thread_id, frames = await open_stream(state)
        await read_through_tool(frames)
        stopped = await overseer_api.stop_thread_generation(request_for(state), thread_id)
        assert stopped == {"stopped": True}
        # The reader is released rather than left waiting on a dead run.
        rest = [frame async for frame in frames]
        assert not any(frame.startswith("event: done\n") for frame in rest)
        await wait_idle(state, thread_id)
        return state.threads.get(thread_id)["messages"][-1]

    reply = asyncio.run(scenario())
    assert reply["status"] == "interrupted"
    assert reply["text"] == "Response stopped."


def test_quiet_round_sends_keepalive_comments(state, tmp_path, monkeypatch):
    state = omnia_state(state, tmp_path)
    monkeypatch.setattr(overseer_api, "KEEPALIVE_SECONDS", 0.05)

    async def scenario():
        thread_id, frames = await open_stream(state)
        return thread_id, [frame async for frame in frames]

    thread_id, frames = asyncio.run(scenario())
    assert ": ping\n\n" in frames
    # A timed-out wait must not swallow the event that lands as it expires.
    assert frames[-1].startswith("event: done\n")
    reply = state.threads.get(thread_id)["messages"][-1]
    assert reply["status"] == "complete"
    assert reply["text"] == "All done."
