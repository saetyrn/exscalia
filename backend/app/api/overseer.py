import asyncio
import json
import logging
import time
import uuid
from datetime import date as date_cls, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..overseer.agent import MAX_MESSAGE_IMAGES, Overseer
from ..overseer import observations
from ..overseer.history import summarize_before_trim
from ..overseer.provider import build_provider
from ..overseer.threads import MAX_HISTORY_CHARS
from ..scheduler import run_scope
from ..overseer.tools import execute_tool, get_digest_data
from ..overseer.vision import is_attachable
from ..overseer.runtime_config import (
    PROVIDERS,
    REASONING_LEVELS,
    api_key_matches,
    effective_ai,
    env_key,
    env_model,
    load_ai_config,
    model_ids,
    provider_info,
    reasoning_level,
    save_ai_config,
)
from ..tz import local_now
from ..vault.notes import NoteError

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["overseer"])
log = logging.getLogger(__name__)

# How often a streaming reply is written to its transcript. The live text goes
# out over SSE; this file only has to be current enough that a reload or a
# crash mid-reply loses no more than this much.
PERSIST_INTERVAL = 0.25

# Seconds of silence before the reply stream sends an SSE comment. A tool round
# can run quiet for a long time, and proxies drop connections that look idle;
# sse.ts skips comment frames, so the client never sees these.
KEEPALIVE_SECONDS = 15

# Every in-flight generation, held strongly. A turn now outlives the request
# that started it, and ThreadStore._runs keeps only the latest per thread, so
# without this a run could be left referenced by nothing but the event loop,
# which may garbage-collect a pending task mid-flight.
_detached_runs: set[asyncio.Task] = set()


class ChatMessage(BaseModel):
    message: str
    session_id: str | None = None
    context_note: str | None = None
    # Same "current moment" fields the streaming path takes (see ThreadMessage).
    # This endpoint used to drop them, so a client that could not stream — the
    # desktop app's fallback, any scripted caller — asked "rewrite this" and
    # OMNIA had no idea what "this" was.
    selection: str | None = None
    view: str | None = None
    open_tabs: list[str] | None = None
    route: str | None = None
    selection_range: dict | None = None


class ThreadCreate(BaseModel):
    title: str = "New chat"
    # A note's drawer conversation: bound to the note server-side, and the
    # note's existing thread is returned instead if another device made one.
    note_path: str | None = None


class NoteThreadBind(BaseModel):
    path: str
    thread_id: str


def _note_path(path: str | None) -> str | None:
    if path is None:
        return None
    path = path.strip().strip("/")
    if not path.endswith(".md"):
        raise NoteError(400, "Only a note can have a conversation of its own")
    return path


class ThreadUpdate(BaseModel):
    title: str


class ThreadMessage(BaseModel):
    message: str
    context_note: str | None = None
    # Vault-relative attachment paths, already uploaded via /api/attachments.
    # Paths rather than inline base64: thread JSON is git-committed, so the
    # bytes would land in the repo on every turn.
    images: list[str] = []
    # "Current moment" awareness — what the user is looking at right now, so
    # "rewrite this" / "what's on here?" resolve. Pure prompt context, never
    # persisted (unlike context_note, which also defaults tool targets).
    selection: str | None = None
    view: str | None = None
    open_tabs: list[str] | None = None
    # The page's pathname, e.g. /recipes/recipes/Carbonara.md: in Recipes the
    # turn starts with the recipes tools and knows which recipe is open.
    route: str | None = None
    # The editor selection's exact offsets (from/to/head/doc_len/path). Unlike
    # the prompt-only fields above, this IS persisted onto the user message so
    # selection-editing tools can act on the precise range.
    selection_range: dict | None = None


def _ui_context(body) -> dict | None:
    context = {
        "selection": getattr(body, "selection", None),
        "view": getattr(body, "view", None),
        "open_tabs": getattr(body, "open_tabs", None),
        "route": getattr(body, "route", None),
    }
    return context if any(context.values()) else None


class ApproveBody(BaseModel):
    force: bool = False
    # Approve-all only: restrict to the ids the review page listed.
    ids: list[int] | None = None


class AlertAckBody(BaseModel):
    keys: list[str]


class OmniaActionBody(BaseModel):
    thread_id: str


class AIConfigBody(BaseModel):
    provider: str | None = None  # switch active provider
    api_key: str | None = None  # omit/empty = keep current key
    clear_key: bool = False
    model: str | None = None
    reasoning: str | None = None  # "default" | "low" | "medium" | "high" | "max"


# --- overseer chat ----------------------------------------------------------


def _public_thread(thread: dict) -> dict:
    return {key: value for key, value in thread.items() if key != "provider_history"}


def _auth_session_id(request: Request) -> str | None:
    auth = request.app.state.auth
    row = auth.session_for_token(auth.token_from_request(request), touch=False)
    return str(row["id"]) if row else None


# Rides at the end of the user's message, provider copy only, on the turn after
# a confirmation card or an Undo was answered: those clicks change the
# transcript, and without this the model never learned of them.
OUTCOMES_NOTE = "[system] Since your last reply: "


def _note_outcome(state, thread_id: str, text: str) -> None:
    actions = getattr(state, "actions", None)
    if actions is not None:
        actions.note_outcome(thread_id, text)


def _outcome_notes(history: list[dict]) -> list[str]:
    """The outcome notes the user turns of a provider history carry, in order."""
    notes = []
    for turn in history:
        if turn.get("role") != "user":
            continue
        content = turn.get("content")
        pieces = (
            [content]
            if isinstance(content, str)
            else [
                str(block.get("text") or "")
                for block in content or []
                if isinstance(block, dict) and block.get("type") == "text"
            ]
        )
        for piece in pieces:
            _, marker, note = piece.rpartition(OUTCOMES_NOTE)
            if marker:
                notes.append(note.removesuffix("."))
    return notes


