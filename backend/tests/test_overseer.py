"""Offline tests: overseer tool loop + pending-changes lifecycle, using the
scripted FakeProvider (no API key, no network)."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.index.db import open_db
from app.index.indexer import Indexer
from app.overseer.agent import Overseer
from app.overseer.provider import FakeProvider, ProviderReply, ToolCall
from app.pending.store import PendingStore
from app.vault.notes import NoteError, NoteStore
from app.vault.repo import VaultRepo


def tool_use_reply(name: str, input: dict, call_id: str = "t1") -> ProviderReply:
    return ProviderReply(
        text="",
        tool_calls=[ToolCall(id=call_id, name=name, input=input)],
        stop_reason="tool_use",
        raw_content=[{"type": "tool_use", "id": call_id, "name": name, "input": input}],
    )


def test_agent_stages_create_and_approve(state):
    provider = FakeProvider(
        [
            tool_use_reply(
                "create_note",
                {
                    "path": "ideas/From AI.md",
                    "content": "---\ntags: [ai]\n---\n\nStaged by the overseer.",
                    "rationale": "test",
                },
            ),
            ProviderReply(text="Staged [[From AI]] for review.", stop_reason="end_turn"),
        ]
    )
    overseer = Overseer(provider, state)
    result = asyncio.run(overseer.chat("Create an idea note"))

    assert result["pending_count"] == 1
    assert "review" in result["reply"].lower()
    assert result["trace"][0]["tool"] == "create_note"
    # Nothing on disk yet — staged only.
    assert not state.store.exists("ideas/From AI.md")

    changes = asyncio.run(state.pending.list_changes())
    assert changes[0].op == "create"
    outcome = asyncio.run(state.pending.approve(changes[0].id))
    assert outcome["commit"]
    assert state.store.exists("ideas/From AI.md")
    content = state.store.read("ideas/From AI.md")
    assert "Staged by the overseer" in content
    assert "created:" in content  # frontmatter normalized
    # Commit author is OMNIA, distinct from the user.
    last = state.vault.repo.head.commit
    assert last.author.name == "OMNIA"


def test_agent_streams_tool_activity_and_text_deltas(state):
    provider = FakeProvider(
        [
            tool_use_reply("read_notes", {"paths": ["Welcome.md"]}),
            ProviderReply(text="Finished reading.", stop_reason="end_turn"),
        ]
    )
    overseer = Overseer(provider, state)
    events: list[tuple[str, dict]] = []

    async def run():
        async def capture(event: str, value: dict):
            events.append((event, value))

        return await overseer.chat("Read the welcome note", event_callback=capture)

    result = asyncio.run(run())
    assert next(event for event, _value in events if event == "tool") == "tool"
    assert "".join(
        value["text"] for event, value in events if event == "delta"
    ) == "Finished reading."
    assert result["reply"] == "Finished reading."

    # A slow tool used to show nothing until it finished. tool_start announces
    # the call as it starts and must precede the "tool" event that reports it.
    names = [event for event, _value in events if event in ("tool_start", "tool")]
    assert names == ["tool_start", "tool"]
    started = next(value for event, value in events if event == "tool_start")
    # The payload contract the client reads: name and a summarized input.
    assert set(started) == {"tool", "summary"}
    assert started["tool"] == "read_notes"
    assert "Welcome.md" in started["summary"]


def test_tool_start_is_live_only_and_never_lands_in_the_stored_trace(
    tmp_path, monkeypatch
):
    """It rides the SSE stream beside the "tool" event that follows it. Persist
    both and every tool row doubles in the transcript on the next reload."""
    import json

    from fastapi.testclient import TestClient

    from app import main as main_module
    from app.config import Settings

    settings = Settings(
        data_dir=tmp_path / "app-data",
        frontend_dir=Path(__file__).resolve().parents[2] / "frontend" / "build",
        embedding_backend="none",
        autocommit_debounce=0.01,
        auth_password=None,
        anthropic_api_key=None,
        openai_api_key=None,
        google_api_key=None,
        backup_remote=None,
    )
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)

    with TestClient(main_module.app) as client:
        app_state = client.app.state
        thread_id = client.post("/api/omnia/threads").json()["id"]
        mid_turn: list[list[dict]] = []

        class PeekingProvider(FakeProvider):
            """Reads the transcript back between rounds — the only moment the
            partial trace is visible, since the final write replaces it."""

            async def chat(self, *args, **kwargs):
                message = app_state.threads.get(thread_id)["messages"][-1]
                mid_turn.append(list(message.get("trace") or []))
                return await super().chat(*args, **kwargs)

        app_state.overseer = Overseer(
            PeekingProvider(
                [
                    tool_use_reply("read_structure", {}),
                    ProviderReply(text="Nothing unusual.", stop_reason="end_turn"),
                ]
            ),
            app_state,
        )

        body = client.post(
            f"/api/omnia/threads/{thread_id}/messages",
            json={"message": "what is in my vault?"},
        ).text
        events = [
            (
                block.split("\n")[0].removeprefix("event: "),
                json.loads(block.split("\ndata: ", 1)[1]),
            )
            for block in body.strip().split("\n\n")
        ]

        # Live: the start reaches the client, ahead of the completed row.
        assert [name for name, _ in events if name.startswith("tool")] == [
            "tool_start",
            "tool",
        ]
        assert dict(events)["tool_start"]["tool"] == "read_structure"

        # Persisted: exactly one row, mid-turn and at rest alike.
        assert [row["tool"] for row in mid_turn[-1]] == ["read_structure"]
        stored = client.get(f"/api/omnia/threads/{thread_id}").json()["messages"][-1]
        assert [row["tool"] for row in stored["trace"]] == ["read_structure"]


def test_agent_reads_own_staged_state(state):
    provider = FakeProvider(
        [
            tool_use_reply("create_note", {"path": "A.md", "content": "first"}, "t1"),
            tool_use_reply("edit_note", {"path": "A.md", "new_content": "second"}, "t2"),
            tool_use_reply("read_notes", {"paths": ["A.md"]}, "t3"),
            ProviderReply(text="done", stop_reason="end_turn"),
        ]
    )
    overseer = Overseer(provider, state)
    asyncio.run(overseer.chat("chain"))
    # The read_notes tool_result (last tool_result message) must reflect the staged edit.
    tool_results = [
        m for m in provider.seen[-1] if m["role"] == "user" and isinstance(m["content"], list)
    ]
    assert "second" in tool_results[-1]["content"][0]["content"]
    # Chained edit is NOT stale (base = overlay, not working tree).
    changes = asyncio.run(state.pending.list_changes())
    assert [c.op for c in changes] == ["create", "edit"]
    assert not asyncio.run(state.pending.is_stale(changes[1]))
    # Approve-all applies in order: file ends up with the edit.
    asyncio.run(state.pending.approve_all())
    assert "second" in state.store.read("A.md")


def test_stale_detection_on_user_edit(state):
    state.store.create("B.md", "---\ntitle: B\n---\n\noriginal")
    provider = FakeProvider(
        [
            tool_use_reply("edit_note", {"path": "B.md", "new_content": "ai version"}),
            ProviderReply(text="staged", stop_reason="end_turn"),
        ]
    )
    overseer = Overseer(provider, state)
    asyncio.run(overseer.chat("edit B"))

    # User edits the same file after the proposal.
    state.store.write("B.md", "---\ntitle: B\n---\n\nuser version")

    changes = asyncio.run(state.pending.list_changes())
    assert asyncio.run(state.pending.is_stale(changes[0]))
    with pytest.raises(NoteError) as exc:
        asyncio.run(state.pending.approve(changes[0].id))
    assert exc.value.status == 409
    # Force overrides after re-review.
    outcome = asyncio.run(state.pending.approve(changes[0].id, force=True))
    assert "edit B.md" in outcome["applied"]


def test_memory_note_injected_into_system_prompt(acting_state):
    from app.overseer.tool_expansion import remember

    state = acting_state
    state.store.create(
        "omnia/memory.md",
        "---\ntitle: OMNIA Memory\n---\n\n## About the user\n\nPrefers short summaries.",
    )
    provider = FakeProvider([ProviderReply(text="ok", stop_reason="end_turn")])
    overseer = Overseer(provider, state)
    asyncio.run(overseer.chat("hi"))
    system = provider.systems[-1]
    assert "Prefers short summaries." in system
    assert "omnia/memory.md" in system
    # A staged (unapproved) whole-file edit stays out of the prompt — the
    # prompt reads committed memory only — and no longer hides a remember()
    # written after it.
    asyncio.run(
        state.pending.stage(
            "s1",
            "edit",
            "omnia/memory.md",
            new_content="---\ntitle: OMNIA Memory\n---\n\nWorks in Go.",
        )
    )
    asyncio.run(remember(state, "s1", {"text": "Works in Rust."}))
    asyncio.run(overseer.chat("hi again"))
    assert "Works in Go." not in provider.systems[-1]
    assert "Works in Rust." in provider.systems[-1]
    assert "Prefers short summaries." in provider.systems[-1]


def test_show_graphic_collected_and_ack_minimal(state):
    provider = FakeProvider(
        [
            tool_use_reply(
                "show_graphic",
                {
                    "type": "bar",
                    "title": "Notes per tag",
                    "x_labels": ["daily", "ideas"],
                    "series": [{"name": "notes", "values": [4, 2]}],
                },
            ),
            ProviderReply(text="Here's the chart.", stop_reason="end_turn"),
        ]
    )
    overseer = Overseer(provider, state)
    result = asyncio.run(overseer.chat("chart my tags"))
    assert len(result["graphics"]) == 1
    spec = result["graphics"][0]
    assert spec["type"] == "bar"
    assert spec["series"][0]["values"] == [4.0, 2.0]
    # The model got a bare ack, not its own spec echoed back.
    tool_results = [
        m for m in provider.seen[-1] if m["role"] == "user" and isinstance(m["content"], list)
    ]
    assert "_spec" not in tool_results[-1]["content"][0]["content"]
    # Nothing staged — graphics are ephemeral.
    assert result["pending_count"] == 0


def test_show_graphic_invalid_input_errors(state):
    provider = FakeProvider(
        [
            tool_use_reply("show_graphic", {"type": "heatmap"}),  # missing cells
            ProviderReply(text="oops", stop_reason="end_turn"),
        ]
    )
    overseer = Overseer(provider, state)
    result = asyncio.run(overseer.chat("chart"))
    assert result["graphics"] == []
    tool_results = [
        m for m in provider.seen[-1] if m["role"] == "user" and isinstance(m["content"], list)
    ]
    assert "error" in tool_results[-1]["content"][0]["content"]


def test_persona_injected_and_reinjected_when_long(state):
    provider = FakeProvider(
        [
            ProviderReply(text="ok", stop_reason="end_turn"),
            ProviderReply(text="ok", stop_reason="end_turn"),
        ]
    )
    overseer = Overseer(provider, state)
    result = asyncio.run(overseer.chat("hi"))
    system = provider.systems[-1]
    # Load-bearing persona phrases, verbatim.
    assert "Ending flatly is correct" in system
    assert "ask if the answer changes future behavior" in system
    # Length must follow the question: brevity governs how a reply ends, not
    # how much substance it carries. Guard both halves against drift.
    assert "Length follows the question" in system
    assert "a one-line reply to a genuine question is a failure" in system
    assert "Be concise, not curt" in system
    # Persona precedes the app briefing.
    assert system.index("dry and understated") < system.index("Your abilities")
    # Short session: no reminder yet.
    assert "voice rules above still apply" not in system
    # Long session: the reminder is re-injected.
    sid = result["session_id"]
    overseer.sessions[sid].extend(
        {"role": "user", "content": f"m{i}"} for i in range(20)
    )
    asyncio.run(overseer.chat("still there?", session_id=sid))
    assert "voice rules above still apply" in provider.systems[-1]


def test_missing_memory_note_mentioned(state):
    provider = FakeProvider([ProviderReply(text="ok", stop_reason="end_turn")])
    overseer = Overseer(provider, state)
    asyncio.run(overseer.chat("hi"))
    assert "memory note is missing" in provider.systems[-1]


def test_habit_store_curious_then_settles(state):
    from app.overseer.tools import execute_tool
    import json as j

    async def run(inp):
        return j.loads(await execute_tool(state, "s", "update_habit", inp))

    # Ask once, record the answer.
    r = asyncio.run(run({"key": "daily_note.logs_hours", "value": "true", "event": "set"}))
    assert r["confidence"] == 0.85
    # Reinforce on matching behavior.
    r = asyncio.run(run({"key": "daily_note.logs_hours", "event": "confirm"}))
    assert r["confidence"] == 0.9 and r["times_confirmed"] == 1
    # Contradictions decay confidence; low confidence suggests re-asking once.
    for _ in range(3):
        r = asyncio.run(run({"key": "daily_note.logs_hours", "event": "contradict"}))
    assert r["confidence"] < 0.4
    assert "re-asking" in r["note"]
    # read_habits surfaces the record.
    out = j.loads(asyncio.run(execute_tool(state, "s", "read_habits", {})))
    assert out["habits"][0]["key"] == "daily_note.logs_hours"


def test_proactive_pass_blocks_action_tier(state):
    provider = FakeProvider(
        [
            tool_use_reply("delete_note", {"path": "Welcome.md"}),
            ProviderReply(text="tried to delete", stop_reason="end_turn"),
        ]
    )
    overseer = Overseer(provider, state)
    result = asyncio.run(overseer.chat("morning pass", proactive=True))
    # The delete was refused: nothing staged, error surfaced to the model.
    assert result["pending_count"] == 0
    tool_results = [
        m for m in provider.seen[-1] if m["role"] == "user" and isinstance(m["content"], list)
    ]
    assert "action-tier" in tool_results[-1]["content"][0]["content"]
    # And the proactive system prompt made the constraint explicit.
    assert "PROACTIVE" in provider.systems[-1]


def test_digest_buckets_tasks(state):
    from app.overseer.tools import execute_tool
    import json as j
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    # The digest buckets by the app's timezone, not the machine's.
    day = datetime.now(ZoneInfo(state.timezone)).date()
    today = day.isoformat()
    yesterday = (day - timedelta(days=1)).isoformat()
    tomorrow = (day + timedelta(days=1)).isoformat()
    content = (
        f"---\ntitle: T\n---\n\n- [ ] late 📅 {yesterday}\n- [ ] now 📅 {today}\n"
        f"- [ ] soon 📅 {tomorrow}\n- [ ] whenever\n- [x] finished\n"
    )
    state.store.create("Tasks.md", content)
    asyncio.run(state.indexer.index_note("Tasks.md", state.store.read("Tasks.md")))
    data = j.loads(asyncio.run(execute_tool(state, "s", "get_digest_data", {"days": 1})))

    def mine(bucket):  # ignore tasks from the seeded Welcome note
        return [
            t["text"].split(" 📅")[0] for t in data["tasks"][bucket] if t["path"] == "Tasks.md"
        ]

    assert mine("overdue") == ["late"]
    assert mine("due_today") == ["now"]
    assert mine("upcoming") == ["soon"]
    assert mine("no_due_date") == ["whenever"]
    assert any(t["text"] == "finished" for t in data["tasks_completed_recently"])


def _done_task(task_id: str, text: str, completed_at: str) -> str:
    meta = f'{{"id":"{task_id}","completed_at":"{completed_at}"}}'
    return f"<!-- exscalia-task:{meta} -->\n- [x] {text}\n"


def _write_and_index(state, path: str, content: str) -> None:
    target = state.store.resolve(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    asyncio.run(state.indexer.index_note(path, content))


def test_digest_counts_tasks_by_when_they_were_finished(state):
    """The note's `modified` used to be the criterion: one edit to a note
    credited every done task in it as finished today."""
    from app.overseer.tools import execute_tool
    from app.vault.internal import iso_utc
    import json as j
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    recent = iso_utc(now - timedelta(hours=1))
    long_ago = iso_utc(now - timedelta(days=60))
    stale = (now - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Touched today, but only two of its done tasks were finished recently.
    state.store.create(
        "Tasks Inbox.md",
        "---\ntitle: Tasks Inbox\n---\n\n"
        + _done_task("inbox-old", "months back", long_ago)
        + _done_task("inbox-new", "this morning's win", recent)
        + "- [x] unstamped\n",
    )
    asyncio.run(
        state.indexer.index_note("Tasks Inbox.md", state.store.read("Tasks Inbox.md"))
    )
    # Untouched for weeks, yet a task in it was just finished.
    _write_and_index(
        state,
        "Archive.md",
        f"---\ntitle: Archive\ncreated: '{stale}'\nmodified: '{stale}'\n---\n\n"
        + _done_task("archive-new", "closed out", recent)
        + "- [x] unstamped and stale\n",
    )
    # A folder board's finished work rejoins the global views, as on the
    # universal board and the calendar.
    _write_and_index(
        state,
        "Projects/Tasks.md",
        f"---\ntitle: Tasks\ntask_board: true\ncreated: '{stale}'\nmodified: '{stale}'\n---\n\n"
        + _done_task("board-new", "shipped the board item", recent),
    )

    data = j.loads(asyncio.run(execute_tool(state, "s", "get_digest_data", {"days": 1})))
    texts = [t["text"] for t in data["tasks_completed_recently"]]
    assert sorted(texts) == sorted(
        ["this morning's win", "unstamped", "closed out", "shipped the board item"]
    )
    # Newest first: the unstamped task falls back to its note's fresh stamp.
    assert texts[0] == "unstamped"
    assert data["tasks_completed_count"] == len(texts)


def test_digest_counts_completions_past_the_list_cap(state):
    from app.overseer.tools import get_digest_data
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    body = "".join(
        _done_task(
            f"t{i}",
            f"task {i}",
            (now - timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        for i in range(45)
    )
    state.store.create("Tasks Inbox.md", "---\ntitle: Tasks Inbox\n---\n\n" + body)
    asyncio.run(
        state.indexer.index_note("Tasks Inbox.md", state.store.read("Tasks Inbox.md"))
    )

    data = asyncio.run(get_digest_data(state, "s", {"days": 1}))
    listed = [t["text"] for t in data["tasks_completed_recently"]]
    assert listed == [f"task {i}" for i in range(40)]
    assert data["tasks_completed_count"] == 45


def test_briefing_stat_reads_the_completion_count_not_the_capped_list(state):
    """get_digest_data lists at most 40 finished tasks and reports the real
    count beside them; the stat under the banner must read the count, or a
    productive week shows as exactly 40."""
    from datetime import date

    from app.api.overseer import _briefing_stats

    digest = {
        "tasks_completed_recently": [{"text": f"task {i}"} for i in range(40)],
        "tasks_completed_count": 45,
    }
    stats = asyncio.run(_briefing_stats(state, date(2026, 9, 21), digest))
    assert stats["tasks_completed"] == 45
    # A digest without the count (older callers) still falls back to the list.
    del digest["tasks_completed_count"]
    stats = asyncio.run(_briefing_stats(state, date(2026, 9, 21), digest))
    assert stats["tasks_completed"] == 40


def test_analyze_tags_clusters_variants(state):
    from app.overseer.tools import execute_tool
    import json as j

    state.store.create("A.md", "---\ntitle: A\n---\n\n#ml notes")
    state.store.create("B.md", "---\ntitle: B\n---\n\n#machine-learning and #Recipes")
    state.store.create("C.md", "---\ntitle: C\n---\n\n#recipe collection")
    for p in ("A.md", "B.md", "C.md"):
        asyncio.run(state.indexer.index_note(p, state.store.read(p)))
    out = j.loads(asyncio.run(execute_tool(state, "s", "analyze_tags", {})))
    clusters = [sorted(t["tag"] for t in c) for c in out["possible_duplicates"]]
    assert ["Recipes", "recipe"] in clusters  # singular/plural + case variant


def test_backup_pushes_to_remote(state, tmp_path):
    from git import Repo

    from app.vault.repo import BackupPusher

    bare = tmp_path / "offsite.git"
    Repo.init(bare, bare=True)
    pusher = BackupPusher(state.vault, str(bare), interval=3600)
    pusher.configure_remote_sync()

    result = asyncio.run(pusher.push())
    assert result["pushed"] is True
    # The off-site repo now has the vault's HEAD.
    assert Repo(bare).commit("main").hexsha == state.vault.repo.head.commit.hexsha
    assert pusher.last_push is not None and pusher.last_error is None

    # No new commits → nothing to push.
    again = asyncio.run(pusher.push())
    assert again == {"pushed": False, "reason": "nothing new to push"}

    # New commit → pushed and off-site catches up.
    state.store.create("Backup Me.md", "---\ntitle: Backup Me\n---\n\nhello")
    state.vault.commit_all_sync("autosave", state.vault.user_author)
    third = asyncio.run(pusher.push())
    assert third["pushed"] is True
    assert Repo(bare).commit("main").hexsha == state.vault.repo.head.commit.hexsha


def test_discard_leaves_vault_untouched(state):
    provider = FakeProvider(
        [
            tool_use_reply("delete_note", {"path": "Welcome.md"}),
            ProviderReply(text="staged deletion", stop_reason="end_turn"),
        ]
    )
    overseer = Overseer(provider, state)
    asyncio.run(overseer.chat("delete welcome"))
    assert state.store.exists("Welcome.md")  # still there — staged only
    n = asyncio.run(state.pending.discard_all())
    assert n == 1
    assert state.store.exists("Welcome.md")
    assert asyncio.run(state.pending.count()) == 0


def test_session_cache_evicts_least_recently_used(state):
    """The eviction loop used to be unreachable: every caller pre-seeds the
    session key, so the `session_id not in self.sessions` guard was always
    false and the cache grew for the whole process lifetime."""
    from app.overseer.agent import MAX_SESSIONS

    provider = FakeProvider(
        [ProviderReply(text="ok", stop_reason="end_turn")] * (MAX_SESSIONS + 10)
    )
    overseer = Overseer(provider, state)

    for i in range(MAX_SESSIONS + 5):
        # Mirror production: the API pre-populates the key before chatting.
        overseer.sessions[f"thread-{i}"] = []
        asyncio.run(overseer.chat("hi", session_id=f"thread-{i}"))

    assert len(overseer.sessions) <= MAX_SESSIONS
    assert "thread-0" not in overseer.sessions
    assert f"thread-{MAX_SESSIONS + 4}" in overseer.sessions


def test_touching_an_old_session_keeps_it_alive(state):
    """LRU, not FIFO — the thread you are actively using must not be evicted."""
    from app.overseer.agent import MAX_SESSIONS

    provider = FakeProvider(
        [ProviderReply(text="ok", stop_reason="end_turn")] * (MAX_SESSIONS + 10)
    )
    overseer = Overseer(provider, state)

    overseer.sessions["keep-me"] = []
    asyncio.run(overseer.chat("hi", session_id="keep-me"))
    for i in range(MAX_SESSIONS - 1):
        overseer.sessions[f"filler-{i}"] = []
        asyncio.run(overseer.chat("hi", session_id=f"filler-{i}"))
        # Keep using the first thread so it stays most-recent.
        asyncio.run(overseer.chat("still here", session_id="keep-me"))

    assert "keep-me" in overseer.sessions


def test_turn_usage_is_accumulated_and_returned(state):
    """Token usage was captured per provider reply and then read by nothing."""
    provider = FakeProvider(
        [
            ProviderReply(
                text="",
                tool_calls=[ToolCall(id="t1", name="read_structure", input={})],
                stop_reason="tool_use",
                raw_content=[
                    {"type": "tool_use", "id": "t1", "name": "read_structure", "input": {}}
                ],
                usage={"input": 900, "output": 40, "cache_read": 800},
            ),
            ProviderReply(
                text="done", stop_reason="end_turn", usage={"input": 950, "output": 12}
            ),
        ]
    )

    result = asyncio.run(Overseer(provider, state).chat("what's in my vault?"))

    # Summed across every round of the turn, not just the last reply.
    assert result["usage"] == {"input": 1850, "output": 52, "cache_read": 800}


def test_output_limit_stops_the_turn_instead_of_running_partial_tool_calls(state):
    """A reply cut off at the output ceiling can carry half-built tool
    arguments; running them would act on garbage."""
    provider = FakeProvider(
        [
            ProviderReply(
                text="Partial answer",
                tool_calls=[ToolCall(id="t1", name="delete_note", input={"path": "A"})],
                stop_reason="max_tokens",
                raw_content=[],
            ),
            ProviderReply(text="unreached", stop_reason="end_turn"),
        ]
    )

    result = asyncio.run(Overseer(provider, state).chat("rewrite everything"))

    assert "length limit" in result["reply"]
    assert result["trace"] == []  # the truncated tool call never ran


def test_failed_tool_calls_are_marked_in_the_trace(state):
    """execute_tool returns errors as JSON rather than raising, so a failed
    call used to look identical to a successful one in the transcript."""
    provider = FakeProvider(
        [
            tool_use_reply("read_notes", {"paths": ["Welcome.md"]}, "ok1"),
            tool_use_reply("read_notes", {}, "bad1"),  # missing required paths
            ProviderReply(text="done", stop_reason="end_turn"),
        ]
    )

    result = asyncio.run(Overseer(provider, state).chat("read things"))

    good, bad = result["trace"][0], result["trace"][1]
    assert good["ok"] is True and "error" not in good
    assert bad["ok"] is False
    assert bad["error"]


def test_confirmations_survive_a_restart(state, tmp_path):
    """They lived in a plain dict, so every deploy left a live-looking Approve
    button that 404'd when clicked."""
    from app.overseer.actions import ActionCoordinator

    first = ActionCoordinator(state)
    card = first.request_confirmation(
        "thread-1", "delete_note", {"path": "A.md"}, auth_session_id="sess-1"
    )

    # A fresh coordinator is what a restarted process gets.
    second = ActionCoordinator(state)
    restored = second.consume_confirmation(card["token"], "thread-1", "sess-1")

    assert restored["tool"] == "delete_note"
    assert restored["inputs"] == {"path": "A.md"}
    # Still single-use after the restart.
    with pytest.raises(NoteError):
        second.consume_confirmation(card["token"], "thread-1", "sess-1")


