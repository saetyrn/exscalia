import asyncio
import logging
import mimetypes
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
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
from .api import proofread as proofread_api
from .api import push as push_api
from .api import live as live_api
from .api import overseer as overseer_api
from .api import recovery as recovery_api
from .api import tasks as tasks_api
from .config import get_settings
from .event_store import EventStore
from .index.db import content_hash, open_db
from .live import LiveBus
from .proofread import ProofreadStore
from .index.indexer import Indexer
from .index.semantic import SemanticIndex
from .overseer.agent import MEMORY_PATH, MEMORY_TEMPLATE, Overseer
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
from .pending.store import PendingStore
from .task_service import MIGRATION_VERSION, migrate_task_metadata
from .alerts import TICK_SECONDS, AlertQueue, due_reminders, task_due_reminders
from . import push_service
from .scheduler import SCHEDULE_TICK, run_due_schedules
from .vault.internal import TaskTombstones, TrashStore, read_json
from .vault.notes import NoteError, NoteStore
from .vault.repo import AutoCommitter, BackupPusher, VaultRepo

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

# Python's mimetypes table predates the PWA manifest type; without this the
# manifest serves as octet-stream and browsers refuse to install the app.
mimetypes.add_type("application/manifest+json", ".webmanifest")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.settings = settings
    app.state.live = LiveBus()
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
    app.state.task_tombstones = TaskTombstones(settings.vault_dir)
    app.state.activities = ActivityStore(settings.vault_dir)
    app.state.events = EventStore(settings.vault_dir)
    app.state.threads = ThreadStore(settings.vault_dir, settings.data_dir)
    app.state.flashcards = FlashcardStore(settings.vault_dir)
    app.state.proofread = ProofreadStore(settings.data_dir)
    interrupted_threads = app.state.threads.reconcile_incomplete()
    if interrupted_threads:
        log.info(
            "marked %d interrupted OMNIA response(s) after restart",
            interrupted_threads,
        )
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
    expired_trash = app.state.trash.purge_expired()
    if expired_trash:
        log.info("purged %d expired Trash item(s)", len(expired_trash))

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
    if migration_needed and vault.has_changes():
        await vault.commit_all("Autosave before EXSCALIA data migration")
    migrated = migrate_task_metadata(app.state.store, settings.vault_dir)
    if migration_needed and (migrated or vault.has_changes()):
        await vault.commit_all("Migrate task lifecycle metadata")
        if migrated:
            log.info("migrated task metadata in %d note(s)", len(migrated))
    elif vault.has_changes():
        await vault.commit_all("Recover working tree after restart")

    app.state.db = await open_db(settings.index_dir)
    app.state.indexer = Indexer(
        app.state.db, app.state.store, app.state.task_tombstones
    )

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

    await app.state.indexer.sync_all()
    if vault.has_changes():
        await vault.commit_all("Reconcile internal task history")

    app.state.pending = PendingStore(
        app.state.db,
        app.state.store,
        vault,
        app.state.indexer,
        app.state.trash,
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
        """Slow housekeeping nothing should ever wait on: expired trash, the
        vector index's fragment garbage, and the SQLite WAL (which otherwise
        grows unbounded — it sat at 3 MB against a 128 KB database)."""
        while True:
            await asyncio.sleep(6 * 60 * 60)
            if app.state.trash.purge_expired():
                app.state.autocommit.touch()
            await app.state.semantic.optimize()
            try:
                await app.state.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                log.warning("wal checkpoint failed", exc_info=True)

    trash_cleaner = asyncio.create_task(maintenance())

    # Reminder delivery. Nothing ran between morning greetings, so a reminder
    # set for 15 minutes before a 4pm lecture was emitted at 8am and never at
    # 3:45pm. This wakes every minute and queues reminders as they come due.
    app.state.alerts = AlertQueue(settings.data_dir)
    # Web Push keypair — generated on first boot and persisted to the data dir.
    app.state.vapid = push_service.load_or_create_vapid(settings)

    async def reminder_ticker():
        while True:
            await asyncio.sleep(TICK_SECONDS)
            try:
                # Each due reminder is produced exactly once (events consume via
                # mark_surfaced, tasks via the reminder ledger), so pushing the
                # returned lists never repeats across ticks.
                event_alerts = due_reminders(app.state)
                task_alerts = await task_due_reminders(app.state)
                app.state.alerts.add(event_alerts)
                app.state.alerts.add(task_alerts)
                await push_service.deliver(app.state, event_alerts + task_alerts)
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
        # One column scan against the vector table decides what needs
        # embedding. On an up-to-date index this loads NEITHER the embedding
        # model nor a single vector — the previous version re-read every note
        # and ran a filtered table scan per note, every boot, and pulled the
        # model in unconditionally despite its documented lazy load.
        try:
            stored = await app.state.semantic.stored_hashes()
            if stored is None:
                return
            for info in app.state.store.list_notes():
                if info.path.startswith("templates/"):
                    continue
                content = app.state.store.read(info.path)
                if content_hash(content) != stored.get(info.path):
                    await app.state.semantic.index_note(info.path, content)
        except Exception:
            log.exception("semantic backfill failed")

    backfill = asyncio.create_task(semantic_backfill())

    # Off-site backup: push the vault to a git remote on an interval.
    app.state.backup = None
    if settings.backup_remote:
        try:
            pusher = BackupPusher(vault, settings.backup_remote, settings.backup_interval)
            pusher.configure_remote_sync()
            pusher.start()
            app.state.backup = pusher
            log.info("backup remote configured (every %ds)", int(pusher.interval))
        except Exception:
            log.exception("failed to configure backup remote")

    log.info("vault ready at %s", settings.vault_dir)
    yield

    backfill.cancel()
    trash_cleaner.cancel()
    reminder_task.cancel()
    scheduler_task.cancel()
    housekeeping_task.cancel()
    # Losing a queued embedding here would be healed by the next boot's
    # backfill anyway, but a short drain avoids the churn when possible.
    try:
        await asyncio.wait_for(app.state.semantic.drain(), timeout=30)
    except Exception:
        log.warning("semantic drain did not finish cleanly", exc_info=True)
    await app.state.autocommit.stop()  # flush a final autosave commit…
    if app.state.backup:
        await app.state.backup.stop()
        await app.state.backup.push()  # …then push it off-site
    await app.state.db.close()


app = FastAPI(title="EXSCALIA", lifespan=lifespan)


@app.middleware("http")
async def require_auth(request: Request, call_next):
    """Protect all /api routes (except auth itself) when a password is set."""
    path = request.url.path
    if (
        request.method != "OPTIONS"
        and path.startswith("/api/")
        and not path.startswith("/api/auth/")
        # The docker healthcheck probes from inside the container, cookieless.
        # It answers a constant {"ok": true} — nothing to protect.
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


# The desktop app runs the SPA from a tauri://localhost origin and
# authenticates with a bearer token, so cross-origin API access is safe to
# allow for exactly those origins (no cookies involved). Added last so it is
# outermost and answers preflights before require_auth sees them.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["tauri://localhost", "http://tauri.localhost"],
    allow_methods=["*"],
    allow_headers=["authorization", "content-type"],
)


app.include_router(auth_api.router)
app.include_router(activities_api.router)
app.include_router(attachments_api.router)
app.include_router(notes_api.router)
app.include_router(media_api.router)
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


@app.get("/api/health")
async def health():
    return {"ok": True}


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
if settings.frontend_dir.is_dir():

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str):
        if path.startswith("api/"):
            return JSONResponse(status_code=404, content={"detail": "Not found"})
        candidate = (settings.frontend_dir / path).resolve()
        if (
            path
            and candidate.is_relative_to(settings.frontend_dir.resolve())
            and candidate.is_file()
        ):
            # SvelteKit content-hashes everything under _app/immutable/, so
            # those may cache forever; anything else (index.html above all)
            # must revalidate, or browsers heuristically serve stale builds.
            cache = (
                "public, max-age=31536000, immutable"
                if path.startswith("_app/immutable/")
                else "no-cache"
            )
            return FileResponse(candidate, headers={"Cache-Control": cache})
        return FileResponse(
            settings.frontend_dir / "index.html", headers={"Cache-Control": "no-cache"}
        )
