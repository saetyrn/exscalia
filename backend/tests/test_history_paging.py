"""Version history pages past the newest window: history_sync(skip=) and
/api/history?skip= reach every version once, name pre-rename versions by
their old path, and say whether another page may follow."""
import asyncio

from app.api import recovery as recovery_api
from tests.test_task_boards_api import make_client


def _commit(state, message: str) -> None:
    asyncio.run(state.vault.commit_all(message))


def _versions_with_noise(state) -> None:
    # Another note's commits interleave with this one's: git's own --skip
    # counts those too under --follow, which is what paging must not use.
    state.store.create("Log.md", "---\ntitle: Log\n---\n\nfirst body")
    _commit(state, "autosave")
    for n in (2, 3):
        state.store.create(f"Other {n}.md", f"unrelated {n}")
        _commit(state, "autosave")
        state.store.write("Log.md", "---\ntitle: Log\n---\n\n" + f"body {n}\n" * n)
        _commit(state, "autosave")


def test_history_pages_cover_every_version_once(state):
    _versions_with_noise(state)
    everything = state.vault.history_sync("Log.md")
    assert len(everything) == 3

    newest = state.vault.history_sync("Log.md", limit=2)
    older = state.vault.history_sync("Log.md", limit=2, skip=2)

    assert [v["sha"] for v in newest] == [v["sha"] for v in everything[:2]]
    assert [v["sha"] for v in older] == [everything[2]["sha"]]
    assert "first body" in state.vault.commit_content_sync(older[0]["sha"], older[0]["version_path"])
    assert newest + older == everything
    assert state.vault.history_sync("Log.md", limit=2, skip=3) == []


def test_history_page_after_a_rename_uses_the_old_path(state):
    body = "\n".join(f"line {n} of a note that keeps its text" for n in range(12))
    state.store.create("Before.md", f"---\ntitle: Before\n---\n\none\n{body}")
    _commit(state, "autosave")
    state.store.write("Before.md", f"---\ntitle: Before\n---\n\ntwo\n{body}")
    _commit(state, "autosave")
    state.store.rename("Before.md", "After.md")
    _commit(state, "Rename note")
    state.store.write("After.md", f"---\ntitle: After\n---\n\nthree\n{body}")
    _commit(state, "autosave")

    everything = state.vault.history_sync("After.md")
    assert [v["version_path"] for v in everything] == [
        "After.md",
        "After.md",
        "Before.md",
        "Before.md",
    ]
    pages = [state.vault.history_sync("After.md", 1, skip) for skip in range(5)]
    assert pages[4] == []
    assert [page[0] for page in pages[:4]] == everything
    oldest = pages[3][0]
    assert oldest["version_path"] == "Before.md"
    assert "one" in state.vault.commit_content_sync(oldest["sha"], "Before.md")


def test_history_api_pages_with_has_more(state):
    _versions_with_noise(state)
    client = make_client(state, recovery_api.router)

    first = client.get("/api/history", params={"path": "Log.md", "limit": 2})
    assert first.status_code == 200
    assert len(first.json()["versions"]) == 2
    assert first.json()["has_more"] is True

    rest = client.get("/api/history", params={"path": "Log.md", "limit": 2, "skip": 2})
    assert rest.status_code == 200
    assert rest.json()["has_more"] is False
    shas = {v["sha"] for v in first.json()["versions"]}
    assert [v["sha"] for v in rest.json()["versions"]] == [
        v["sha"] for v in state.vault.history_sync("Log.md")[2:]
    ]
    assert not shas & {v["sha"] for v in rest.json()["versions"]}

    assert client.get("/api/history", params={"path": "Log.md", "skip": -1}).status_code == 422