def test_expired_confirmations_are_not_restored(state):
    from datetime import timedelta

    from app.overseer.actions import ActionCoordinator

    first = ActionCoordinator(state)
    first.CONFIRMATION_LIFETIME = timedelta(seconds=-1)  # already expired
    card = first.request_confirmation("t", "delete_note", {"path": "A.md"}, "s")

    second = ActionCoordinator(state)
    with pytest.raises(NoteError):
        second.consume_confirmation(card["token"], "t", "s")


def test_round_limit_ends_in_a_progress_summary_not_a_canned_string(state):
    """Exhausting MAX_ROUNDS spends one more call, tool use forbidden, asking
    the model what it finished and what remains, so the user can decide about
    'continue' — the canned notice is only the fallback."""
    from app.overseer.agent import MAX_ROUNDS

    script = [
        tool_use_reply("read_structure", {}, call_id=f"t{i}")
        for i in range(MAX_ROUNDS)
    ]
    script.append(
        ProviderReply(
            text="Renamed 3 of 5 tags; two remain. Say 'continue' to finish.",
            stop_reason="end_turn",
        )
    )
    overseer = Overseer(FakeProvider(script), state)
    result = asyncio.run(overseer.chat("Rename every tag"))
    assert "two remain" in result["reply"]
    assert "Stopped after too many tool rounds" not in result["reply"]


