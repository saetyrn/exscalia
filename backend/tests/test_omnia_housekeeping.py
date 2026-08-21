"""The nightly conversation tidy.

The rules decide what OMNIA is even allowed to consider; the model only picks
within that set. These tests lean hardest on the two properties that make
deleting without asking defensible: nothing the rules protected ever reaches
the model, and every deletion is recoverable from git by name.
"""
import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.index.db import open_db
from app.index.indexer import Indexer
from app.live import LiveBus
from app.overseer import housekeeping as hk
from app.overseer.provider import FakeProvider, ProviderReply
from app.overseer.threads import ThreadStore
from app.pending.store import PendingStore
from app.vault.notes import NoteStore
from app.vault.repo import VaultRepo

TZ = "America/New_York"


class NoSemantic:
    available = False

    async def search(self, q, k=8):
        return []

    async def index_note(self, *a, **k):
        return None


@pytest.fixture
def hk_state(tmp_path: Path):
    vault_dir = tmp_path / "vault"
    vault = VaultRepo(vault_dir, "user", "user@test")
    vault.ensure()
    store = NoteStore(vault_dir)

    async def build():
        db = await open_db(tmp_path / "index")
        indexer = Indexer(db, store)
        await indexer.sync_all()
        threads = ThreadStore(vault_dir, tmp_path)
        return SimpleNamespace(
            db=db,
            store=store,
            vault=vault,
            indexer=indexer,
            threads=threads,
            pending=PendingStore(db, store, vault, indexer),
            semantic=NoSemantic(),
            settings=SimpleNamespace(
                vault_dir=vault_dir,
                data_dir=tmp_path,
                # The background-model lookup walks the same settings the real
                # app has; mirror the relevant fields of config.Settings.
                ai_provider="anthropic",
                anthropic_api_key=None,
                openai_api_key=None,
                google_api_key=None,
                anthropic_model="claude-opus-4-8",
                openai_model="gpt-5.6-sol",
                google_model="gemini-3.6-flash",
            ),
            autocommit=SimpleNamespace(touch=lambda: None),
            live=LiveBus(),
            timezone=TZ,
            omnia_housekeeping=True,
            overseer=None,
        )

    state = asyncio.run(build())
    yield state
    asyncio.run(state.db.close())


def with_model(state, *replies):
    """Attach a scripted model. `sessions` mirrors the real Overseer surface
    housekeeping touches when it drops a deleted thread's history."""
    provider = FakeProvider(list(replies))
    state.overseer = SimpleNamespace(provider=provider, sessions={})
    return provider


def verdicts(*items) -> ProviderReply:
    return ProviderReply(text=json.dumps({"verdicts": list(items)}), stop_reason="end_turn")


def after_boundary(days_ago: int = 0) -> datetime:
    """A moment just past tonight's run time, in the app's timezone."""
    now = datetime.now(ZoneInfo(TZ))
    return (now - timedelta(days=days_ago)).replace(
        hour=hk.RUN_HOUR, minute=hk.RUN_MINUTE + 5, second=0, microsecond=0
    )


def local_today() -> str:
    return after_boundary().date().isoformat()


def seed_marker(state, day: str) -> None:
    hk.write_marker(state, {"version": hk.MARKER_VERSION, "last_run_day": day})


def make_thread(state, title: str, messages: list[dict], *, thread_id: str | None = None,
                kind: str = "named", date: str | None = None) -> str:
    thread = state.threads.create(title=title, kind=kind, date=date, thread_id=thread_id)
    for message in messages:
        state.threads.append_public(thread["id"], message)
    # Age it past the "someone is typing in this" guard.
    raw = state.threads.get(thread["id"])
    raw["updated_at"] = "2020-01-01T00:00:00Z"
    from app.vault.internal import atomic_json

    atomic_json(state.threads._path(thread["id"]), raw)
    return thread["id"]


def run(state, *, days_ago: int = 0) -> dict:
    return asyncio.run(hk.run_housekeeping(state, now=after_boundary(days_ago)))


def exists(state, thread_id: str) -> bool:
    return state.threads._path(thread_id).exists()


# --- the hard rules ---------------------------------------------------------


def test_a_pending_confirmation_is_never_deleted_and_never_reaches_the_model(hk_state):
    seed_marker(hk_state, "2020-01-01")
    tid = make_thread(
        hk_state, "Awaiting", [{"role": "assistant", "text": "Ok.", "confirmations": [{"token": "t1"}]}]
    )
    provider = with_model(hk_state, verdicts({"id": tid, "delete": True}))

    run(hk_state)

    assert exists(hk_state, tid)
    # The rules gate the model, not the other way round: it is never even shown.
    assert tid not in json.dumps(provider.seen)