def _validated_images(state, paths: list[str]) -> list[str]:
    """Check attached images before the stream opens: real, viewable, in the
    attachment area, and within the per-message budget."""
    cleaned: list[str] = []
    for raw in paths or []:
        rel = str(raw).strip()
        if not rel:
            continue
        if not is_attachable(rel):
            raise NoteError(400, f"{rel} must be an image, a PDF or a Word doc")
        # resolve_attachment rejects traversal and anything outside the folder.
        if not state.store.resolve_attachment(rel).is_file():
            raise NoteError(404, f"Attachment not found: {rel}")
        if rel not in cleaned:
            cleaned.append(rel)
    if len(cleaned) > MAX_MESSAGE_IMAGES:
        raise NoteError(400, f"Attach at most {MAX_MESSAGE_IMAGES} files per message")
    return cleaned


def _merge_settled(
    state,
    thread_id: str,
    message_id: str,
    confirmations: list[dict],
    actions: list[dict],
) -> tuple[list[dict], list[dict]]:
    """The turn's confirmations and actions, with what was settled on the stored
    message meanwhile carried over.

    A card can be confirmed, cancelled or undone while its reply is still being
    written. Those endpoints edit the stored message; the turn only holds its
    own copies, and writing them back as they were put a confirmed card back to
    pending and dropped the Undo card of the change it had made.
    """
    stored = next(
        (
            message
            for message in state.threads.get(thread_id).get("messages") or []
            if message.get("id") == message_id
        ),
        None,
    )
    if stored is None:
        return list(confirmations), list(actions)
    settled = {
        item.get("token"): item["status"]
        for item in stored.get("confirmations") or []
        if item.get("status")
    }
    merged_confirmations = [
        {**item, "status": settled[item.get("token")]}
        if item.get("token") in settled
        else item
        for item in confirmations
    ]
    undone = {
        item.get("id") for item in stored.get("actions") or [] if item.get("undone")
    }
    merged_actions = [
        {**item, "undoable": False, "undone": True}
        if item.get("id") in undone
        else item
        for item in actions
    ]
    known = {item.get("id") for item in actions}
    merged_actions.extend(
        item for item in stored.get("actions") or [] if item.get("id") not in known
    )
    return merged_confirmations, merged_actions


async def _run_thread_chat(
    state,
    thread_id: str,
    message: str,
    context_note: str | None,
    event_callback=None,
    auth_session_id: str | None = None,
    images: list[str] | None = None,
    inline_images: list[dict[str, str]] | None = None,
    ui_context: dict | None = None,
    selection_range: dict | None = None,
) -> dict:
    state.threads.get(thread_id)  # 404 for an unknown thread, before any append
    lock = state.threads.generation_lock(thread_id)
    async with lock:
        user_message = state.threads.append_public(
            thread_id,
            {
                "role": "user",
                "text": message,
                "context_note": context_note,
                "images": list(images or []),
                "selection_range": selection_range,
            },
        )
        assistant = state.threads.append_public(
            thread_id,
            {
                "role": "assistant",
                "text": "",
                "status": "streaming",
            },
        )
        state.autocommit.touch()
        if state.overseer is None:
            assistant = state.threads.update_message(
                thread_id,
                assistant["id"],
                text="No AI provider is configured. Add an API key in Settings to enable OMNIA.",
                status="error",
            )
            state.autocommit.touch()
            return {
                "session_id": thread_id,
                "reply": assistant["text"],
                "trace": [],
                "graphics": [],
                "pending_count": await state.pending.count(),
                "error": "unavailable",
                "user_message": user_message,
                "assistant_message": assistant,
            }

        # Provider-neutral tool history is persisted separately from the
        # compact public transcript so restarts retain tool-call context.
        # Read through the store so a thread broken by an earlier interruption
        # is repaired before it reaches the provider. chat() appends to this
        # very list, so it is what gets persisted: re-reading the sessions dict
        # afterwards picked up whatever another run had seeded there since.
        history = state.threads.provider_history(thread_id)
        state.overseer.sessions[thread_id] = history
        # Inside the user message, not a message of its own, so
        # history.user_turn_starts() still finds one turn per question; the
        # transcript keeps the text as typed.
        actions = getattr(state, "actions", None)
        outcomes = actions.drain_outcomes(thread_id) if actions is not None else []
        prompt = message
        if outcomes:
            prompt = (
                f"{message}\n\n" if message else ""
            ) + f"{OUTCOMES_NOTE}{'; '.join(outcomes)}."
        partial_text = ""
        partial_trace: list[dict] = []
        partial_actions: list[dict] = []
        partial_confirmations: list[dict] = []

        last_persist = 0.0

        def flush_partial() -> None:
            nonlocal last_persist
            last_persist = time.monotonic()
            confirmations, actions = _merge_settled(
                state, thread_id, assistant["id"], partial_confirmations, partial_actions
            )
            state.threads.update_message(
                thread_id,
                assistant["id"],
                text=partial_text,
                trace=list(partial_trace),
                actions=actions,
                confirmations=confirmations,
                status="streaming",
            )

        async def persist_event(event: str, value: dict):
            nonlocal partial_text
            if event == "delta":
                partial_text += str(value.get("text") or "")
                # Deltas arrive per token. Writing the transcript on each one
                # re-serialized the whole file thousands of times per reply for
                # a few extra characters; the SSE stream is the live channel,
                # and this copy only has to be current enough for a reload.
                if time.monotonic() - last_persist >= PERSIST_INTERVAL:
                    flush_partial()
            elif event == "tool_start":
                # Live only. It is the same call the "tool" event reports when
                # it finishes, so persisting both would double every row of the
                # stored trace on the next reload.
                pass
            elif event in ("tool", "action", "confirmation_required"):
                # Rare and load-bearing — the review tray reads these — so they
                # are never deferred.
                {
                    "tool": partial_trace,
                    "action": partial_actions,
                    "confirmation_required": partial_confirmations,
                }[event].append(value)
                flush_partial()
            state.autocommit.touch()
            if event_callback is not None:
                await event_callback(event, value)

        try:
            result = await state.overseer.chat(
                prompt,
                thread_id,
                context_note=context_note,
                event_callback=persist_event,
                auth_session_id=auth_session_id,
                images=images,
                inline_images=inline_images,
                ui_context=ui_context,
            )
        except asyncio.CancelledError:
            state.threads.set_provider_history(thread_id, list(history))
            state.threads.update_message(
                thread_id,
                assistant["id"],
                text=partial_text or "Response stopped.",
                status="interrupted",
            )
            state.autocommit.touch()
            raise
        except Exception as exc:
            # str(exc) alone is all the thread keeps — for a code bug that is
            # "'id'" with no traceback, thread or time. The ref ties what the
            # user sees to the logged traceback. One site covers both routes.
            ref = uuid.uuid4().hex[:8]
            log.exception("OMNIA turn failed thread=%s ref=%s", thread_id, ref)
            # The routes log what they swallow; this one is logged already.
            exc.omnia_ref = ref
            state.threads.set_provider_history(thread_id, list(history))
            state.threads.update_message(
                thread_id,
                assistant["id"],
                text=partial_text or f"OMNIA hit an error (ref {ref}): {exc}",
                status="error",
            )
            state.autocommit.touch()
            raise
        # The only persist path with an event loop and a finished turn: if this
        # history is long enough that saving it will trim exchanges away
        # permanently, summarize them first. Failures and a paused budget fall
        # through to the old lossy trim inside set_provider_history.
        history = await summarize_before_trim(state, list(history), MAX_HISTORY_CHARS)
        state.overseer.sessions[thread_id] = history
        state.threads.set_provider_history(thread_id, history)
        confirmations, actions = _merge_settled(
            state,
            thread_id,
            assistant["id"],
            result.get("confirmations") or [],
            result.get("actions") or [],
        )
        assistant = state.threads.update_message(
            thread_id,
            assistant["id"],
            text=result["reply"],
            status="complete",
            trace=result.get("trace") or [],
            graphics=result.get("graphics") or [],
            galleries=result.get("galleries") or [],
            actions=actions,
            confirmations=confirmations,
            usage=result.get("usage") or {},
        )
        state.autocommit.touch()
        return {
            **result,
            "user_message": user_message,
            "assistant_message": assistant,
        }