def multi_tool_reply(calls: list[tuple[str, dict]]) -> ProviderReply:
    tool_calls = [
        ToolCall(id=f"t{i}", name=name, input=inp)
        for i, (name, inp) in enumerate(calls, 1)
    ]
    return ProviderReply(
        text="",
        tool_calls=tool_calls,
        stop_reason="tool_use",
        raw_content=[
            {"type": "tool_use", "id": c.id, "name": c.name, "input": c.input}
            for c in tool_calls
        ],
    )


def test_batched_direct_edits_run_without_a_bulk_confirmation(acting_state):
    """'Take the due dates off them' produced an 'Apply 2 OMNIA changes?'
    card — re-asking after the user already decided. Direct tools answer an
    explicit request and stay individually undoable, so a batch of them runs
    as-is; only calls that would confirm on their own still gate."""
    provider = FakeProvider(
        [
            multi_tool_reply(
                [
                    ("create_note", {"path": "A.md", "content": "a"}),
                    ("create_note", {"path": "B.md", "content": "b"}),
                ]
            ),
            ProviderReply(text="Both created.", stop_reason="end_turn"),
        ]
    )
    result = asyncio.run(Overseer(provider, acting_state).chat("make two notes"))

    assert result["confirmations"] == []
    assert acting_state.store.exists("A.md")
    assert acting_state.store.exists("B.md")
    # Still one undoable action (and scoped commit) per mutation.
    assert len(result["actions"]) == 2