def test_an_errand_can_be_cleared_and_the_model_is_told_what_it_did(hk_state):
    """Having done something is not by itself worth remembering — logging time
    or proofreading a note leaves its record in the action ledger."""
    seed_marker(hk_state, "2020-01-01")
    tid = make_thread(
        hk_state,
        "WKS BIBILE",
        [
            {"role": "user", "text": "log 3 hours for WRO"},
            {
                "role": "assistant",
                "text": "Logged 3 hours.",
                "actions": [{"id": "a1", "tool": "log_activity", "summary": "Logged 3 hours for WRO"}],
                "trace": [{"tool": "log_activity", "ok": True}],
            },
        ],
    )
    provider = with_model(hk_state, verdicts({"id": tid, "delete": True}))

    result = run(hk_state)

    assert result["deleted"] == 1
    assert not exists(hk_state, tid)
    # It judges on the evidence, so what was done has to be in the payload.
    shown = json.dumps(provider.seen)
    assert "Logged 3 hours for WRO" in shown
    assert "log_activity" in shown


def test_far_reaching_work_is_offered_to_the_model_and_can_be_kept(hk_state):
    seed_marker(hk_state, "2020-01-01")
    tid = make_thread(
        hk_state,
        "Cleanup",
        [
            {
                "role": "assistant",
                "text": "Renamed the tag everywhere.",
                "actions": [{"id": "a1", "tool": "rename_tag", "summary": "Renamed #uni to #school across 43 notes"}],
            }
        ],
    )
    with_model(hk_state, verdicts({"id": tid, "delete": False, "summary": "Merged the school tags"}))

    run(hk_state)

    assert exists(hk_state, tid)
    assert next(t for t in hk_state.threads.list() if t["id"] == tid)["preview"] == "Merged the school tags"


def test_undo_still_works_after_its_conversation_is_gone(hk_state):
    """The action ledger outlives the conversation, so the history page's undo
    must not care that the thread it names has been cleared."""
    from app.api import overseer as overseer_api
    from app.overseer.actions import ActionCoordinator

    hk_state.actions = ActionCoordinator(hk_state)
    tid = make_thread(hk_state, "Errand", [{"role": "assistant", "text": "Added it."}])
    asyncio.run(hk_state.vault.commit_all("seed"))
    # The change has to be uncommitted when it is recorded, or there is no
    # scoped commit to revert later.
    hk_state.store.create("Logged.md", "---\ntitle: Logged\n---\n\nbefore\n")
    action = asyncio.run(
        hk_state.actions.record(
            thread_id=tid,
            tool="create_note",
            summary="Created Logged.md",
            paths=["Logged.md"],
            refresh=["notes"],
            result={},
        )
    )["_action"]

    hk_state.threads.delete(tid)

    request = SimpleNamespace(app=SimpleNamespace(state=hk_state))
    body = SimpleNamespace(thread_id=tid)
    result = asyncio.run(overseer_api.undo_omnia_action(request, action["id"], body))

    assert result.get("ok") is not False


def test_a_thread_with_staged_proposals_is_never_deleted(hk_state):
    seed_marker(hk_state, "2020-01-01")
    tid = make_thread(hk_state, "Proposed", [{"role": "assistant", "text": "Staged it."}])
    asyncio.run(
        hk_state.pending.stage(tid, "edit", "Note.md", new_content="---\ntitle: Note\n---\n\nhi\n")
    )
    provider = with_model(hk_state, verdicts({"id": tid, "delete": True}))

    run(hk_state)

    assert exists(hk_state, tid)
    assert tid not in json.dumps(provider.seen)


def test_todays_daily_survives_while_yesterdays_twin_goes(hk_state):
    seed_marker(hk_state, "2020-01-01")
    today = local_today()
    yesterday = (after_boundary().date() - timedelta(days=1)).isoformat()
    greeting = [{"role": "assistant", "text": "Morning."}]
    today_id = make_thread(hk_state, "Today", greeting, thread_id=f"daily-{today}", kind="daily", date=today)
    old_id = make_thread(hk_state, "Yesterday", greeting, thread_id=f"daily-{yesterday}", kind="daily", date=yesterday)
    with_model(hk_state, verdicts({"id": old_id, "delete": True}))

    run(hk_state)

    # Today's is live, and it is the morning greeting's only marker.
    assert exists(hk_state, today_id)
    assert not exists(hk_state, old_id)