@router.get("/overseer/status")
async def overseer_status(request: Request):
    overseer = request.app.state.overseer
    provider = overseer.provider if overseer else None
    return {
        "available": overseer is not None,
        "provider": provider.name if provider else None,
        "model": provider.model if provider else None,
    }


# No UI caller (chat streams via /omnia/threads); pinned by tests/contracts/desktop-*.json.
@router.post("/overseer/chat")
async def overseer_chat(request: Request, body: ChatMessage):
    state = request.app.state
    thread_id = body.session_id
    if not thread_id:
        thread_id = state.threads.create()["id"]
    try:
        return await _run_thread_chat(
            state,
            thread_id,
            body.message,
            body.context_note,
            auth_session_id=_auth_session_id(request),
            ui_context=_ui_context(body),
            selection_range=body.selection_range,
        )
    except Exception as e:  # provider/auth/network errors → readable chat reply
        if not getattr(e, "omnia_ref", None):
            log.exception("OMNIA turn failed (thread %s)", thread_id)
        detail = str(e)
        lowered = detail.lower()
        if any(
            marker in lowered
            for marker in (
                "authentication",
                "invalid x-api-key",
                "incorrect api key",
                "api key not valid",
                "401",
            )
        ):
            detail = "The API key was rejected by the provider. Check it in Settings."
        return {
            "session_id": thread_id,
            "reply": f"OMNIA hit an error: {detail}",
            "trace": [],
            "pending_count": await request.app.state.pending.count(),
            "error": "provider_error",
        }


@router.get("/omnia/threads")
async def list_threads(request: Request):
    return {"threads": request.app.state.threads.list()}


@router.post("/omnia/threads")
async def create_thread(request: Request, body: ThreadCreate | None = None):
    title = body.title if body is not None else "New chat"
    note_path = _note_path(body.note_path if body is not None else None)
    store = request.app.state.threads
    if note_path:
        thread = store.create_for_note(note_path, title)
    else:
        thread = store.create(title=title)
    request.app.state.autocommit.touch()
    return _public_thread(thread)


@router.get("/omnia/note-thread")
async def note_thread(request: Request, path: str):
    """The conversation bound to a note, if any: how a device that never saw
    the thread made, or never saw the note move, finds it."""
    return {"id": request.app.state.threads.for_note(_note_path(path))}


@router.put("/omnia/note-thread")
async def bind_note_thread(request: Request, body: NoteThreadBind):
    """Bind a thread a device only remembered locally (made before notes had
    bindings). The first binding stands; the answer names the winner, or
    null when the thread belongs to another note."""
    store = request.app.state.threads
    note_path = _note_path(body.path)
    before = store.for_note(note_path)
    bound = store.bind_note(note_path, body.thread_id)
    if bound != before:
        request.app.state.autocommit.touch()
    return {"id": bound}


@router.get("/omnia/threads/{thread_id}")
async def get_thread(request: Request, thread_id: str):
    return _public_thread(request.app.state.threads.get(thread_id))


@router.patch("/omnia/threads/{thread_id}")
async def rename_thread(request: Request, thread_id: str, body: ThreadUpdate):
    thread = request.app.state.threads.rename(thread_id, body.title)
    request.app.state.autocommit.touch()
    return _public_thread(thread)


@router.delete("/omnia/threads/{thread_id}")
async def delete_thread(request: Request, thread_id: str):
    request.app.state.threads.delete(thread_id)
    request.app.state.autocommit.touch()
    return {"ok": True}