def test_multiple_destructive_calls_fold_into_one_bulk_card(acting_state):
    acting_state.store.create("A.md", "a")
    acting_state.store.create("B.md", "b")
    provider = FakeProvider(
        [
            multi_tool_reply(
                [
                    ("delete_note", {"path": "A.md", "base_revision": "x"}),
                    ("delete_note", {"path": "B.md", "base_revision": "x"}),
                ]
            ),
            ProviderReply(text="Waiting for approval.", stop_reason="end_turn"),
        ]
    )
    result = asyncio.run(Overseer(provider, acting_state).chat("delete both"))

    assert [c["tool"] for c in result["confirmations"]] == ["bulk_actions"]
    assert "2" in result["confirmations"][0]["summary"]
    # Nothing ran ahead of approval.
    assert acting_state.store.exists("A.md")
    assert acting_state.store.exists("B.md")


def test_mixed_round_gates_only_the_destructive_call(acting_state):
    acting_state.store.create("Old.md", "old")
    provider = FakeProvider(
        [
            multi_tool_reply(
                [
                    ("delete_note", {"path": "Old.md", "base_revision": "x"}),
                    ("create_note", {"path": "New.md", "content": "n"}),
                ]
            ),
            ProviderReply(text="Created; delete awaits approval.", stop_reason="end_turn"),
        ]
    )
    result = asyncio.run(Overseer(provider, acting_state).chat("swap the notes"))

    # The direct create ran; only the delete asks, as its own card (no bulk).
    assert acting_state.store.exists("New.md")
    assert acting_state.store.exists("Old.md")
    assert [c["tool"] for c in result["confirmations"]] == ["delete_note"]


