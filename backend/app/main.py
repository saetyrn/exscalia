import asyncio
import base64
import contextlib
import hashlib
import logging
import logging.handlers
import mimetypes
import re
import signal
import threading
import uuid
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from .activity_store import ActivityStore
from .api import activities as activities_api
from .api import attachments as attachments_api
from .api import auth as auth_api
from .api import autocorrect as autocorrect_api
from .api import calendar as calendar_api
from .api import events as events_api
from .api import flashcards as flashcards_api
from .api import media as media_api
from .api import meta as meta_api
from .api import notes as notes_api
from .api import paper as paper_api
from .api import proofread as proofread_api
from .api import push as push_api
from .api import live as live_api
from .api import overseer as overseer_api
from .api import recovery as recovery_api
from .api import tasks as tasks_api
from .body_limit import MB, BodyLimitMiddleware, reject_oversized_bodies
from .config import DEFAULT_SESSION_SECRET, Settings, get_settings
from .event_store import EventStore
from .index.db import (
    INDEX_VERSION,
    index_version,
    open_db,
    stamp_index_version,
)
from .live import LiveBus
from .proofread import ProofreadStore
from .index.indexer import Indexer
from .index.paper_index import PaperIndex
from .index.semantic import SemanticIndex
from .index.thread_index import ThreadIndex
from .overseer.agent import Overseer
from .overseer import habits
from .overseer.memory import MEMORY_PATH, MEMORY_TEMPLATE
from .overseer.actions import ActionCoordinator
from .overseer.housekeeping import (
    DEFAULT_ENABLED as HOUSEKEEPING_DEFAULT,
    housekeeping_ticker,
)
from .overseer.provider import build_provider
from .overseer.runtime_config import effective_ai, load_ai_config, reasoning_level
from .overseer.usage import UsageLedger
from .flashcards import FlashcardStore
from .overseer.threads import ThreadStore
from .paper_store import PaperStore
from .pending.store import PendingStore
from .task_service import MIGRATION_VERSION, migrate_task_metadata
from .alerts import TICK_SECONDS, AlertQueue
from .alerts import reminder_tick as queue_due_reminders
from . import media_theme, push_service
from .scheduler import SCHEDULE_TICK, run_due_schedules
from .vault.internal import TaskTombstones, TrashStore, read_json
from .vault import fm
from .vault.notes import NoteError, NoteStore
from .vault.repo import AutoCommitter, BackupPusher, VaultRepo

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
log = logging.getLogger(__name__)

# Names the WARNING+ file handler on the root logger (see attach_log_file).
LOG_FILE_HANDLER = "omnia-file"

# Python's mimetypes table predates the PWA manifest type; without this the
# manifest serves as octet-stream and browsers refuse to install the app.
mimetypes.add_type("application/manifest+json", ".webmanifest")


async def maintenance_once(state) -> None:
    """Slow housekeeping nothing should ever wait on: expired trash, the
    vector index's fragment garbage, and the SQLite WAL (which otherwise
    grows unbounded — it sat at 3 MB against a 128 KB database).

    Each step is guarded on its own: a purge error used to end the loop for
    the life of the process, silently taking compaction and WAL truncation
    with it. ``semantic.optimize`` catches its own failures."""
    try:
        if state.trash.purge_expired():
            state.autocommit.touch()
    except Exception:
        log.exception("trash purge failed")
    await state.semantic.optimize()
    try:
        await state.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:
        log.warning("wal checkpoint failed", exc_info=True)

def set_on_stop_signal(event: asyncio.Event) -> None:
    """Set `event` when the server is told to stop, not when lifespan shutdown
    finally runs.

    uvicorn runs lifespan shutdown only after every open response has finished,
    and /api/live never finishes on its own, so an event set there would fire
    after --timeout-graceful-shutdown had already cancelled the streams (and
    the dev server's reload hung on them outright). uvicorn installs its
    SIGINT/SIGTERM handlers before lifespan startup; chaining onto them ends
    the streams first, so shutdown proceeds at once."""
    if threading.current_thread() is not threading.main_thread():
        return  # signals are main-thread only; TestClient runs us elsewhere
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        previous = signal.getsignal(sig)
        if not callable(previous):
            continue

        def handler(signum, frame, previous=previous):
            with contextlib.suppress(RuntimeError):  # loop already closed
                loop.call_soon_threadsafe(event.set)
            previous(signum, frame)

        signal.signal(sig, handler)