@router.delete("/omnia/threads/{thread_id}/messages/{message_id}")
async def truncate_thread(request: Request, thread_id: str, message_id: str):
    """Drop a message and everything after it, so a regenerated answer replaces
    the old one instead of being appended alongside it."""
    state = request.app.state
    # The lock covers the scheduled runs and the morning check too, which are
    # not registered runs: truncating under one had its history overwritten
    # by the untruncated copy the moment it finished.
    if state.threads.is_running(thread_id) or state.threads.generation_lock(thread_id).locked():
        raise NoteError(409, "That conversation is still generating")
    before = _outcome_notes(state.threads.provider_history(thread_id))
    thread = state.threads.truncate_from(thread_id, message_id)
    # The in-memory session mirrors the stored history; drop it so the next
    # message reloads the truncated version rather than the stale one.
    if state.overseer is not None:
        state.overseer.sessions.pop(thread_id, None)
    # A retried question takes the outcomes its old turn carried with it;
    # they go back in the queue for the resend. What is kept is a prefix, so
    # the notes past its count are the ones cut.
    after = _outcome_notes(state.threads.provider_history(thread_id))
    for note in before[len(after):]:
        _note_outcome(state, thread_id, note)
    state.autocommit.touch()
    return _public_thread(thread)


def _sse(event: str, value: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(value, ensure_ascii=False)}\n\n"


@router.post("/omnia/threads/{thread_id}/messages")
async def stream_thread_message(
    request: Request, thread_id: str, body: ThreadMessage
):
    # Validate before response headers are sent — once the stream opens, an
    # error can only be reported inside the event body.
    state = request.app.state
    state.threads.get(thread_id)
    images = _validated_images(state, body.images)
    if not body.message.strip() and not images:
        raise NoteError(400, "Message cannot be empty")
    queued = request.app.state.threads.generation_lock(thread_id).locked()
    # Read now: the run below may outlive this request's connection.
    auth_session_id = _auth_session_id(request)

    async def stream():
        # Unbounded, so nothing blocks produce() once no one is reading.
        queue: asyncio.Queue[tuple[str, dict] | None] = asyncio.Queue()

        async def emit(event: str, value: dict):
            await queue.put((event, value))

        async def produce():
            try:
                result = await _run_thread_chat(
                    state,
                    thread_id,
                    body.message.strip(),
                    body.context_note,
                    event_callback=emit,
                    auth_session_id=auth_session_id,
                    images=images,
                    ui_context=_ui_context(body),
                    selection_range=body.selection_range,
                )
                await queue.put(
                    (
                        "done",
                        {
                            "thread_id": thread_id,
                            "message": result.get("assistant_message"),
                            # So the client can settle its local copy of the
                            # question too, rather than reloading the thread.
                            "user_message": result.get("user_message"),
                            "pending_count": result.get("pending_count", 0),
                        },
                    )
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not getattr(exc, "omnia_ref", None):
                    log.exception("OMNIA turn failed (thread %s)", thread_id)
                thread = state.threads.get(thread_id)
                assistant = next(
                    (
                        message
                        for message in reversed(thread.get("messages") or [])
                        if message.get("role") == "assistant"
                    ),
                    None,
                )
                await queue.put(
                    ("error", {"detail": str(exc), "message": assistant})
                )
            finally:
                await queue.put(None)

        task = asyncio.create_task(produce())
        # Registered so POST .../stop can cancel it from any device. That is
        # the only cancel: a closed chat surface drops this reader, not the
        # turn, which finishes and persists on its own.
        state.threads.register_run(thread_id, task)
        task.add_done_callback(lambda done: state.threads.clear_run(thread_id, done))
        _detached_runs.add(task)
        task.add_done_callback(_detached_runs.discard)
        yield _sse("status", {"status": "queued" if queued else "working"})
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SECONDS)
            except TimeoutError:
                yield ": ping\n\n"
                continue
            if item is None:
                break
            yield _sse(item[0], item[1])

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


def _mark_confirmation(
    state,
    thread_id: str,
    token: str,
    status: str,
    new_actions: list[dict] | None = None,
) -> None:
    thread = state.threads.get(thread_id)
    for message in thread.get("messages") or []:
        confirmations = list(message.get("confirmations") or [])
        if not any(item.get("token") == token for item in confirmations):
            continue
        for item in confirmations:
            if item.get("token") == token:
                item["status"] = status
        actions = list(message.get("actions") or [])
        actions.extend(new_actions or [])
        state.threads.update_message(
            thread_id,
            message["id"],
            confirmations=confirmations,
            actions=actions,
        )
        break


@router.post("/omnia/confirmations/{token}/approve")
async def approve_omnia_confirmation(
    request: Request, token: str, body: OmniaActionBody
):
    state = request.app.state
    auth_session_id = _auth_session_id(request)
    item = state.actions.consume_confirmation(
        token, body.thread_id, auth_session_id
    )
    operations = (
        item["inputs"].get("operations") or []
        if item["tool"] == "bulk_actions"
        else [{"tool": item["tool"], "inputs": item["inputs"]}]
    )
    results: list[dict] = []
    actions: list[dict] = []
    summary = str(item.get("summary") or item["tool"]).removesuffix("?")
    for number, operation in enumerate(operations, 1):
        raw = await execute_tool(
            state,
            body.thread_id,
            operation["tool"],
            operation.get("inputs") or {},
            confirmed=True,
            auth_session_id=auth_session_id,
        )
        result = json.loads(raw)
        # A tool that stopped part-way returns its error beside the action
        # recording what it applied; the card carries that Undo too.
        action = result.pop("_action", None)
        if result.get("error"):
            _mark_confirmation(
                state, body.thread_id, token, "failed", [*actions, *([action] if action else [])]
            )
            which = f"operation {number} of {len(operations)}" if len(operations) > 1 else "it"
            applied = f" ({len(actions)} before it applied)" if actions else ""
            _note_outcome(
                state,
                body.thread_id,
                f'the user approved "{summary}" but {which} failed{applied}: '
                f"{str(result['error'])[:200]}",
            )
            raise NoteError(
                409,
                f"{result['error']} ({len(actions)} earlier action(s) were applied and remain undoable)",
            )
        if action:
            actions.append(action)
        results.append(result)
    _mark_confirmation(state, body.thread_id, token, "approved", actions)
    _note_outcome(state, body.thread_id, f'the user approved "{summary}" (applied)')
    state.autocommit.touch()
    return {
        "result": results[0] if len(results) == 1 else {"operations": results},
        "action": actions[0] if len(actions) == 1 else None,
        "actions": actions,
    }


