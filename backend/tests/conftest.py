"""Offline tests: overseer tool loop + pending-changes lifecycle, using the
scripted FakeProvider (no API key, no network)."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.index.db import open_db
from app.live import LiveBus
from app.index.indexer import Indexer
from app.index.paper_index import PaperIndex
from app.overseer.actions import ActionCoordinator
from app.overseer.agent import Overseer
from app.overseer.provider import FakeProvider, ProviderReply, ToolCall
from app.paper_store import PaperStore
from app.pending.store import PendingStore
from app.vault.internal import TaskTombstones, TrashStore
from app.vault.notes import NoteError, NoteStore
from app.vault.repo import VaultRepo


class FakeSemantic:
    """Stands in for the vector index, and remembers what it was handed.

    Nothing here embeds anything, but the calls themselves are load-bearing:
    Paper's index is the only writer that feeds the semantic store something
    other than a markdown file, so tests assert on `indexed`/`removed`.
    """

    available = False

    def __init__(self):
        self.indexed: list[tuple[str, str]] = []
        self.removed: list[str] = []
        # Read by the status and rebuild routes.
        self.error: dict = {"error": None, "since": None}
        self.last_rebuild_errors: list[str] = []

    async def search(self, q, k=8, path_prefix=None, exclude_prefix=None, include_paper=False):
        return []

    async def index_note(self, rel_path, content):
        self.indexed.append((rel_path, content))

    async def remove_note(self, rel_path):
        self.removed.append(rel_path)

    async def index_note_soon(self, rel_path, content):
        await self.index_note(rel_path, content)

    async def remove_note_soon(self, rel_path):
        await self.remove_note(rel_path)

    async def drain(self):
        return None


@pytest.fixture
def state(tmp_path: Path):
    vault_dir = tmp_path / "vault"
    vault = VaultRepo(vault_dir, "user", "user@test")
    vault.ensure()
    store = NoteStore(vault_dir)
    trash = TrashStore(vault_dir, store)
    task_tombstones = TaskTombstones(vault_dir)

    async def build():
        db = await open_db(tmp_path / "index")
        # Wired as main.lifespan wires them: without the Trash an approved
        # delete took the permanent-delete fallback production never takes.
        indexer = Indexer(db, store, task_tombstones)
        await indexer.sync_all()
        pending = PendingStore(
            db, store, vault, indexer, trash, write_lock=indexer.write_lock
        )
        paper = PaperStore(vault_dir, trash)
        semantic = FakeSemantic()
        return SimpleNamespace(
            db=db,
            store=store,
            vault=vault,
            indexer=indexer,
            pending=pending,
            trash=trash,
            task_tombstones=task_tombstones,
            paper=paper,
            paper_index=PaperIndex(
                db, paper, semantic, write_lock=indexer.write_lock
            ),
            semantic=semantic,
            settings=SimpleNamespace(vault_dir=vault_dir, data_dir=tmp_path),
            autocommit=SimpleNamespace(touch=lambda min_delay=None: None),
            live=LiveBus(),
            # The app's clock. Tests derive "today" from this, never from the
            # machine's local date (CI runs in UTC).
            timezone="America/New_York",
        )

    st = asyncio.run(build())
    yield st
    asyncio.run(st.db.close())


@pytest.fixture
def acting_state(state):
    """State with the action coordinator wired, so direct tools take the real
    git-scoped, undoable path instead of falling back to the review tray."""
    state.actions = ActionCoordinator(state)
    return state


def test_settings(tmp_path: Path, **overrides) -> Settings:
    """Settings for a test that boots the real lifespan (main.app): data under
    tmp_path, no embeddings, no auth, no provider keys, no backup remote."""
    values = dict(
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
    values.update(overrides)
    return Settings(**values)


# A helper, not a test: keep pytest from collecting it wherever it is imported.
test_settings.__test__ = False


def make_client(state, *routers) -> TestClient:
    """A bare app over a fixture state, with main.py's NoteError handler."""
    # Imported lazily: loading app.main builds the whole app and reads settings.
    from app.main import note_error_handler

    app = FastAPI()
    app.add_exception_handler(NoteError, note_error_handler)
    for router in routers:
        app.include_router(router)
    app.state = state
    return TestClient(app)
