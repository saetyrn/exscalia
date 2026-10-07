"""find_duplicates: a scope the model can pass, templated folders (daily/,
media/, omnia/) left out unless asked for, the note cap counted over that
scope, and the difflib confirmation pass kept off the event loop."""
import asyncio
import difflib
import json
import threading

from app.index.semantic import prefix_list
from app.overseer.tools import execute_tool
from app.vault import fm
from app.vault.repo import DAILY_TEMPLATE

# Captured before any test patches difflib, so the stub's scoring is untouched.
SequenceMatcher = difflib.SequenceMatcher

LECTURE = (
    "Thermodynamics lecture 4. Entropy is a state function; the second law "
    "says the entropy of an isolated system never decreases. Carnot cycles "
    "bound the efficiency of every heat engine between two reservoirs."
)


class OverlapSemantic:
    """The vector index, stood in by text overlap. It applies the folder scope
    as a prefilter the way SemanticIndex.search does, and records each call."""

    available = True

    def __init__(self, store):
        self.store = store
        self.calls: list[tuple[list[str], list[str]]] = []

    async def search(self, q, k=8, path_prefix=None, exclude_prefix=None, include_paper=False):
        included, excluded = prefix_list(path_prefix), prefix_list(exclude_prefix)
        self.calls.append((included, excluded))
        hits = []
        for n in self.store.list_notes():
            if included and not n.path.startswith(tuple(included)):
                continue
            if excluded and n.path.startswith(tuple(excluded)):
                continue
            body = fm.parse(self.store.read(n.path))[1]
            score = SequenceMatcher(None, q, body[:2000]).ratio()
            hits.append({"path": n.path, "heading": "", "text": body, "score": round(score, 4)})
        return sorted(hits, key=lambda h: -h["score"])[:k]


def run(state, inp: dict | None = None) -> dict:
    return json.loads(asyncio.run(execute_tool(state, "s", "find_duplicates", inp or {})))


def pairs(out: dict) -> set[tuple[str, str]]:
    return {(c["a"], c["b"]) for c in out["candidates"]}


def seed(state) -> OverlapSemantic:
    """Three untouched daily notes (identical template bodies), two copies of
    one lecture under Notes/ and two of another under Work/."""
    for day in ("2026-09-01", "2026-09-02", "2026-09-03"):
        state.store.create(
            f"daily/{day}.md",
            DAILY_TEMPLATE.replace("{{date}}", day).replace("{{now}}", f"{day}T08:00:00Z"),
        )
    state.store.create("Notes/A.md", f"# Lecture\n\n{LECTURE}\n")
    state.store.create("Notes/B.md", f"# Lecture copy\n\n{LECTURE} Worked example next week.\n")
    memo = "Quarterly budget review: travel is over plan, hiring is under plan, " * 3
    state.store.create("Work/C.md", f"{memo}\n")
    state.store.create("Work/D.md", f"{memo} Follow up with finance.\n")
    state.semantic = OverlapSemantic(state.store)
    return state.semantic


def test_templated_dailies_are_not_reported_by_default(state):
    semantic = seed(state)
    out = run(state)
    assert pairs(out) == {("Notes/A.md", "Notes/B.md"), ("Work/C.md", "Work/D.md")}
    # The vector search is scoped like the note list, not filtered afterwards.
    assert semantic.calls
    for included, excluded in semantic.calls:
        assert included == []
        assert {"templates/", "daily/", "media/", "omnia/"} <= set(excluded)


def test_path_prefix_into_a_skipped_folder_scans_it(state):
    semantic = seed(state)
    out = run(state, {"path_prefix": "daily/"})
    assert pairs(out) == {
        ("daily/2026-09-01.md", "daily/2026-09-02.md"),
        ("daily/2026-09-01.md", "daily/2026-09-03.md"),
        ("daily/2026-09-02.md", "daily/2026-09-03.md"),
    }
    assert all(included == ["daily/"] for included, _ in semantic.calls)


def test_path_prefix_limits_the_scan_to_one_folder(state):
    semantic = seed(state)
    out = run(state, {"path_prefix": "Notes/"})
    assert pairs(out) == {("Notes/A.md", "Notes/B.md")}
    assert len(semantic.calls) == 2


def test_exclude_prefix_adds_to_the_default_skips(state):
    seed(state)
    out = run(state, {"exclude_prefix": "Notes/"})
    assert pairs(out) == {("Work/C.md", "Work/D.md")}


def test_cap_counts_only_the_scoped_notes(state):
    seed(state)
    for i in range(301):
        state.store.create(f"media/Book {i}.md", f"A book, number {i}.\n\n## Log\n")
    out = run(state)
    assert "error" not in out
    assert ("Notes/A.md", "Notes/B.md") in pairs(out)


def test_over_cap_error_names_the_scope_parameters(state):
    state.semantic = OverlapSemantic(state.store)
    for i in range(301):
        state.store.create(f"Archive/Note {i}.md", f"Archived note {i}.\n")
    out = run(state)
    assert "at most 300" in out["error"]
    assert "path_prefix" in out["error"] and "exclude_prefix" in out["error"]
    assert state.semantic.calls == []
    # Following the error's advice gets a scan through.
    assert "error" not in run(state, {"exclude_prefix": "Archive/"})


def test_empty_result_names_the_scope_and_thresholds(state):
    seed(state)
    out = run(state, {"exclude_prefix": ["Notes/", "Work/"]})
    assert out["candidates"] == []
    assert "“daily/”" in out["note"] and "“Notes/”" in out["note"]
    assert "0.75" in out["note"] and "similarity_threshold" in out["note"]


def test_empty_folder_scope_says_there_was_nothing_to_scan(state):
    seed(state)
    out = run(state, {"path_prefix": "Nowhere/"})
    assert out["candidates"] == []
    assert out["note"].startswith("No notes to scan under “Nowhere/”")


def test_overlap_pass_runs_off_the_event_loop(state, monkeypatch):
    seed(state)
    threads: list[threading.Thread] = []

    class Recording(SequenceMatcher):
        def ratio(self):
            threads.append(threading.current_thread())
            return super().ratio()

    monkeypatch.setattr(difflib, "SequenceMatcher", Recording)
    out = run(state, {"path_prefix": "Notes/"})
    assert pairs(out) == {("Notes/A.md", "Notes/B.md")}
    assert threads and threading.main_thread() not in threads
