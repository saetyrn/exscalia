import asyncio
from contextlib import suppress
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..overseer.agent import MAX_MESSAGE_IMAGES, Overseer
from ..overseer.provider import build_provider
from ..overseer.tools import execute_tool
from ..overseer.vision import is_attachable
from ..overseer.runtime_config import (
    PROVIDERS,
    REASONING_LEVELS,
    api_key_matches,
    effective_ai,
    env_key,
    env_model,
    model_ids,
    provider_info,
    reasoning_level,
    save_ai_config,
)
from ..vault.internal import iso_utc
from ..vault.notes import NoteError

router = APIRouter(prefix="/api", tags=["overseer"])

# How often a streaming reply is written to its transcript. The live text goes
# out over SSE; this file only has to be current enough that a reload or a
# crash mid-reply loses no more than this much.
PERSIST_INTERVAL = 0.25


class ChatMessage(BaseModel):
    message: str
    session_id: str | None = None
    context_note: str | None = None


class ThreadCreate(BaseModel):
    title: str = "New chat"


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
    # The editor selection's exact offsets (from/to/head/doc_len/path). Unlike
    # the prompt-only fields above, this IS persisted onto the user message so
    # selection-editing tools can act on the precise range.
    selection_range: dict | None = None


def _ui_context(body) -> dict | None:
    context = {
        "selection": getattr(body, "selection", None),
        "view": getattr(body, "view", None),
        "open_tabs": getattr(body, "open_tabs", None),
    }
    return context if any(context.values()) else None


class ApproveBody(BaseModel):
    force: bool = False


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


def _validated_images(state, paths: list[str]) -> list[str]:
    """Check attached images before the stream opens: real, viewable, in the
    attachment area, and within the per-message budget."""
    cleaned: list[str] = []
    for raw in paths or []:
        rel = str(raw).strip()
        if not rel:
            continue
        if not is_attachable(rel):
            raise NoteError(400, f"{rel} must be an image or a PDF")
        # resolve_attachment rejects traversal and anything outside the folder.
        if not state.store.resolve_attachment(rel).is_file():
            raise NoteError(404, f"Attachment not found: {rel}")
        if rel not in cleaned:
            cleaned.append(rel)
    if len(cleaned) > MAX_MESSAGE_IMAGES:
        raise NoteError(400, f"Attach at most {MAX_MESSAGE_IMAGES} files per message")
    return cleaned


async def _run_thread_chat(
    state,
    thread_id: str,
    message: str,
    context_note: str | None,
    event_callback=None,
    auth_session_id: str | None = None,
    images: list[str] | None = None,
    ui_context: dict | None = None,
    selection_range: dict | None = None,
) -> dict:
    thread = state.threads.get(thread_id)
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
        # is repaired before it reaches the provider.
        state.overseer.sessions[thread_id] = state.threads.provider_history(thread_id)
        partial_text = ""
        partial_trace: list[dict] = []
        partial_actions: list[dict] = []
        partial_confirmations: list[dict] = []

        last_persist = 0.0

        def flush_partial() -> None:
            nonlocal last_persist
            last_persist = time.monotonic()
            state.threads.update_message(
                thread_id,
                assistant["id"],
                text=partial_text,
                trace=list(partial_trace),
                actions=list(partial_actions),
                confirmations=list(partial_confirmations),
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
                message,
                thread_id,
                context_note=context_note,
                event_callback=persist_event,
                auth_session_id=auth_session_id,
                images=images,
                ui_context=ui_context,
            )
        except asyncio.CancelledError:
            state.threads.set_provider_history(
                thread_id, list(state.overseer.sessions.get(thread_id) or [])
            )
            state.threads.update_message(
                thread_id,
                assistant["id"],
                text=partial_text or "Response stopped.",
                status="interrupted",
            )
            state.autocommit.touch()
            raise
        except Exception as exc:
            state.threads.set_provider_history(
                thread_id, list(state.overseer.sessions.get(thread_id) or [])
            )
            state.threads.update_message(
                thread_id,
                assistant["id"],
                text=partial_text or f"OMNIA hit an error: {exc}",
                status="error",
            )
            state.autocommit.touch()
            raise
        state.threads.set_provider_history(
            thread_id, list(state.overseer.sessions.get(thread_id) or [])
        )
        assistant = state.threads.update_message(
            thread_id,
            assistant["id"],
            text=result["reply"],
            status="complete",
            trace=result.get("trace") or [],
            graphics=result.get("graphics") or [],
            galleries=result.get("galleries") or [],
            actions=result.get("actions") or [],
            confirmations=result.get("confirmations") or [],
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
        )
    except Exception as e:  # provider/auth/network errors → readable chat reply
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
    thread = request.app.state.threads.create(
        title=body.title if body is not None else "New chat"
    )
    request.app.state.autocommit.touch()
    return _public_thread(thread)


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
    if state.threads.is_running(thread_id):
        raise NoteError(409, "That conversation is still generating")
    thread = state.threads.truncate_from(thread_id, message_id)
    # The in-memory session mirrors the stored history; drop it so the next
    # message reloads the truncated version rather than the stale one.
    if state.overseer is not None:
        state.overseer.sessions.pop(thread_id, None)
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

    async def stream():
        queue: asyncio.Queue[tuple[str, dict] | None] = asyncio.Queue()

        async def emit(event: str, value: dict):
            await queue.put((event, value))

        async def produce():
            try:
                result = await _run_thread_chat(
                    request.app.state,
                    thread_id,
                    body.message.strip(),
                    body.context_note,
                    event_callback=emit,
                    auth_session_id=_auth_session_id(request),
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
                            "pending_count": result.get("pending_count", 0),
                        },
                    )
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                thread = request.app.state.threads.get(thread_id)
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
        # Registered so POST .../stop can cancel it from any device.
        state.threads.register_run(thread_id, task)
        yield _sse("status", {"status": "queued" if queued else "working"})
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield _sse(item[0], item[1])
        finally:
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            state.threads.clear_run(thread_id, task)

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
    for operation in operations:
        raw = await execute_tool(
            state,
            body.thread_id,
            operation["tool"],
            operation.get("inputs") or {},
            confirmed=True,
            auth_session_id=auth_session_id,
        )
        result = json.loads(raw)
        if result.get("error"):
            _mark_confirmation(state, body.thread_id, token, "failed", actions)
            raise NoteError(
                409,
                f"{result['error']} ({len(actions)} earlier action(s) were applied and remain undoable)",
            )
        action = result.pop("_action", None)
        if action:
            actions.append(action)
        results.append(result)
    _mark_confirmation(state, body.thread_id, token, "approved", actions)
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
    state.actions.cancel_confirmation(
        token, body.thread_id, _auth_session_id(request)
    )
    _mark_confirmation(state, body.thread_id, token, "cancelled")
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
async def overseer_alerts(request: Request):
    """Reminders that have come due since the last check, shown once.

    The client polls this while the app is open; the scheduler queues them at
    their real trigger time rather than inside the next morning greeting.
    """
    alerts = getattr(request.app.state, "alerts", None)
    return {"alerts": alerts.drain() if alerts else []}


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


