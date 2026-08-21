"""Offline tests: overseer tool loop + pending-changes lifecycle, using the
scripted FakeProvider (no API key, no network)."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.index.db import open_db
from app.live import LiveBus
from app.index.indexer import Indexer
from app.overseer.actions import ActionCoordinator
from app.overseer.agent import Overseer
from app.overseer.provider import FakeProvider, ProviderReply, ToolCall
from app.pending.store import PendingStore
from app.vault.notes import NoteError, NoteStore
from app.vault.repo import VaultRepo


class FakeSemantic:
    available = False

    async def search(self, q, k=8):
        return []


@pytest.fixture
def state(tmp_path: Path):
    vault_dir = tmp_path / "vault"
    vault = VaultRepo(vault_dir, "user", "user@test")
    vault.ensure()
    store = NoteStore(vault_dir)

    async def build():
        db = await open_db(tmp_path / "index")
        indexer = Indexer(db, store)
        await indexer.sync_all()
        pending = PendingStore(db, store, vault, indexer)
        return SimpleNamespace(
            db=db,
            store=store,
            vault=vault,
            indexer=indexer,
            pending=pending,
            semantic=FakeSemantic(),
            settings=SimpleNamespace(vault_dir=vault_dir, data_dir=tmp_path),
            autocommit=SimpleNamespace(touch=lambda: None),
            live=LiveBus(),
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