def attach_log_file(data_dir: Path) -> logging.Handler:
    """Copy WARNING and above to data_dir/logs/app.log.

    Every deploy recreates the container, and with it the only other log
    store (Docker's json-file log), so what failed before a deploy used to be
    gone after it. The data volume outlives the container. A handler left
    behind by an earlier lifespan in this process (tests run many) is
    replaced, never stacked."""
    root = logging.getLogger()
    for stale in [h for h in root.handlers if h.get_name() == LOG_FILE_HANDLER]:
        detach_log_file(stale)
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        log_dir / "app.log", maxBytes=5 * MB, backupCount=5, encoding="utf-8"
    )
    handler.set_name(LOG_FILE_HANDLER)
    handler.setLevel(logging.WARNING)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root.addHandler(handler)
    return handler


def detach_log_file(handler: logging.Handler) -> None:
    logging.getLogger().removeHandler(handler)
    handler.close()


def check_auth_settings(settings: Settings) -> None:
    """Refuse to boot a server that must be authenticated but is not.

    With no password every /api route is open, and SessionAuth says so to no
    one; a lost or half-rewritten .env used to bring the site up that way.
    The Docker image sets OMNIA_REQUIRE_AUTH, so there no-auth takes an
    explicit OMNIA_REQUIRE_AUTH=0; a local dev server keeps it off."""
    if not settings.require_auth:
        return
    if not settings.auth_password:
        raise RuntimeError(
            "OMNIA_AUTH_PASSWORD is empty but OMNIA_REQUIRE_AUTH is set: refusing "
            "to start with every /api route open. Set a password, or set "
            "OMNIA_REQUIRE_AUTH=0 to run without authentication."
        )
    if settings.session_secret in ("", DEFAULT_SESSION_SECRET):
        raise RuntimeError(
            "OMNIA_SESSION_SECRET is unset or the default while OMNIA_REQUIRE_AUTH "
            "is set: generate one with `openssl rand -hex 32`."
        )


async def _push_reminders(state, alerts: list[dict]) -> None:
    try:
        await push_service.deliver(state, alerts)
    except Exception:
        log.warning("reminder push failed", exc_info=True)