# --- what the memory note looks like once it reaches the prompt --------------


def _memory_block(system: str) -> str:
    return system.split("<memory>", 1)[1].split("</memory>", 1)[0]


def test_placeholder_lines_and_empty_sections_never_reach_the_model(state):
    """A fresh note is five headings of "(nothing recorded yet)". Injecting it
    verbatim spent tokens every turn saying nothing."""
    from app.overseer.memory import MEMORY_TEMPLATE

    state.store.create("omnia/memory.md", MEMORY_TEMPLATE)
    provider = FakeProvider([ProviderReply(text="ok", stop_reason="end_turn")])
    asyncio.run(Overseer(provider, state).chat("hi"))

    block = _memory_block(provider.systems[-1])
    assert "(nothing recorded yet)" not in block
    assert "## About the user" not in block  # empty, so not even the heading


def test_a_note_over_budget_loses_its_oldest_entries_not_its_newest(state):
    """Truncating head-first cut the tail of the note — which is the OLDEST
    entries of the LAST sections, plus every heading after the cut. Entries are
    newest-first, so the budget now keeps the top of each section."""
    from app.overseer import agent as agent_module

    filler = "x" * 300
    def section(title, tag):
        lines = [f"- 2026-01-01 {tag} newest {filler}"]
        lines += [f"- 2025-01-01 {tag} older {index} {filler}" for index in range(40)]
        return f"## {title}\n\n" + "\n".join(lines)

    state.store.create(
        "omnia/memory.md",
        "---\ntitle: OMNIA Memory\n---\n\n"
        + section("About the user", "ABOUT")
        + "\n\n"
        + section("Vault conventions", "CONVENTION")
        + "\n",
    )
    provider = FakeProvider([ProviderReply(text="ok", stop_reason="end_turn")])
    asyncio.run(Overseer(provider, state).chat("hi"))

    block = _memory_block(provider.systems[-1])
    assert len(block) <= agent_module.MAX_MEMORY_CHARS
    # Both sections are represented — the budget is shared, not spent on the
    # first heading until it runs out.
    assert "ABOUT newest" in block
    assert "CONVENTION newest" in block
    # And what was dropped is the old end of each, not the new end of the last.
    assert "ABOUT older 39" not in block
    assert "CONVENTION older 39" not in block
    # Nothing was written: the note on disk is untouched.
    assert "CONVENTION older 39" in state.store.read("omnia/memory.md")


