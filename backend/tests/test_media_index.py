"""Media items and their log entries are indexed into rebuildable tables."""
import asyncio

from app import media_items


def media_note(title="Dune", media="book", extra_meta="", body=""):
    return (
        f"---\ntitle: {title}\nmedia: {media}\nmedia_status: active\n"
        f"creator: Frank Herbert\nyear: 1965\nrating: 9\ntotal: 412\n{extra_meta}---\n\n"
        f"{body}"
    )


LOG_BODY = "Great so far.\n\n## Log\n- 2026-08-01 — started\n- 2026-08-03 — page 120\n"


def test_media_note_populates_both_tables(state):
    async def run():
        await state.indexer.index_note("media/Dune.md", media_note(body=LOG_BODY))
        await state.indexer.index_note(
            "plain.md", "---\ntitle: plain\n---\n\nnothing here\n"
        )
        items = await state.db.execute_fetchall("SELECT * FROM media_items")
        log = await state.db.execute_fetchall("SELECT * FROM media_log ORDER BY at")
        return [dict(r) for r in items], [dict(r) for r in log]

    items, log = asyncio.run(run())
    assert len(items) == 1
    item = items[0]
    assert item["path"] == "media/Dune.md"
    assert item["title"] == "Dune"
    assert item["media_type"] == "book"
    assert item["status"] == "active"
    assert item["creator"] == "Frank Herbert"
    assert item["year"] == 1965
    assert item["rating"] == 9
    assert item["total"] == 412
    assert [(e["kind"], e["value"]) for e in log] == [
        ("started", None),
        ("progress", "page 120"),
    ]
    # Line numbers include the frontmatter offset, like tasks.
    assert all(e["line"] > 0 for e in log)
    assert log[0]["line"] < log[1]["line"]


def test_garbage_fields_degrade_instead_of_crashing(state):
    content = (
        "---\ntitle: Odd\nmedia: game\nmedia_status: speedrunning\n"
        "year: someday\nrating: eleven\ntotal: -3\n---\n\n"
    )

    async def run():
        await state.indexer.index_note("media/Odd.md", content)
        rows = await state.db.execute_fetchall("SELECT * FROM media_items")
        return [dict(r) for r in rows]

    (item,) = asyncio.run(run())
    assert item["status"] == "planned"
    assert item["year"] is None
    assert item["rating"] is None
    assert item["total"] is None


def test_reindex_replaces_rows_and_removal_clears_them(state):
    async def run():
        await state.indexer.index_note("media/Dune.md", media_note(body=LOG_BODY))
        # Re-save with one more entry: rows must be replaced, not duplicated.
        await state.indexer.index_note(
            "media/Dune.md",
            media_note(body=LOG_BODY + "- 2026-08-05 — page 200\n"),
        )
        counts = (
            len(await state.db.execute_fetchall("SELECT * FROM media_items")),
            len(await state.db.execute_fetchall("SELECT * FROM media_log")),
        )
        # Dropping the marker turns it back into an ordinary note.
        await state.indexer.index_note(
            "media/Dune.md", "---\ntitle: Dune\n---\n\n" + LOG_BODY
        )
        after_unmark = (
            len(await state.db.execute_fetchall("SELECT * FROM media_items")),
            len(await state.db.execute_fetchall("SELECT * FROM media_log")),
        )
        await state.indexer.index_note("media/Dune.md", media_note(body=LOG_BODY))
        await state.indexer.remove_note("media/Dune.md")
        after_remove = (
            len(await state.db.execute_fetchall("SELECT * FROM media_items")),
            len(await state.db.execute_fetchall("SELECT * FROM media_log")),
        )
        return counts, after_unmark, after_remove

    counts, after_unmark, after_remove = asyncio.run(run())
    assert counts == (1, 3)
    assert after_unmark == (0, 0)
    assert after_remove == (0, 0)


def test_full_rebuild_regenerates_media_tables(state):
    content = media_note(body=LOG_BODY)

    async def run():
        state.store.create("media/Dune.md", content)
        await state.indexer.sync_all()
        await state.db.execute("DELETE FROM media_items")
        await state.db.execute("DELETE FROM media_log")
        await state.db.commit()
        await state.indexer.sync_all(force=True)
        items = await state.db.execute_fetchall("SELECT path, title FROM media_items")
        log = await state.db.execute_fetchall("SELECT kind FROM media_log")
        return [dict(r) for r in items], len(log)

    items, log_count = asyncio.run(run())
    assert items == [{"path": "media/Dune.md", "title": "Dune"}]
    assert log_count == 2


def test_exempt_root_preserves_display_titles(state):
    # store.create runs prepare_content: outside exempt roots the title is
    # forced to the filename stem; media/ must keep the display title.
    written = state.store.create(
        "media/Elon Musk A Biography.md",
        media_items.build_item_content(
            title="Elon Musk: A Biography", media_type="book", today="2026-08-12"
        ),
    )
    assert "title: 'Elon Musk: A Biography'" in written or (
        'title: "Elon Musk: A Biography"' in written
    )