@router.post("/omnia/confirmations/{token}/cancel")
async def cancel_omnia_confirmation(
    request: Request, token: str, body: OmniaActionBody
):
    state = request.app.state
    item = state.actions.cancel_confirmation(
        token, body.thread_id, _auth_session_id(request)
    )
    _mark_confirmation(state, body.thread_id, token, "cancelled")
    if item is not None:
        summary = str(item.get("summary") or item["tool"]).removesuffix("?")
        _note_outcome(state, body.thread_id, f'the user cancelled "{summary}"')
    state.autocommit.touch()
    return {"ok": True}


@router.post("/omnia/threads/{thread_id}/stop")
async def stop_thread_generation(request: Request, thread_id: str):
    """Cancel an in-flight generation server-side.

    Stop used to be a client-side fetch abort, so you could not stop a run from
    another tab or device, and closing the laptop was the only cancel signal.
    """
    stopped = request.app.state.threads.stop_run(thread_id)
    return {"stopped": stopped}


@router.get("/overseer/alerts")
async def overseer_alerts(request: Request, peek: bool = False):
    """Reminders that have come due and not yet been acknowledged.

    The client polls this while the app is open; the scheduler queues them at
    their real trigger time rather than inside the next morning greeting.
    With `peek` reading does not consume, and the client acks what it showed:
    clearing in the same GET let a response lost to a radio drop or a reload
    take the reminder with it. A plain GET still drains, because the desktop
    app bundles its own frontend and a build from before the ack would toast
    every queued alert again on each poll.
    """
    alerts = getattr(request.app.state, "alerts", None)
    if not alerts:
        return {"alerts": []}
    return {"alerts": alerts.peek() if peek else alerts.drain()}


@router.post("/overseer/alerts/ack")
async def ack_overseer_alerts(request: Request, body: AlertAckBody):
    """Drop alerts the client has shown."""
    alerts = getattr(request.app.state, "alerts", None)
    return {"acked": alerts.ack(body.keys) if alerts else 0}


@router.get("/omnia/actions")
async def list_omnia_actions(request: Request, limit: int = 100):
    """Everything OMNIA has done, newest first. The records were always written
    to disk; there was no route to read them, so nothing OMNIA did was
    reviewable after the fact outside the thread that did it."""
    return {"actions": request.app.state.actions.list(limit)}


@router.post("/omnia/actions/{action_id}/undo")
async def undo_omnia_action(
    request: Request, action_id: str, body: OmniaActionBody
):
    result = await request.app.state.actions.undo(action_id, body.thread_id)
    # The conversation may be gone — nightly housekeeping clears ones whose
    # whole substance was the action, and the action ledger outlives them. The
    # revert has already landed; marking it undone in the transcript is
    # bookkeeping, so a missing thread must not fail the request.
    try:
        thread = request.app.state.threads.get(body.thread_id)
    except NoteError:
        thread = None
    if thread is not None:
        # The model's own undo_last_action reads its result; this is the button.
        summary = request.app.state.actions.get(action_id).get("summary") or action_id
        _note_outcome(request.app.state, body.thread_id, f'the user undid "{summary}"')
    for message in (thread or {}).get("messages") or []:
        actions = list(message.get("actions") or [])
        if not any(action.get("id") == action_id for action in actions):
            continue
        for action in actions:
            if action.get("id") == action_id:
                action["undoable"] = False
                action["undone"] = True
        request.app.state.threads.update_message(
            body.thread_id, message["id"], actions=actions
        )
        break
    request.app.state.autocommit.touch()
    return result


# --- staged observations ----------------------------------------------------


async def _apply_observation(state, item: dict) -> None:
    """Turn a confirmed observation into the thing it was standing in for.

    Both paths go through the ordinary executors, so the write is recorded and
    undoable exactly as it would be had OMNIA done it in conversation. The
    thread it is attributed to is today's daily one, which is where the user
    answered the question.
    """
    from ..overseer.tool_expansion import remember

    try:
        tz = ZoneInfo(getattr(state, "timezone", "America/New_York"))
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo("America/New_York")
    thread_id = f"daily-{datetime.now(tz).date().isoformat()}"

    if item.get("kind") == "habit" and item.get("habit_key") and item.get("habit_value"):
        await execute_tool(
            state,
            thread_id,
            "update_habit",
            {
                "key": item["habit_key"],
                "value": item["habit_value"],
                "event": "set",
                "source": "observation",
            },
        )
        return
    # kind="memory", or a habit observation with no value to store: the text is
    # what the user actually agreed to, so keep that rather than nothing.
    await remember(state, thread_id, {"text": item.get("text") or ""})


@router.post("/overseer/observations/{observation_id}/confirm")
async def confirm_observation(request: Request, observation_id: str):
    state = request.app.state
    # Applied first, marked second: if the write fails the user's answer is
    # still waiting for them rather than silently consumed.
    item = observations.get_pending(state, observation_id)
    await _apply_observation(state, item)
    observations.settle(state, observation_id, "confirmed")
    state.autocommit.touch()
    return {"ok": True}


@router.post("/overseer/observations/{observation_id}/dismiss")
async def dismiss_observation(request: Request, observation_id: str):
    """Decline a suggestion. A habit key declined here is not raised again for
    months — the tombstone is the whole point of the endpoint."""
    state = request.app.state
    observations.settle(state, observation_id, "dismissed")
    state.autocommit.touch()
    return {"ok": True}


# --- morning greeting (first open of the day) -------------------------------

# Daily threads whose greeting is being generated right now. Generation takes
# a model round (~15s); a reload in that window must not start a second one.
_greeting_inflight: set[str] = set()


def _greeting_text(thread: dict) -> str | None:
    """The day's greeting — the ``greeting`` the morning check stamped on the
    thread. Scheduled runs write their status lines into the same daily thread
    hours before the first open, so "first assistant message" stopped being the
    greeting; threads from before the stamp existed fall back to that scan.

    Enforces the same contract as generation: a greeting that does not begin
    with "Morning" (a stray "Reviewed." from an earlier build) is shown as a
    plain "Morning." without rewriting the stored transcript.
    """
    def sanitized(text: str) -> str:
        return text if text.strip().lower().startswith("morning") else "Morning."

    greeting = thread.get("greeting")
    if greeting:
        return sanitized(str(greeting))
    for message in thread.get("messages") or []:
        if message.get("role") == "assistant" and message.get("text"):
            return sanitized(message["text"])
    return None