def test_the_missing_note_fallback_points_at_the_tool_that_creates_it(state):
    provider = FakeProvider([ProviderReply(text="ok", stop_reason="end_turn")])
    asyncio.run(Overseer(provider, state).chat("hi"))

    system = provider.systems[-1]
    assert "memory note is missing" in system
    assert "remember()" in _memory_block(system)


def test_the_memory_template_does_not_promise_an_approval_step(state):
    """It claimed updates "land in the pending-changes box… nothing is
    remembered without your approval". remember() is a direct write."""
    from app.overseer.memory import MEMORY_TEMPLATE

    assert "pending-changes box" not in MEMORY_TEMPLATE
    assert "without your approval" not in MEMORY_TEMPLATE
    assert "undone" in MEMORY_TEMPLATE


# --- habits: decay, mirroring, restore ---------------------------------------


def _add_habit(state, key, value, confidence, last_seen):
    asyncio.run(
        state.db.execute(
            "INSERT INTO habits(key, value, confidence, last_seen, times_confirmed)"
            " VALUES (?,?,?,?,0)",
            (key, value, confidence, last_seen),
        )
    )
    asyncio.run(state.db.commit())


def test_a_recent_habit_outranks_an_ancient_more_confident_one(state):
    """Ranking on raw confidence froze the list: something recorded once years
    ago and never seen again sat above what was confirmed last week."""
    from app.vault.internal import iso_utc, utc_now
    from datetime import timedelta

    _add_habit(state, "ancient.certain", "yes", 0.95, iso_utc(utc_now() - timedelta(days=900)))
    _add_habit(state, "recent.likely", "yes", 0.85, iso_utc(utc_now()))

    provider = FakeProvider([ProviderReply(text="ok", stop_reason="end_turn")])
    asyncio.run(Overseer(provider, state).chat("hi"))
    block = provider.systems[-1].split("<habits>", 1)[1].split("</habits>", 1)[0]

    assert block.index("recent.likely") < block.index("ancient.certain")
    # Decayed, not deleted: an old certainty is still worth knowing.
    assert "ancient.certain" in block
    # The number shown is still the stored confidence, not the rank.
    assert "confidence 0.95" in block