def test_a_greeting_only_thread_takes_its_history_sidecar_with_it(hk_state):
    seed_marker(hk_state, "2020-01-01")
    tid = make_thread(hk_state, "Friday", [{"role": "assistant", "text": "Noted."}])
    hk_state.threads.set_provider_history(tid, [{"role": "user", "content": "x"}])
    assert hk_state.threads._history_path(tid).exists()
    with_model(hk_state, verdicts({"id": tid, "delete": True}))

    result = run(hk_state)

    assert result["deleted"] == 1
    assert not exists(hk_state, tid)
    assert not hk_state.threads._history_path(tid).exists()


# --- the parser fails closed ------------------------------------------------


def test_an_unreadable_reply_deletes_nothing(hk_state):
    seed_marker(hk_state, "2020-01-01")
    tid = make_thread(hk_state, "Friday", [{"role": "assistant", "text": "Noted."}])
    with_model(hk_state, ProviderReply(text="sure, I'll clean those up", stop_reason="end_turn"))

    result = run(hk_state)

    assert result["deleted"] == 0
    assert exists(hk_state, tid)


def test_a_verdict_for_a_thread_that_was_not_a_candidate_is_ignored(hk_state):
    seed_marker(hk_state, "2020-01-01")
    protected = make_thread(
        hk_state,
        "Awaiting",
        [{"role": "assistant", "text": "Ok.", "confirmations": [{"token": "t1"}]}],
    )
    trivial = make_thread(hk_state, "Friday", [{"role": "assistant", "text": "Noted."}])
    # The reply names a thread the rules already excluded — the shape a
    # hallucination or an injected instruction would take.
    with_model(hk_state, verdicts({"id": protected, "delete": True}))

    run(hk_state)

    assert exists(hk_state, protected)
    assert exists(hk_state, trivial)  # no verdict of its own, so it stays


def test_a_missing_verdict_keeps_the_thread(hk_state):
    seed_marker(hk_state, "2020-01-01")
    a = make_thread(hk_state, "Friday", [{"role": "assistant", "text": "Noted."}])
    b = make_thread(hk_state, "Thursday", [{"role": "assistant", "text": "Hello."}])
    with_model(hk_state, verdicts({"id": a, "delete": True}))

    run(hk_state)

    assert not exists(hk_state, a)
    assert exists(hk_state, b)


def test_only_a_literal_true_deletes(hk_state):
    seed_marker(hk_state, "2020-01-01")
    tid = make_thread(hk_state, "Friday", [{"role": "assistant", "text": "Noted."}])
    with_model(hk_state, verdicts({"id": tid, "delete": "false"}))  # a truthy string

    run(hk_state)

    assert exists(hk_state, tid)


# --- summaries --------------------------------------------------------------


def test_kept_threads_get_a_summary_as_their_preview(hk_state):
    seed_marker(hk_state, "2020-01-01")
    tid = make_thread(
        hk_state,
        "Reading",
        [{"role": "user", "text": "what should I read"}, {"role": "assistant", "text": "Three short ones."}],
    )
    before = hk_state.threads.get(tid)["updated_at"]
    with_model(hk_state, verdicts({"id": tid, "delete": False, "summary": "Picked the next book to read"}))

    result = run(hk_state)

    assert result["summarized"] == 1
    listed = next(t for t in hk_state.threads.list() if t["id"] == tid)
    assert listed["preview"] == "Picked the next book to read"
    # Summarizing must not reorder the sidebar, which sorts on updated_at.
    assert hk_state.threads.get(tid)["updated_at"] == before


def test_a_rambling_summary_is_dropped_rather_than_stored(hk_state):
    seed_marker(hk_state, "2020-01-01")
    tid = make_thread(hk_state, "Reading", [{"role": "assistant", "text": "Three short ones."}])
    with_model(
        hk_state,
        verdicts({"id": tid, "delete": False, "summary": " ".join(["word"] * 30)}),
    )

    run(hk_state)

    assert next(t for t in hk_state.threads.list() if t["id"] == tid)["preview"] == "Three short ones."


# --- scheduling -------------------------------------------------------------


def test_it_runs_once_per_local_day(hk_state):
    seed_marker(hk_state, "2020-01-01")
    make_thread(hk_state, "Friday", [{"role": "assistant", "text": "Noted."}])
    provider = with_model(hk_state, verdicts(), verdicts())

    assert run(hk_state)["ran"] is True
    assert run(hk_state) == {"ran": False, "reason": "already_ran"}
    assert len(provider.systems) == 1