def digest_window(today: date_cls) -> int:
    """How far back the morning digest looks: a week on Monday, a day
    otherwise. Shared so the greeting prompt and briefing.stats count over the
    same window rather than quietly disagreeing."""
    return 7 if today.weekday() == 0 else 1


async def _briefing_stats(state, today: date_cls, digest: dict) -> dict:
    """The numbers under the briefing. Every one comes from a store or helper
    that already computes it — no new model call, no new query surface — and
    every key is always present, zeroed, so the banner never has to guess.
    """
    from ..flashcards import deck_stats

    decks = state.flashcards.list() if hasattr(state, "flashcards") else []
    yesterday = today - timedelta(days=1)
    hours = 0.0
    if hasattr(state, "activities"):
        hours = float(
            state.activities.range_totals(yesterday, yesterday).get(
                yesterday.isoformat(), 0.0
            )
        )
    media_rows = await state.db.execute_fetchall(
        "SELECT COUNT(*) AS c FROM media_items WHERE status = 'active'"
    )
    return {
        "notes_created": len(digest.get("notes_created") or []),
        "notes_modified": len(digest.get("notes_modified") or []),
        # The list is capped at 40; the digest carries the real count beside it.
        "tasks_completed": digest.get(
            "tasks_completed_count", len(digest.get("tasks_completed_recently") or [])
        ),
        "flashcards_due": sum(int(deck_stats(deck).get("due") or 0) for deck in decks),
        "activity_hours_yesterday": round(hours, 2),
        "pending_changes": await state.pending.count(),
        "media_in_progress": int(media_rows[0]["c"]) if media_rows else 0,
    }


async def build_briefing(
    request: Request, today: date_cls, digest: dict | None = None
) -> dict:
    """The at-a-glance daily briefing: today's events, in-progress tasks, the
    coming week's tasks/events grouped by day, and the day's stats.

    Built from the same task-row / event-occurrence / calendar-range machinery
    the calendar page uses, so the briefing never re-derives any of it. `digest`
    is the get_digest_data payload the greeting also uses — passed in so the
    window is computed once and both halves agree.
    """
    from .calendar import _task_rows, calendar_range
    from ..task_service import public_task

    state = request.app.state
    if digest is None:
        digest = await get_digest_data(
            state, "morning", {"days": digest_window(today)}
        )

    today_events = (
        state.events.occurrences(today, today)
        if hasattr(state, "events")
        else []
    )

    rows = await _task_rows(request)
    in_progress = [
        public_task(row)
        for row in rows
        if row.get("status") == "in_progress"
        and not row.get("tombstone")
        and not row.get("board")
    ]

    # The seven days after today, grouped. calendar_range hands back due_tasks
    # (each carrying `due`) and event occurrences (each carrying
    # `occurrence_date`); bucket both by day so empty days still render.
    first = today + timedelta(days=1)
    last = today + timedelta(days=7)
    window = await calendar_range(request, first.isoformat(), last.isoformat())
    tasks_by_day: dict[str, list] = {}
    for task in window["due_tasks"]:
        tasks_by_day.setdefault(task["due"], []).append(task)
    events_by_day: dict[str, list] = {}
    for event in window["events"]:
        events_by_day.setdefault(event["occurrence_date"], []).append(event)
    week = []
    cursor = first
    while cursor <= last:
        day = cursor.isoformat()
        week.append(
            {
                "date": day,
                "tasks": tasks_by_day.get(day, []),
                "events": events_by_day.get(day, []),
            }
        )
        cursor += timedelta(days=1)

    backup = getattr(state, "backup", None)
    return {
        "today_events": today_events,
        "in_progress": in_progress,
        "week": week,
        "stats": await _briefing_stats(state, today, digest),
        # The off-site copy is the one thing a quiet failure costs for good.
        "backup_stale": bool(backup and backup.stale),
        "backup_failing_since": backup.failing_since if backup else None,
    }