# --- morning greeting (first open of the day) -------------------------------


@router.post("/overseer/morning")
async def morning_check(request: Request):
    """Once per calendar day: assemble what's worth raising and greet.

    Proactive → suggestion-tier only. Quiet when nothing's up.
    """
    state = request.app.state
    try:
        tz = ZoneInfo(state.timezone)
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("America/New_York")
    now = datetime.now(tz)
    today = now.date().isoformat()
    title = now.strftime("%A, %B %-d")
    thread, created = state.threads.ensure_daily(today, title)
    if not created and thread.get("messages"):
        return {"greet": False, "thread_id": thread["id"]}

    from ..overseer.tools import get_digest_data

    # Monday gets a week's view rather than just today: get_digest_data
    # has always accepted `days` and was only ever called with 1, so the
    # documented weekly digest never actually existed.
    weekly = now.date().weekday() == 0
    digest = await get_digest_data(state, "morning", {"days": 7 if weekly else 1})
    overdue = digest["tasks"]["overdue"]
    due_today = digest["tasks"]["due_today"]
    events_today = digest.get("events_today") or []
    reminders = (digest.get("event_reminders") or []) + (
        digest.get("missed_event_reminders") or []
    )

    message = None
    if state.overseer is not None:
        try:
            state.overseer.sessions[thread["id"]] = state.threads.provider_history(
                thread["id"]
            )
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
                    },
                    ensure_ascii=False,
                )
                + " Your learned habits are already in the system prompt — use "
                "them to decide what is worth verifying (e.g. whether yesterday's "
                "daily note has what it usually has) and check those notes if "
                "warranted; do not spend a round re-reading them. Then greet per your "
                'persona examples: something worth raising → "Morning — …" with the '
                'items, one line. Nothing worth raising → exactly "Morning." Do not '
                "manufacture content.",
                session_id=thread["id"],
                proactive=True,
            )
            message = result["reply"]
            state.threads.set_provider_history(
                thread["id"],
                list(state.overseer.sessions.get(thread["id"]) or []),
            )
        except Exception:
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
    state.events.digest(now.date(), mark_surfaced=True)
    state.autocommit.touch()

    state.threads.append_public(
        thread["id"], {"role": "assistant", "text": message}
    )
    state.autocommit.touch()
    return {
        "greet": True,
        "message": message,
        "thread_id": thread["id"],
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
    config = dict(state.ai_config)
    config.setdefault("keys", dict(config.get("keys") or {}))
    config.setdefault("models", dict(config.get("models") or {}))

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
    return await request.app.state.pending.approve_all(force=body.force if body else False)


@router.post("/pending-discard-all")
async def discard_all(request: Request):
    n = await request.app.state.pending.discard_all()
    return {"discarded": n}


# --- semantic search & index ------------------------------------------------


@router.get("/search/semantic/status")
async def semantic_status(request: Request):
    return {"available": request.app.state.semantic.available}


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
    notes = [(n.path, state.store.read(n.path)) for n in state.store.list_notes()]
    chunks = await state.semantic.rebuild(notes)
    return {"notes_indexed": indexed, "chunks": chunks, "semantic": state.semantic.available}