def test_it_catches_up_after_a_night_the_container_was_down(hk_state):
    seed_marker(hk_state, "2020-01-01")  # a stale marker, not today
    make_thread(hk_state, "Friday", [{"role": "assistant", "text": "Noted."}])
    with_model(hk_state, verdicts())

    assert run(hk_state)["ran"] is True


def test_it_waits_until_the_local_boundary(hk_state):
    seed_marker(hk_state, "2020-01-01")
    with_model(hk_state, verdicts())
    early = after_boundary().replace(hour=0, minute=0)

    assert asyncio.run(hk.run_housekeeping(hk_state, now=early))["reason"] == "before_boundary"


def test_the_first_ever_run_only_adopts_today(hk_state):
    tid = make_thread(hk_state, "Friday", [{"role": "assistant", "text": "Noted."}])
    provider = with_model(hk_state, verdicts({"id": tid, "delete": True}))

    # Deploying the feature must not delete anything the same night.
    assert run(hk_state)["reason"] == "seeded"
    assert exists(hk_state, tid)
    assert provider.systems == []
    assert hk.read_marker(hk_state)["last_run_day"] == local_today()


def test_without_a_model_it_does_nothing_at_all(hk_state):
    make_thread(hk_state, "Friday", [{"role": "assistant", "text": "Noted."}])

    assert run(hk_state)["reason"] == "no_model"
    # Not even a marker: no disk churn while the app sits without a key.
    assert not hk.marker_path(hk_state).exists()


def test_the_toggle_off_is_a_no_op(hk_state):
    seed_marker(hk_state, "2020-01-01")
    tid = make_thread(hk_state, "Friday", [{"role": "assistant", "text": "Noted."}])
    with_model(hk_state, verdicts({"id": tid, "delete": True}))
    hk_state.omnia_housekeeping = False

    assert run(hk_state)["reason"] == "disabled"
    assert exists(hk_state, tid)


# --- races and recoverability ----------------------------------------------


def test_a_thread_replied_to_during_the_model_call_is_spared(hk_state):
    seed_marker(hk_state, "2020-01-01")
    tid = make_thread(hk_state, "Friday", [{"role": "assistant", "text": "Noted."}])

    class Interrupting(FakeProvider):
        async def chat(self, system, messages, tools, on_text=None):
            # The user types while the model is deciding.
            hk_state.threads.append_public(tid, {"role": "user", "text": "wait, one more thing"})
            return await super().chat(system, messages, tools, on_text)

    provider = Interrupting([verdicts({"id": tid, "delete": True})])
    hk_state.overseer = SimpleNamespace(provider=provider, sessions={})

    result = run(hk_state)

    assert result["deleted"] == 0
    assert exists(hk_state, tid)


def test_every_deletion_is_recoverable_from_git_by_name(hk_state):
    """The premise the whole feature rests on: deleting without asking is only
    defensible because the conversation is still in history afterwards."""
    seed_marker(hk_state, "2020-01-01")
    tid = make_thread(hk_state, "Friday", [{"role": "assistant", "text": "Noted."}])
    with_model(hk_state, verdicts({"id": tid, "delete": True}))

    run(hk_state)
    assert not exists(hk_state, tid)

    repo = hk_state.vault.repo
    rel = f"{NoteStore.INTERNAL_DIR}/omnia/threads/{tid}.json"
    log = repo.git.log("--diff-filter=D", "--format=%H %s", "--", rel)
    assert "OMNIA housekeeping" in log

    deleting_sha = log.split("\n")[0].split(" ", 1)[0]
    restored = repo.git.show(f"{deleting_sha}~1:{rel}")
    assert "Noted." in restored


def test_booting_the_app_does_no_housekeeping(hk_state):
    """The ticker sleeps first, which is what keeps the real-lifespan test fast
    and stops a deploy from deleting minutes after start-up."""
    seed_marker(hk_state, "2020-01-01")
    tid = make_thread(hk_state, "Friday", [{"role": "assistant", "text": "Noted."}])
    with_model(hk_state, verdicts({"id": tid, "delete": True}))

    async def boot_and_stop():
        task = asyncio.create_task(hk.housekeeping_ticker(hk_state))
        await asyncio.sleep(0.05)
        task.cancel()

    asyncio.run(boot_and_stop())

    assert exists(hk_state, tid)