async def reminder_tick(state, inflight: set[asyncio.Task]) -> None:
    """Queue every reminder now due, then push them without waiting.

    alerts.reminder_tick queues each due reminder before marking it delivered
    (events via mark_surfaced, tasks via the reminder ledger) and returns what
    it newly queued, so pushing that never repeats across ticks. Delivery runs
    as its own task (held in `inflight`) so a slow push service never delays
    the queue."""
    due = await queue_due_reminders(state, push=False)
    if due:
        task = asyncio.create_task(_push_reminders(state, due))
        inflight.add(task)
        task.add_done_callback(inflight.discard)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    check_auth_settings(settings)  # before anything is opened or started
    log_file = attach_log_file(settings.data_dir)
    if not settings.auth_password:
        log.warning("authentication is DISABLED: every /api route is open")
    app.state.settings = settings
    app.state.live = LiveBus()
    # Ends /api/live streams so the server can shut down (see live.py).
    app.state.shutting_down = asyncio.Event()
    set_on_stop_signal(app.state.shutting_down)
    app.state.auth = auth_api.SessionAuth(
        settings.auth_username,
        settings.auth_password,
        settings.session_secret,
        settings.data_dir,
    )

    vault = VaultRepo(settings.vault_dir, settings.git_author_name, settings.git_author_email)
    vault.ensure()
    app.state.vault = vault
    app.state.store = NoteStore(settings.vault_dir)
    app.state.trash = TrashStore(settings.vault_dir, app.state.store)
    app.state.paper = PaperStore(settings.vault_dir, app.state.trash)
    app.state.task_tombstones = TaskTombstones(settings.vault_dir)
    app.state.activities = ActivityStore(settings.vault_dir)
    app.state.events = EventStore(settings.vault_dir)
    app.state.thread_index = ThreadIndex(settings.index_dir / "metadata.db")
    app.state.threads = ThreadStore(
        settings.vault_dir, settings.data_dir, index=app.state.thread_index
    )
    app.state.flashcards = FlashcardStore(settings.vault_dir)
    app.state.proofread = ProofreadStore(settings.data_dir)
    interrupted_threads = app.state.threads.reconcile_incomplete()
    if interrupted_threads:
        log.info(
            "marked %d interrupted OMNIA response(s) after restart",
            interrupted_threads,
        )
    # Cheap: this is a few hundred short messages at most, and on an already
    # populated table it is one DISTINCT scan. It heals a database that predates
    # the table, and purges rows for threads housekeeping deleted overnight.
    indexed_messages = app.state.thread_index.sync(
        app.state.threads.list(include_messages=True)
    )
    if indexed_messages:
        log.info("indexed %d OMNIA conversation message(s) for search", indexed_messages)
    internal_config = read_json(
        settings.vault_dir / NoteStore.INTERNAL_DIR / "config.json", {}
    )
    app.state.timezone = (
        internal_config.get("timezone")
        if isinstance(internal_config, dict) and internal_config.get("timezone")
        else settings.timezone
    )
    app.state.omnia_housekeeping = (
        bool(internal_config.get("omnia_housekeeping", HOUSEKEEPING_DEFAULT))
        if isinstance(internal_config, dict)
        else HOUSEKEEPING_DEFAULT
    )
    # Full auto: OMNIA applies mutations without asking first. Everything it
    # does is still recorded as an undoable action and committed to the vault.
    app.state.omnia_full_auto = (
        bool(internal_config.get("omnia_full_auto", False))
        if isinstance(internal_config, dict)
        else False
    )
    # Autocorrect scans cost a model call per pause, so they are opt-in.
    app.state.omnia_autocorrect = (
        bool(internal_config.get("omnia_autocorrect", False))
        if isinstance(internal_config, dict)
        else False
    )
    try:
        expired_trash = app.state.trash.purge_expired()
    except Exception:  # the Trash must never keep the app from booting
        log.exception("trash purge failed")
        expired_trash = []
    if expired_trash:
        log.info("purged %d expired Trash item(s)", len(expired_trash))
    # Paper deletes stay in the change feed twice as long as the Trash keeps
    # the payload, so an iPad that was away for a month still learns about them.
    pruned_tombstones = app.state.paper.prune_tombstones()
    if pruned_tombstones:
        log.info("pruned %d expired Paper tombstone(s)", pruned_tombstones)

    boot_commit_failed = False

    async def boot_commit(message: str) -> None:
        """Commit what the last run left in the working tree, if anything.

        A git failure here used to propagate out of the lifespan, and
        restart: unless-stopped crash-looped the app behind Caddy. The
        edits are safe on disk either way; the autocommitter retries."""
        nonlocal boot_commit_failed
        try:
            if vault.has_changes():
                await vault.commit_all(message)
        except Exception:
            boot_commit_failed = True
            log.exception("boot commit failed; the autocommitter will retry")

    # Make the one-time task rewrite its own recoverable commit. The marker is
    # Git-backed, so restarts and redeployments cannot repeat it.
    task_migration = read_json(
        settings.vault_dir
        / NoteStore.INTERNAL_DIR
        / "migrations"
        / "tasks-v2.json",
        {},
    )
    migration_needed = not (
        isinstance(task_migration, dict)
        and task_migration.get("version") == MIGRATION_VERSION
    )
    if migration_needed:
        await boot_commit("Autosave before EXSCALIA data migration")
    migrated = migrate_task_metadata(app.state.store, settings.vault_dir)
    if migration_needed:
        await boot_commit("Migrate task lifecycle metadata")
        if migrated:
            log.info("migrated task metadata in %d note(s)", len(migrated))
    else:
        await boot_commit("Recover working tree after restart")

    app.state.db = await open_db(settings.index_dir)
    # Rows derived by another INDEX_VERSION (an upgrade, or a rollback in
    # either direction) are re-derived below even where the mtime matches.
    stored_version = await index_version(app.state.db)
    reindex = stored_version != INDEX_VERSION
    if reindex:
        log.info(
            "index version %d -> %d: re-deriving every note, Paper page and conversation",
            stored_version,
            INDEX_VERSION,
        )
    app.state.indexer = Indexer(
        app.state.db, app.state.store, app.state.task_tombstones
    )
    # The habits table is part of the rebuildable index, but nothing could
    # rebuild it — what the user answered lives in no note. Every write mirrors
    # it into the vault, so an index lost or reset comes back from git here.
    restored_habits = await habits.restore_if_empty(app.state)
    if restored_habits:
        log.info("restored %d learned habit(s) from the vault mirror", restored_habits)

    app.state.semantic = SemanticIndex(
        settings.index_dir,
        settings.embedding_model,
        enabled=settings.embedding_backend != "none",
    )
    # The _soon variants embed in the background (in write order): a request
    # must never wait on the embedding model — its lazy first load stalled the
    # first note write after every restart by several seconds.
    app.state.indexer.on_note_changed = app.state.semantic.index_note_soon
    app.state.indexer.on_note_removed = app.state.semantic.remove_note_soon
    # Paper's index is built here rather than beside the Indexer above because
    # it hands transcripts to the semantic index, which only exists now.
    app.state.paper_index = PaperIndex(
        app.state.db,
        app.state.paper,
        app.state.semantic,
        write_lock=app.state.indexer.write_lock,
    )

    await app.state.indexer.sync_all(force=reindex)
    # Logs its own count.
    await app.state.paper_index.sync_all(force=reindex)
    if reindex:
        try:
            app.state.thread_index.rebuild(
                app.state.threads.list(include_messages=True)
            )
        except Exception:  # conversation search must never keep the app from booting
            log.exception("conversation index rebuild failed")
    # Every boot: an older image run in between stamps its own version, so
    # rolling forward again re-derives what that image built.
    await stamp_index_version(app.state.db)
    await boot_commit("Reconcile internal task history")

    app.state.pending = PendingStore(
        app.state.db,
        app.state.store,
        vault,
        app.state.indexer,
        app.state.trash,
        write_lock=app.state.indexer.write_lock,
    )

    app.state.usage = UsageLedger(settings.vault_dir)
    app.state.ai_config = load_ai_config(settings.data_dir)
    provider_id, key, model = effective_ai(settings, app.state.ai_config)
    provider = build_provider(
        settings,
        provider_id=provider_id,
        api_key=key,
        model=model,
        reasoning=reasoning_level(app.state.ai_config),
    )
    app.state.overseer = Overseer(provider, app.state) if provider else None
    if provider:
        log.info("OMNIA enabled: %s / %s", provider.name, provider.model)
    else:
        log.info("OMNIA disabled (no API key configured)")

    app.state.autocommit = AutoCommitter(vault, settings.autocommit_debounce)
    app.state.autocommit.start()
    if boot_commit_failed:
        app.state.autocommit.touch()
    app.state.actions = ActionCoordinator(app.state)
    # Approving from the review tray records an undoable action too, so the
    # safe path is no longer the one without recovery.
    app.state.pending.actions = app.state.actions
    purged_actions = app.state.actions.purge_old()
    if purged_actions:
        log.info("purged %d old OMNIA action record(s)", purged_actions)

    purged_pending = await app.state.pending.purge_old()
    if purged_pending:
        log.info("purged %d pending change(s) older than %d days",
                 purged_pending, app.state.pending.PURGE_AFTER_DAYS)

    async def maintenance():
        while True:
            await asyncio.sleep(6 * 60 * 60)
            await maintenance_once(app.state)

    trash_cleaner = asyncio.create_task(maintenance())

    # Reminder delivery. Nothing ran between morning greetings, so a reminder
    # set for 15 minutes before a 4pm lecture was emitted at 8am and never at
    # 3:45pm. This wakes every minute and queues reminders as they come due.
    app.state.alerts = AlertQueue(settings.data_dir)
    # Web Push keypair — generated on first boot and persisted to the data dir.
    app.state.vapid = push_service.load_or_create_vapid(settings)

    reminder_pushes: set[asyncio.Task] = set()

    async def reminder_ticker():
        while True:
            await asyncio.sleep(TICK_SECONDS)
            try:
                await reminder_tick(app.state, reminder_pushes)
            except Exception:  # a bad event must not kill the loop
                log.warning("reminder tick failed", exc_info=True)

    reminder_task = asyncio.create_task(reminder_ticker())

    # OMNIA scheduled runs. Dormant until the user creates a schedule; each due
    # one fires a proactive (suggestion-tier only) agent turn, budget-gated.
    async def scheduler_ticker():
        while True:
            await asyncio.sleep(SCHEDULE_TICK)
            try:
                await run_due_schedules(app.state)
            except Exception:  # a bad schedule must not kill the loop
                log.warning("scheduler tick failed", exc_info=True)

    scheduler_task = asyncio.create_task(scheduler_ticker())

    # Nightly tidy of the conversation list. Polls rather than sleeping to
    # midnight, so it re-reads the timezone each tick and catches up on a night
    # the container was down.
    housekeeping_task = asyncio.create_task(housekeeping_ticker(app.state))

    # OMNIA's persistent memory about the user lives in the vault itself.
    if not app.state.store.exists(MEMORY_PATH):
        content = app.state.store.create(MEMORY_PATH, MEMORY_TEMPLATE)
        await app.state.indexer.index_note(MEMORY_PATH, content)
        app.state.autocommit.touch()
        log.info("created OMNIA memory note at %s", MEMORY_PATH)

    async def semantic_backfill():
        # Stale notes and Paper pages get embedded, vectors of paths that are
        # gone get dropped; an up-to-date index loads neither the model nor a
        # vector (see SemanticIndex.backfill).
        try:
            pages = await app.state.paper_index.semantic_documents()
        except Exception:  # then nothing is removed, and notes still embed
            log.exception("semantic backfill could not list Paper pages")
            pages = None
        try:
            await app.state.semantic.backfill(app.state.store, pages)
        except Exception:
            log.exception("semantic backfill failed")

    backfill = asyncio.create_task(semantic_backfill())

    # Off-site backup: push the vault to a git remote on an interval.
    app.state.backup = None
    if settings.backup_remote:
        try:
            pusher = BackupPusher(
                vault,
                settings.backup_remote,
                settings.backup_interval,
                state_dir=settings.data_dir,
            )
            pusher.configure_remote_sync()

            # Weeks of silent exit-128 pushes went unnoticed: after repeated
            # failures, say so once a day, in-app and over Web Push.
            async def backup_failing(alert):
                app.state.alerts.add([alert])
                await push_service.deliver(app.state, [alert])

            pusher.on_persistent_failure = backup_failing
            pusher.start()
            app.state.backup = pusher
            log.info("backup remote configured (every %ds)", int(pusher.interval))
        except Exception:
            log.exception("failed to configure backup remote")

    log.info("vault ready at %s", settings.vault_dir)
    yield

    # Usually already set by the stop signal; this covers a stop without one.
    app.state.shutting_down.set()
    backfill.cancel()
    trash_cleaner.cancel()
    reminder_task.cancel()
    for push in list(reminder_pushes):
        push.cancel()
    scheduler_task.cancel()
    housekeeping_task.cancel()
    # Media jobs write notes, attachments and the index; stop them before the
    # final commit and before the database closes under them.
    await media_theme.shutdown()
    # The slow steps below are bounded so shutdown fits compose's
    # stop_grace_period; past it Docker SIGKILLs the process mid-commit.
    # Losing a queued embedding or removal here would be healed by the next
    # boot's backfill anyway, but a short drain avoids the churn when possible.
    try:
        await asyncio.wait_for(app.state.semantic.drain(), timeout=10)
    except Exception:
        log.warning("semantic drain did not finish cleanly", exc_info=True)
    await app.state.autocommit.stop()  # flush a final autosave commit…
    if app.state.backup:
        await app.state.backup.stop()
        await app.state.backup.final_push()  # …then push it off-site, bounded
    await app.state.db.close()
    detach_log_file(log_file)  # last, so shutdown's own warnings reach the file