def test_every_habit_write_is_mirrored_into_the_vault(acting_state):
    """The table is in the index, which is documented as rebuildable — but
    nothing could rebuild it. The mirror is what makes that true."""
    import json as j

    from app.overseer import habits
    from app.overseer.tools import execute_tool
    from app.vault.internal import read_json

    asyncio.run(
        execute_tool(
            acting_state, "s", "update_habit",
            {"key": "daily_note.logs_hours", "value": "yes", "event": "set"},
        )
    )
    mirrored = read_json(habits.mirror_path(acting_state), {})["habits"]
    assert mirrored["daily_note.logs_hours"]["value"] == "yes"

    asyncio.run(
        execute_tool(
            acting_state, "s", "update_habit",
            {"key": "daily_note.logs_hours", "event": "delete"},
        )
    )
    assert read_json(habits.mirror_path(acting_state), {})["habits"] == {}


def test_an_emptied_habits_table_comes_back_from_the_mirror(acting_state):
    from app.overseer import habits
    from app.overseer.tools import execute_tool

    asyncio.run(
        execute_tool(
            acting_state, "s", "update_habit",
            {"key": "tags.canonical.ml", "value": "machine-learning", "event": "set"},
        )
    )
    asyncio.run(acting_state.db.execute("DELETE FROM habits"))
    asyncio.run(acting_state.db.commit())

    assert asyncio.run(habits.restore_if_empty(acting_state)) == 1

    rows = asyncio.run(acting_state.db.execute_fetchall("SELECT * FROM habits"))
    assert rows[0]["value"] == "machine-learning"
    assert rows[0]["confidence"] == 0.85

    # A populated table is the live one: an older mirror must never overwrite it.
    asyncio.run(
        execute_tool(
            acting_state, "s", "update_habit",
            {"key": "tags.canonical.ml", "value": "changed", "event": "set"},
        )
    )
    assert asyncio.run(habits.restore_if_empty(acting_state)) == 0


