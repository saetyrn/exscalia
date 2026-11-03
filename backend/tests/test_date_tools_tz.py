"""OMNIA's date tools answer in the user's calendar days. Stamps are UTC, so a
note written at 9:30pm in New York is stamped with tomorrow's date; bucketing
by that date prefix dropped the evening from "what did I write yesterday"."""
import asyncio
from datetime import datetime, timezone

from app.overseer import tools
from app.overseer.tools import get_digest_data, query_by_date, read_structure

TZ = "America/New_York"


def seed(state, path: str, created: str, modified: str | None = None) -> None:
    content = (
        f"---\ntitle: {path.rsplit('/', 1)[-1][:-3]}\n"
        f"created: {created}\nmodified: {modified or created}\n---\n\nbody\n"
    )
    target = state.store.resolve(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    asyncio.run(state.indexer.index_note(path, content))


def dated(state, start: str, end: str, **extra) -> dict:
    inp = {"field": "created", "start": start, "end": end, **extra}
    return asyncio.run(query_by_date(state, "s", inp))


def paths(out: dict) -> set[str]:
    return {r["path"] for r in out["results"]}


def test_query_by_date_uses_the_users_day(state):
    state.timezone = TZ
    seed(state, "Dated/Evening.md", "'2026-09-22T01:30:00Z'")  # 21:30 EDT on the 21st
    seed(state, "Dated/After midnight.md", "'2026-09-22T04:30:00Z'")  # 00:30 EDT on the 22nd
    # Hand-written frontmatter: YAML reads it as a datetime, the index stores
    # "2026-09-22 01:45:00+00:00".
    seed(state, "Dated/Imported.md", "2026-09-22 01:45:00+00:00")

    assert paths(dated(state, "2026-09-21", "2026-09-21")) == {
        "Dated/Evening.md",
        "Dated/Imported.md",
    }
    assert paths(dated(state, "2026-09-22", "2026-09-22")) == {"Dated/After midnight.md"}
    assert paths(dated(state, "2026-09-21", "2026-09-22")) == {
        "Dated/Evening.md",
        "Dated/After midnight.md",
        "Dated/Imported.md",
    }

    # UTC is its own zone, not a fixed New York offset.
    state.timezone = "UTC"
    assert paths(dated(state, "2026-09-22", "2026-09-22")) == {
        "Dated/Evening.md",
        "Dated/After midnight.md",
        "Dated/Imported.md",
    }


def test_query_by_date_rejects_malformed_dates(state):
    state.timezone = TZ
    for start, end in (("2026-9-1", "2026-09-02"), ("2026-09-01", ""), ("", "")):
        out = dated(state, start, end)
        assert out == {"error": "start and end must be YYYY-MM-DD"}
    assert "error" in dated(state, "2026-09-02", "2026-09-01")


def test_query_by_date_pages_past_the_limit(state, monkeypatch):
    state.timezone = TZ
    monkeypatch.setattr(tools, "DATE_QUERY_LIMIT", 3)
    for minute in range(5):
        seed(state, f"Dated/N{minute}.md", f"'2026-09-21T15:0{minute}:00Z'")

    first = dated(state, "2026-09-21", "2026-09-21")
    assert (first["returned"], first["offset"], first["more_available"]) == (3, 0, True)
    # Newest first.
    assert [r["path"] for r in first["results"]] == [
        "Dated/N4.md",
        "Dated/N3.md",
        "Dated/N2.md",
    ]
    rest = dated(state, "2026-09-21", "2026-09-21", offset=3)
    assert (rest["returned"], rest["offset"], rest["more_available"]) == (2, 3, False)
    assert paths(first) | paths(rest) == {f"Dated/N{m}.md" for m in range(5)}
    assert "note" not in rest


def test_read_structure_modified_since_uses_the_users_day(state):
    state.timezone = TZ
    seed(state, "Dated/Evening.md", "'2026-09-20T12:00:00Z'", "'2026-09-22T01:30:00Z'")

    def listed(since: str) -> dict:
        return asyncio.run(
            read_structure(state, "s", {"folder": "Dated", "modified_since": since})
        )

    assert [n["path"] for n in listed("2026-09-21")["notes"]] == ["Dated/Evening.md"]
    assert listed("2026-09-22")["notes"] == []
    assert listed("2026-9-21") == {"error": "modified_since must be YYYY-MM-DD"}


def test_digest_window_opens_at_the_users_midnight(state, monkeypatch):
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            # 12:00 EDT on 2026-09-22.
            return datetime(2026, 9, 22, 16, 0, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(tools, "datetime", Frozen)
    state.timezone = TZ
    # days=1 reaches back to the start of the 21st in New York: 04:00Z.
    seed(state, "Dated/Too early.md", "'2026-09-21T03:30:00Z'")  # 23:30 EDT on the 20th
    seed(state, "Dated/In window.md", "'2026-09-21T05:00:00Z'")  # 01:00 EDT on the 21st
    seed(
        state,
        "Dated/Edited.md",
        "'2026-09-20T12:00:00Z'",
        "'2026-09-21T04:30:00Z'",  # 00:30 EDT on the 21st
    )

    data = asyncio.run(get_digest_data(state, "s", {"days": 1}))
    assert data["today"] == "2026-09-22"
    created = {n["path"] for n in data["notes_created"]}
    assert "Dated/In window.md" in created
    assert "Dated/Too early.md" not in created
    modified = {n["path"] for n in data["notes_modified"]}
    assert "Dated/Edited.md" in modified
    assert "Dated/Too early.md" not in modified