# No /docs, /redoc or /openapi.json: require_auth only gates /api/, so they
# handed anyone the full map of the private API. app.openapi() still builds
# the schema offline.
app = FastAPI(
    title="EXSCALIA",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# Registered before require_auth so it sits inside it: a caller without a
# session still gets 401, and an oversized Paper upload gets 413 before
# Starlette spools its multipart body to disk.
app.middleware("http")(reject_oversized_bodies)


@app.middleware("http")
async def require_auth(request: Request, call_next):
    """Protect all /api routes (except auth itself) when a password is set."""
    path = request.url.path
    if (
        request.method != "OPTIONS"
        and path.startswith("/api/")
        and not path.startswith("/api/auth/")
        # The docker healthcheck probes from inside the container, cookieless.
        # It answers {"ok": true} and the build's commit — nothing to protect.
        and path != "/api/health"
    ):
        auth = getattr(request.app.state, "auth", None)
        if auth and not auth.authenticated(request):
            return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
    return await call_next(request)


@app.middleware("http")
async def no_store_api(request: Request, call_next):
    """API responses are live vault state — never let browsers cache them."""
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


# Request-body caps per route family (see body_limit.py); the Caddyfile sets
# the same at the edge. The largest legitimate bodies are Paper's: a 50 MB
# asset, and a page save carrying a 32 MB drawing, an 8 MB render and 2 MB of
# text in one multipart. Added before CORS so CORS stays outermost and a 413
# still carries the desktop origin's CORS headers — without them its fetch
# fails as a network error instead of showing the message.
app.add_middleware(
    BodyLimitMiddleware,
    limits=[
        ("/api/auth/", 64 * 1024),
        ("/api/attachments", 30 * MB),
        ("/api/paper/", 64 * MB),
    ],
    default=64 * MB,
)


# The desktop app runs the SPA from a tauri://localhost origin and
# authenticates with a bearer token, so cross-origin API access is safe to
# allow for exactly those origins (no cookies involved). Added last so it is
# outermost and answers preflights before require_auth sees them. Every app
# request carries x-client-id (the live-echo origin); a preflight that refused
# it would fail every desktop request.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["tauri://localhost", "http://tauri.localhost"],
    allow_methods=["*"],
    allow_headers=["authorization", "content-type", "x-client-id"],
)