@router.post("/overseer/morning")
async def morning_check(request: Request):
    """First open of the day: greet, and hand back the daily briefing.

    The greeting itself is still generated at most once per calendar day
    (proactive → suggestion-tier only, quiet when nothing's up). The briefing
    payload is returned on every open so the banner can re-show until the user
    dismisses it for the day.
    """
    state = request.app.state
    now = local_now(state)
    today = now.date().isoformat()
    title = now.strftime("%A, %B %-d")
    thread, created = state.threads.ensure_daily(today, title)

    # One digest for the whole request: the briefing's stats and the greeting
    # prompt below are counted over the same window. Monday gets a week's view
    # rather than just today — get_digest_data has always accepted `days` and
    # was only ever called with 1, so the documented weekly digest never
    # actually existed.
    window = digest_window(now.date())
    weekly = window == 7
    digest = await get_digest_data(state, "morning", {"days": window})
    briefing = await build_briefing(request, now.date(), digest)

    # The staged observation rides on every open, greeted or not: the banner
    # shows it beside the briefing, and a reload must not lose it.
    observation = observations.public(
        observations.surfaced(observations.prune(observations.load(state)))
    )

    # Already greeted today: skip regeneration (never re-invoke the overseer on
    # a reload), but still return the persisted greeting + the fresh briefing.
    # Keyed on the greeting stamp, NOT on the thread having messages: the
    # 04:00 scheduled runs write status lines into this same thread hours
    # before the first open, and "has messages" silently swallowed the
    # greeting on every scheduled day. The in-flight set keeps two rapid
    # opens (reload during the ~15s generation) from greeting twice.
    if not created and (
        thread.get("greeting") or thread["id"] in _greeting_inflight
    ):
        return {
            "greet": False,
            "message": _greeting_text(thread),
            "thread_id": thread["id"],
            "briefing": briefing,
            "observation": observation,
        }
    _greeting_inflight.add(thread["id"])

    try:
        overdue = digest["tasks"]["overdue"]
        due_today = digest["tasks"]["due_today"]
        events_today = digest.get("events_today") or []
        reminders = (digest.get("event_reminders") or []) + (
            digest.get("missed_event_reminders") or []
        )

        # get_digest_data has always returned what happened recently as well as
        # what is due; the greeting only ever saw the due half and the rest was
        # discarded. Compact — titles and lines, capped — plus the briefing's
        # counts, so a quiet day can be told from a busy one.
        recent = {
            "notes_created": [
                note.get("title") or note.get("path")
                for note in (digest.get("notes_created") or [])[:10]
            ],
            "notes_modified": [
                note.get("title") or note.get("path")
                for note in (digest.get("notes_modified") or [])[:10]
            ],
            "tasks_completed": [
                task.get("text")
                for task in (digest.get("tasks_completed_recently") or [])[:10]
            ],
            "upcoming_events": (digest.get("upcoming_events") or [])[:10],
            "counts": briefing["stats"],
        }

        message = None
        if state.overseer is not None:
            try:
                # Under the thread's generation lock, like a user turn: a
                # greeting generated beside one on the same daily thread
                # replaced its history with a fresh copy from disk, and
                # whichever persisted last dropped the other exchange.
                async with state.threads.generation_lock(thread["id"]), run_scope("morning"):
                    history = state.threads.provider_history(thread["id"])
                    state.overseer.sessions[thread["id"]] = history
                    result = await state.overseer.chat(
                        "(Internal morning check-in — the user just opened the app for the "
                        f"first time today, {today}."
                        + (
                            " It is Monday, so this digest covers the week ahead: it is"
                            " worth a sentence about what is coming up, not just today."
                            if weekly
                            else ""
                        )
                        + ") Digest data: "
                        + json.dumps(
                            {
                                "overdue": overdue,
                                "due_today": due_today,
                                "events_today": events_today,
                                "event_reminders": reminders,
                                **recent,
                            },
                            ensure_ascii=False,
                        )
                        + " The recent-activity lists and counts are context for judging "
                        "what is worth raising — cards due, hours logged, unreviewed "
                        "changes, what moved yesterday. They are NOT a checklist: most "
                        "days none of it needs a mention."
                        + " Your learned habits are already in the system prompt — use "
                        "them to decide what is worth verifying (e.g. whether yesterday's "
                        "daily note has what it usually has) and check those notes if "
                        "warranted; do not spend a round re-reading them. Then greet per your "
                        'persona examples: something worth raising → "Morning — …" with the '
                        'items, one line. Nothing worth raising → exactly "Morning." Do not '
                        "manufacture content. Any checking you do is silent: your entire "
                        'reply is the greeting the user sees, and it MUST begin with "Morning". '
                        'Never report the check itself — no "Reviewed.", "Checked.", "Done.", '
                        "or similar status word.",
                        session_id=thread["id"],
                        proactive=True,
                    )
                    # The closing round alone: narration before a tool call
                    # would push "Morning" off the front of the joined reply.
                    message = result.get("final_text", result["reply"])
                    state.threads.set_provider_history(thread["id"], list(history))
                # Guard the contract regardless of what the model returned: the
                # greeting must begin with "Morning". A stray status like "Reviewed."
                # (the model acknowledging the silent check instead of greeting) is
                # dropped so the code-built greeting below takes over.
                if not (message or "").strip().lower().startswith("morning"):
                    message = None
            except Exception:
                # Without this line a revoked key or a code bug turned every
                # morning into the canned greeting with nothing to say why.
                log.warning(
                    "morning check model call failed (thread %s); using the code-built greeting",
                    thread["id"],
                    exc_info=True,
                )
                message = None  # fall through to the code-built greeting

        if not message:
            parts = []
            if overdue:
                parts.append(f"{len(overdue)} overdue task{'s' if len(overdue) != 1 else ''}")
            if due_today:
                parts.append(f"{len(due_today)} task{'s' if len(due_today) != 1 else ''} due today")
            if events_today:
                parts.append(f"{len(events_today)} event{'s' if len(events_today) != 1 else ''} today")
            if reminders:
                parts.append(f"{len(reminders)} event reminder{'s' if len(reminders) != 1 else ''}")
            message = "Morning" + (" — " + ", ".join(parts) + "." if parts else ".")

        # Only mark reminder occurrences after the greeting was successfully built.
        # `now` must be the user's clock: digest's default is the server's naive
        # now (UTC in the container), which consumed reminders due up to 4-5 h
        # later so the ticker never pushed them at their real time.
        state.events.digest(now.date(), mark_surfaced=True, now=local_now(state))
        state.autocommit.touch()

        state.threads.append_public(
            thread["id"], {"role": "assistant", "text": message}
        )
        state.threads.set_greeting(thread["id"], message)
        state.autocommit.touch()
    finally:
        _greeting_inflight.discard(thread["id"])
    return {
        "greet": True,
        "message": message,
        "thread_id": thread["id"],
        "briefing": briefing,
        "observation": observation,
        "pending_count": await state.pending.count(),
    }


# --- AI configuration (Settings UI) ----------------------------------------


@router.get("/overseer/models")
async def list_models():
    return {"providers": PROVIDERS_PUBLIC}


# PROVIDERS without anything we wouldn't want to ship (currently identical,
# but keys never live here anyway).
PROVIDERS_PUBLIC = [
    {k: p[k] for k in ("id", "label", "key_placeholder", "default_model", "models")}
    for p in PROVIDERS
]


def _key_status(state, provider_id: str) -> dict:
    ui_key = (state.ai_config.get("keys") or {}).get(provider_id)
    key = ui_key or env_key(state.settings, provider_id)
    return {
        "set": bool(key),
        "hint": f"…{key[-4:]}" if key else None,
        "source": "ui" if ui_key else ("env" if key else None),
    }


