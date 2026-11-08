"""The stores that used to grow forever now prune themselves, and attachments
became deletable. None of this may ever lose something still in use."""
import asyncio
from datetime import date, datetime, timedelta, timezone

from app.activity_store import ActivityStore
from app.event_store import EventStore
from app.vault.internal import TaskTombstones, atomic_json, iso_utc


def test_surfaced_reminder_keys_are_pruned_once_stale(state):
    events = EventStore(state.settings.vault_dir)
    created = events.add(
        events.normalize(
            title="Standup",
            start="2026-01-05T09:00",
            end="2026-01-05T09:15",
            all_day=False,
            timezone_name="America/New_York",
            notes=None,
            note_path=None,
            recurrence={"frequency": "daily", "interval": 1},
            reminders=[15],
        )
    )
    # Simulate months of consumed daily reminders plus one recent key.
    old_keys = [
        f"2026-0{m}-{d:02d}T09:00:00|15" for m in (1, 2) for d in range(1, 20)
    ]
    fresh_key = "2026-07-27T09:00:00|15"
    stored = events._events()
    stored[0]["surfaced_reminders"] = old_keys + [fresh_key]
    events._save(stored)

    events.digest(date(2026, 7, 28), mark_surfaced=True, now=datetime(2026, 7, 28, 12, 0))
    surfaced = events._events()[0]["surfaced_reminders"]
    assert fresh_key in surfaced
    assert not any(key in surfaced for key in old_keys)
    del created


def test_activity_log_keeps_only_the_newest_entries(state):
    store = ActivityStore(state.settings.vault_dir)
    store.MAX_ENTRIES = 10
    base = date(2026, 1, 1)
    for i in range(15):
        store.add(base + timedelta(days=i), f"activity {i}", 1.0)
    entries = store._entries()
    assert len(entries) == 10
    # The newest survive, the oldest are gone.
    assert entries[-1]["activity"] == "activity 14"
    assert all(e["activity"] != "activity 0" for e in entries)


def test_tombstones_cap_at_the_newest_deletions(state):
    tombs = TaskTombstones(state.settings.vault_dir)
    tombs.MAX_TOMBSTONES = 5
    for i in range(8):
        tombs.record([{"id": f"task-{i}", "path": "a.md", "text": f"t{i}"}])
    kept = tombs.all()
    assert len(kept) == 5
    assert "task-7" in kept and "task-0" not in kept


def test_pending_tray_purges_after_sixty_days(state):
    async def run():
        stale_at = (
            datetime.now(timezone.utc) - timedelta(days=90)
        ).isoformat()
        await state.pending.stage(
            "sess", "create", "Old.md", new_content="# Old\n", rationale="r"
        )
        await state.db.execute(
            "UPDATE pending_changes SET created_at = ?", (stale_at,)
        )
        await state.db.commit()
        await state.pending.stage(
            "sess", "create", "New.md", new_content="# New\n", rationale="r"
        )
        purged = await state.pending.purge_old()
        assert purged == 1
        remaining = await state.pending.list_changes()
        assert [c.path for c in remaining] == ["New.md"]

    asyncio.run(run())


def test_describe_reports_staleness_without_double_reads(state):
    """describe() must agree with is_stale() — it now derives staleness from
    the base it already fetched instead of fetching it twice."""

    async def run():
        await state.pending.stage(
            "sess", "edit", "Welcome.md", new_content="# Edited\n", rationale="r"
        )
        (change,) = await state.pending.list_changes()
        described = await state.pending.describe(change)
        assert described["stale"] == await state.pending.is_stale(change)

        # A user edit underneath makes it stale by both routes.
        state.store.write("Welcome.md", "# Someone typed here\n")
        described = await state.pending.describe(change)
        assert described["stale"] is True
        assert described["stale"] == await state.pending.is_stale(change)

    asyncio.run(run())


def test_attachment_delete_endpoint_removes_the_file(state):
    from fastapi.testclient import TestClient

    from app.api import attachments as attachments_api
    from test_task_boards_api import make_client

    root = state.settings.vault_dir / "attachments"
    root.mkdir(exist_ok=True)
    (root / "orphan-abc123.png").write_bytes(b"\x89PNG fake")

    client = make_client(state, attachments_api.router)
    assert client.delete("/api/attachments/orphan-abc123.png").status_code == 200
    assert not (root / "orphan-abc123.png").exists()
    assert client.delete("/api/attachments/orphan-abc123.png").status_code == 404


def test_omnia_sees_orphan_flags(state):
    from app.overseer.tool_expansion import list_attachments

    async def run():
        root = state.settings.vault_dir / "attachments"
        root.mkdir(exist_ok=True)
        (root / "used-a1.png").write_bytes(b"x")
        (root / "orphan-b2.png").write_bytes(b"x")
        (root / "cover-c3.jpg").write_bytes(b"x")
        (root / "avatar-d4.png").write_bytes(b"x")
        (root / "old-avatar-e5.png").write_bytes(b"x")
        state.store.write(
            "Welcome.md", "# W\n\n![img](/api/attachments/used-a1.png)\n"
        )
        # Media art is frontmatter, profile art is config — neither is a body link.
        state.store.create(
            "media/Dune.md",
            "---\ntitle: Dune\nmedia_type: book\n"
            "cover: /api/attachments/cover-c3.jpg\n---\n\n## Log\n",
        )
        atomic_json(
            state.settings.vault_dir / ".exscalia" / "config.json",
            {
                "media_profile": {
                    "avatar": "/api/attachments/avatar-d4.png",
                    "avatars": [
                        "/api/attachments/avatar-d4.png",
                        "/api/attachments/old-avatar-e5.png",
                    ],
                }
            },
        )
        result = await list_attachments(state, "t", {})
        flags = {a["path"]: a["referenced"] for a in result["attachments"]}
        assert flags["attachments/used-a1.png"] is True
        assert flags["attachments/cover-c3.jpg"] is True
        assert flags["attachments/avatar-d4.png"] is True
        assert flags["attachments/old-avatar-e5.png"] is True
        assert flags["attachments/orphan-b2.png"] is False
        assert result["orphans"] == 1

    asyncio.run(run())


def test_sync_all_skips_reindex_when_only_mtime_changed(state):
    """A git restore rewrites mtimes over identical bytes; boot must remember
    the new mtimes without paying a full reindex."""
    import os

    async def run():
        # Baseline: everything indexed.
        assert await state.indexer.sync_all() == 0

        # Rewrite mtimes without touching content — a restore's signature.
        for info in state.store.list_notes():
            p = state.store.resolve(info.path)
            os.utime(p, (p.stat().st_atime + 5, p.stat().st_mtime + 5))
        assert await state.indexer.sync_all() == 0  # skipped, not reindexed

        # And the new mtimes stuck: a third pass has nothing to look at.
        assert await state.indexer.sync_all() == 0

        # A real edit still reindexes.
        state.store.write("Welcome.md", "# Actually changed\n")
        p = state.store.resolve("Welcome.md")
        os.utime(p, (p.stat().st_atime + 9, p.stat().st_mtime + 9))
        assert await state.indexer.sync_all() == 1

    asyncio.run(run())