app.include_router(auth_api.router)
app.include_router(activities_api.router)
app.include_router(attachments_api.router)
app.include_router(notes_api.router)
app.include_router(media_api.router)
app.include_router(paper_api.router)
app.include_router(meta_api.router)
app.include_router(overseer_api.router)
app.include_router(live_api.router)
app.include_router(proofread_api.router)
app.include_router(autocorrect_api.router)
app.include_router(push_api.router)
app.include_router(calendar_api.router)
app.include_router(events_api.router)
app.include_router(flashcards_api.router)
app.include_router(tasks_api.router)
app.include_router(recovery_api.router)


@app.exception_handler(NoteError)
async def note_error_handler(request: Request, exc: NoteError):
    return JSONResponse(status_code=exc.status, content={"detail": exc.detail})


@app.exception_handler(fm.FrontmatterError)
async def frontmatter_error_handler(request: Request, exc: fm.FrontmatterError):
    """A rewrite of a note whose frontmatter does not load, refused by
    fm.serialize: the same 422 NoteStore gives its own rewrite paths."""
    return JSONResponse(
        status_code=422, content={"detail": f"Frontmatter is not valid YAML: {exc}"}
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception):
    """Give an unexpected 500 a reference the user can quote, logged beside
    its traceback (and so in data_dir/logs/app.log)."""
    ref = uuid.uuid4().hex[:8]
    log.error(
        "unhandled error ref=%s %s %s",
        ref,
        request.method,
        request.url.path,
        exc_info=exc,
    )
    return JSONResponse(status_code=500, content={"detail": f"Server error (ref {ref})"})