@router.get("/overseer/config")
async def get_ai_config(request: Request):
    state = request.app.state
    provider_id, _key, model = effective_ai(state.settings, state.ai_config)
    return {
        "provider": provider_id,
        "model": model,
        "reasoning": reasoning_level(state.ai_config),
        "available": state.overseer is not None,
        "keys": {p["id"]: _key_status(state, p["id"]) for p in PROVIDERS},
        "models": {
            p["id"]: (state.ai_config.get("models") or {}).get(p["id"])
            or env_model(state.settings, p["id"])
            or p["default_model"]
            for p in PROVIDERS
        },
    }


@router.post("/overseer/config")
async def save_ai_config_route(request: Request, body: AIConfigBody):
    state = request.app.state
    # From disk, not the boot-time copy: that is {} when ai.json could not be
    # read at boot, and {} plus this change saved would bury the keys.
    config = dict(load_ai_config(state.settings.data_dir))
    # Copies, not setdefault: that kept the live dicts, so a key written here
    # landed in state.ai_config even when a later check answered 400 and
    # nothing was saved — memory and disk disagreed until restart.
    config["keys"] = dict(config.get("keys") or {})
    config["models"] = dict(config.get("models") or {})

    if body.provider is not None:
        if not provider_info(body.provider):
            raise NoteError(400, f"Unknown provider: {body.provider}")
        config["provider"] = body.provider

    active = config.get("provider") or state.settings.ai_provider
    info = provider_info(active) or provider_info("anthropic")

    if body.clear_key:
        config["keys"].pop(active, None)
    elif body.api_key:
        key = body.api_key.strip()
        if not api_key_matches(active, key):
            raise NoteError(
                400,
                f"That doesn't look like a {info['label']} API key "
                f"({info['key_placeholder']}).",
            )
        config["keys"][active] = key

    if body.model is not None:
        if body.model not in model_ids(active):
            raise NoteError(400, f"Unknown {info['label']} model: {body.model}")
        config["models"][active] = body.model

    if body.reasoning is not None:
        if body.reasoning not in REASONING_LEVELS:
            raise NoteError(400, f"Unknown reasoning level: {body.reasoning}")
        # One knob across providers — it maps to whatever each accepts.
        config["reasoning"] = body.reasoning

    save_ai_config(state.settings.data_dir, config)
    state.ai_config = config

    # Rebuild the provider live — no restart needed. Chat history carries over
    # (the neutral message format works across providers).
    provider_id, key, model = effective_ai(state.settings, config)
    provider = build_provider(
        state.settings,
        provider_id=provider_id,
        api_key=key,
        model=model,
        reasoning=reasoning_level(config),
    )
    if provider:
        old_sessions = state.overseer.sessions if state.overseer else {}
        state.overseer = Overseer(provider, state)
        state.overseer.sessions = old_sessions
    else:
        state.overseer = None

    return await get_ai_config(request)


# --- pending changes --------------------------------------------------------


@router.get("/pending")
async def list_pending(request: Request):
    pending = request.app.state.pending
    changes = await pending.list_changes()
    return [await pending.describe(c, changes) for c in changes]


@router.get("/pending/count")
async def pending_count(request: Request):
    return {"count": await request.app.state.pending.count()}


@router.post("/pending/{change_id}/approve")
async def approve(request: Request, change_id: int, body: ApproveBody | None = None):
    return await request.app.state.pending.approve(change_id, force=body.force if body else False)


@router.post("/pending/{change_id}/discard")
async def discard(request: Request, change_id: int):
    await request.app.state.pending.discard(change_id)
    return {"ok": True}


@router.post("/pending-approve-all")
async def approve_all(request: Request, body: ApproveBody | None = None):
    return await request.app.state.pending.approve_all(
        force=body.force if body else False, ids=body.ids if body else None
    )


@router.post("/pending-discard-all")
async def discard_all(request: Request):
    n = await request.app.state.pending.discard_all()
    return {"discarded": n}


# --- semantic search & index ------------------------------------------------


@router.get("/search/semantic/status")
async def semantic_status(request: Request):
    # {available, error, since}: why it is off, not only that it is.
    semantic = request.app.state.semantic
    return {"available": semantic.available, **semantic.error}


# No UI caller (search goes through /search/unified); pinned by tests/contracts/desktop-*.json.
@router.get("/search/semantic")
async def semantic_search(request: Request, q: str, k: int = 10):
    results = await request.app.state.semantic.search(q, k=min(k, 30))
    return [
        {
            "path": r["path"],
            "title": r["path"].rsplit("/", 1)[-1].removesuffix(".md"),
            "snippet": r["text"][:300],
            "score": r["score"],
        }
        for r in results
    ]


@router.post("/index/rebuild")
async def rebuild_index(request: Request):
    state = request.app.state
    indexed = await state.indexer.sync_all(force=True)
    documents = [(n.path, state.store.read(n.path)) for n in state.store.list_notes()]
    # Paper rides along: a rebuild that dropped every handwriting vector while
    # rebuilding the note ones would quietly halve semantic search.
    paper_index = getattr(state, "paper_index", None)
    pages = 0
    if paper_index is not None:
        pages = await paper_index.sync_all(force=True)
        documents += await paper_index.semantic_documents()
    # Conversation search is the third keyword index; boot only re-derives it
    # on an INDEX_VERSION change, so Rebuild is the manual path for it too.
    # Under the write lock: its own sqlite3 connection would otherwise block
    # the event loop behind a transaction the loop has to finish.
    messages = 0
    thread_index = getattr(state, "thread_index", None)
    threads = getattr(state, "threads", None)
    if thread_index is not None and threads is not None:
        conversations = threads.list(include_messages=True)
        async with state.indexer.write_lock:
            messages = thread_index.rebuild(conversations)
    chunks = await state.semantic.rebuild(documents)
    # `semantic` is false with `semantic_error` set when the rebuild could not
    # embed at all; Settings reported that as a successful reindex.
    return {
        "notes_indexed": indexed,
        "paper_pages_indexed": pages,
        "conversation_messages_indexed": messages,
        "chunks": chunks,
        "semantic": state.semantic.available,
        "semantic_error": state.semantic.error["error"],
        "semantic_failed": len(state.semantic.last_rebuild_errors),
    }