def _habit(state, thread, inp) -> dict:
    import json as j

    from app.overseer.tools import execute_tool

    return j.loads(asyncio.run(execute_tool(state, thread, "update_habit", inp)))


def _habit_values(state) -> dict:
    rows = asyncio.run(state.db.execute_fetchall("SELECT key, value FROM habits"))
    return {row["key"]: row["value"] for row in rows}


def test_a_habit_write_is_a_recorded_action_with_an_undo(acting_state):
    """Habits ride in every later system prompt. A write that left no card, no
    history entry and no way back was a standing instruction nobody could see."""
    from app.overseer import habits
    from app.overseer.tools import TOOL_MODES, needs_confirmation

    # A write: run under the mutation lock like every direct tool, and still
    # without a card to approve.
    assert TOOL_MODES["update_habit"] == "direct"
    assert not needs_confirmation("update_habit", {"key": "k", "value": "v", "event": "set"})

    _habit(acting_state, "t", {"key": "replies.format", "value": "bullets", "event": "set"})
    out = _habit(acting_state, "t", {"key": "replies.format", "value": "prose", "event": "set"})

    action = out["_action"]
    assert action["tool"] == "update_habit"
    assert action["paths"] == [habits.HABITS_PATH]
    assert "replies.format: prose" in action["summary"]
    assert action["undoable"] is True
    assert action["id"] in {a["id"] for a in acting_state.actions.list()}

    asyncio.run(acting_state.actions.undo(action["id"], "t"))

    # The table follows the reverted mirror, and so does the next prompt.
    assert _habit_values(acting_state) == {"replies.format": "bullets"}
    stored = habits.stored(acting_state)
    assert stored["replies.format"]["value"] == "bullets"


def test_undoing_the_first_habit_empties_the_store(acting_state):
    out = _habit(acting_state, "t", {"key": "daily.logs_hours", "value": "yes", "event": "set"})

    asyncio.run(acting_state.actions.undo(out["_action"]["id"], "t"))

    assert _habit_values(acting_state) == {}
    # And a later write does not bring the undone row back with it.
    _habit(acting_state, "t", {"key": "notes.title_case", "value": "yes", "event": "set"})
    assert _habit_values(acting_state) == {"notes.title_case": "yes"}


def test_confirm_contradict_and_delete_are_recorded_too(acting_state):
    _habit(acting_state, "t", {"key": "daily.logs_hours", "value": "yes", "event": "set"})
    for event in ("confirm", "contradict", "delete"):
        out = _habit(acting_state, "t", {"key": "daily.logs_hours", "event": event})
        assert out["_action"]["tool"] == "update_habit", event

    undo = asyncio.run(
        acting_state.actions.undo(acting_state.actions.list()[0]["id"], "t")
    )
    assert undo["ok"] is True
    assert "daily.logs_hours" in _habit_values(acting_state)

    # Deleting what is not there writes nothing, so it records nothing.
    before = len(acting_state.actions.list())
    out = _habit(acting_state, "t", {"key": "no.such.habit", "event": "delete"})
    assert out["deleted"] is False and "_action" not in out
    assert len(acting_state.actions.list()) == before


def test_habit_keys_and_values_are_capped(acting_state):
    """The top forty rows go verbatim into every system prompt: a key is one
    short token and a value one short line."""
    refused = [
        {"key": "k" * 81, "value": "yes"},
        {"key": "replies format", "value": "yes"},
        {"key": "replies.format", "value": "v" * 201},
        {"key": "replies.format", "value": "short\nthen a second paragraph"},
    ]
    for inp in refused:
        out = _habit(acting_state, "t", {**inp, "event": "set"})
        assert "error" in out, inp
    assert _habit_values(acting_state) == {}
    assert acting_state.actions.list() == []

    out = _habit(acting_state, "t", {"key": "k" * 80, "value": "v" * 200, "event": "set"})
    assert "error" not in out


def test_a_key_stored_before_the_caps_can_still_be_deleted(acting_state):
    _add_habit(acting_state, "legacy key " + "x" * 100, "long\nvalue", 0.85, None)

    out = _habit(acting_state, "t", {"key": "legacy key " + "x" * 100, "event": "delete"})

    assert out["deleted"] is True
    assert _habit_values(acting_state) == {}