@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    """Keep the `{detail: string}` contract every client reads (web, desktop,
    the iPad's PaperAPI): FastAPI's default 422 sends a list, which the web
    rendered as '[object Object]'. The raw list rides along under `errors`."""
    errors = exc.errors()
    parts = []
    for err in errors:
        # The field's own name: ('body', 'n') -> 'n'. Skip list indexes and
        # JSON-decode positions, which read as noise.
        names = [part for part in err.get("loc") or () if isinstance(part, str)]
        msg = str(err.get("msg", "invalid"))
        parts.append(f"{names[-1]}: {msg}" if names else msg)
    return JSONResponse(
        status_code=422,
        content={
            "detail": "; ".join(parts) or "Invalid request",
            "errors": jsonable_encoder(errors),
        },
    )


@app.get("/api/health")
async def health(request: Request):
    # `build` lets deploy.sh confirm, through Caddy, that the new image is
    # the one answering.
    return {"ok": True, "build": request.app.state.settings.git_sha}


# --- desktop-app update feed -----------------------------------------------
# Unauthenticated by design: the updater runs before login and only ever
# receives signed artifacts (signature verified client-side against the
# public key baked into the app).

settings = get_settings()


@app.get("/updates/{path:path}", include_in_schema=False)
async def updates(path: str):
    root = settings.updates_dir.resolve()
    candidate = (root / path).resolve()
    if path and candidate.is_relative_to(root) and candidate.is_file():
        return FileResponse(candidate, headers={"Cache-Control": "no-cache"})
    return JSONResponse(status_code=404, content={"detail": "Not found"})


# --- static SPA (built SvelteKit frontend) ---------------------------------
# The web app's Content-Security-Policy mirrors the desktop's
# (desktop/src-tauri/tauri.conf.json) minus its Tauri-only connect-src
# entries. The built index.html holds two inline scripts no fixed policy can
# name — app.html's theme bootstrap and SvelteKit's per-build init — so
# script-src pins the sha256 of each inline script in the file being served.
# img-src allows https: for the remote covers the media lookup shows before
# ingestion; style-src needs 'unsafe-inline' for CodeMirror, KaTeX and Svelte
# style attributes. The other shells are unaffected: the iPad's WKUserScript
# (__EXSCALIA_IPAD__) and evaluateJavaScript run outside page CSP, and the
# desktop loads its own bundle from tauri://localhost under its own policy.
_INLINE_SCRIPT = re.compile(
    r"<script(\s[^>]*)?>(.*?)</script>", re.DOTALL | re.IGNORECASE
)
_SRC_ATTRIBUTE = re.compile(r"\ssrc\s*=", re.IGNORECASE)


def content_security_policy(html: str) -> str:
    hashes = [
        "'sha256-"
        + base64.b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode("ascii")
        + "'"
        for attributes, body in _INLINE_SCRIPT.findall(html)
        if not _SRC_ATTRIBUTE.search(attributes)
    ]
    return "; ".join(
        [
            "default-src 'self'",
            " ".join(["script-src 'self'", *hashes]),
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' blob: data: https:",
            "font-src 'self' data:",
            "connect-src 'self'",
            "worker-src 'self'",
            "manifest-src 'self'",
            "object-src 'none'",
            "base-uri 'self'",
            "form-action 'self'",
            "frame-ancestors 'none'",
        ]
    )


@lru_cache(maxsize=8)
def _page_policy(path: str, mtime_ns: int, size: int) -> str:
    # Keyed on the file's stat, so a rebuilt frontend (every build has a new
    # init script) is picked up without a server restart.
    return content_security_policy(Path(path).read_text(encoding="utf-8"))


def _html_page(page: Path) -> FileResponse:
    stat = page.stat()
    policy = _page_policy(str(page), stat.st_mtime_ns, stat.st_size)
    return FileResponse(
        page, headers={"Cache-Control": "no-cache", "Content-Security-Policy": policy}
    )


async def spa(path: str):
    if path.startswith("api/"):
        return JSONResponse(status_code=404, content={"detail": "Not found"})
    candidate = (settings.frontend_dir / path).resolve()
    if (
        path
        and candidate.is_relative_to(settings.frontend_dir.resolve())
        and candidate.is_file()
    ):
        if candidate.suffix.lower() == ".html":
            return _html_page(candidate)
        # SvelteKit content-hashes everything under _app/immutable/, so
        # those may cache forever; anything else (index.html above all)
        # must revalidate, or browsers heuristically serve stale builds.
        cache = (
            "public, max-age=31536000, immutable"
            if path.startswith("_app/immutable/")
            else "no-cache"
        )
        return FileResponse(candidate, headers={"Cache-Control": cache})
    return _html_page(settings.frontend_dir / "index.html")


if settings.frontend_dir.is_dir():
    app.get("/{path:path}", include_in_schema=False)(spa)
