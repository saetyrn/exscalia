"""Wikilink resolution: one resolver for the index, the API and OMNIA, agreeing
with the editor's (frontend/src/lib/wikilinks.ts)."""
import asyncio
import sqlite3

import pytest

from app.api import meta as meta_api
from app.api import notes as notes_api
from app.index.db import content_hash, open_db
from app.index.links import names_note, qualified_target, resolve_link, rewrite_links
from app.overseer.tool_expansion import (
    add_wikilink,
    create_note,
    find_broken_links,
    find_notes,
    get_forward_links,
    link_notes,
    rename_note,
)
from app.overseer.tools import get_backlinks

from test_task_boards_api import make_client


# --- the shared vectors ------------------------------------------------------
# Twin of the table in frontend/src/lib/wikilinks.test.ts — keep them equal.

NOTES = [
    "Welcome.md",
    "Index.md",
    "work/Spec.md",
    "a/Plan.md",
    "b/Plan.md",
    "Journal.md",
    "archive/Journal.md",
    "z/Todo.md",
    "a/b/Todo.md",
    "B/Note.md",
    "a/Note.md",
    "notes/Cafe\u0301.md",
    "R\u00e9sum\u00e9.md",
    "\ufffd/Dup.md",
    "\U00020000/Dup.md",
    "a/Ideas.md",
    "b/ideas.md",
    "Case/Memo.md",
    "case/memo.md",
]

VECTORS = [
    ("work/Spec.md", "work/Spec.md"),  # exact path
    ("work/Spec", "work/Spec.md"),  # exact path without .md
    ("Spec", "work/Spec.md"),  # name
    ("Spec.md", "work/Spec.md"),  # name with .md
    ("WORK/spec", "work/Spec.md"),  # case folded
    ("  Spec  ", "work/Spec.md"),  # padding trimmed
    ("Index.md", "Index.md"),
    ("Plan", "a/Plan.md"),  # same depth: code-point order
    ("Plan.md", "a/Plan.md"),
    ("b/Plan", "b/Plan.md"),  # a path beats the name tie-break
    ("B/plan.md", "b/Plan.md"),
    ("Journal", "Journal.md"),  # root beats a folder
    ("archive/Journal", "archive/Journal.md"),
    ("Todo", "z/Todo.md"),  # shallowest beats alphabetical
    ("Note", "B/Note.md"),  # code points: 'B' < 'a' (localeCompare disagrees)
    ("caf\u00e9", "notes/Cafe\u0301.md"),  # NFC target, NFD file
    ("Re\u0301sume\u0301", "R\u00e9sum\u00e9.md"),  # NFD target, NFC file
    ("Dup", "\ufffd/Dup.md"),  # code points, not UTF-16 units
    ("ideas", "b/ideas.md"),  # the exact spelling beats code-point order
    ("Ideas", "a/Ideas.md"),
    ("IDEAS", "a/Ideas.md"),  # no exact spelling: code points
    ("ideas.md", "b/ideas.md"),
    ("case/memo", "case/memo.md"),  # a path spelled exactly beats code-point order
    ("Case/Memo.md", "Case/Memo.md"),
    ("CASE/MEMO", "Case/Memo.md"),  # no exact spelling: code points, 'C' < 'c'
    ("memo", "case/memo.md"),
    ("Memo", "Case/Memo.md"),
    ("Nope", None),
    ("work/Nope", None),
    ("Spec/extra", None),
    ("", None),
    ("   ", None),
]


@pytest.mark.parametrize("target,expected", VECTORS)
def test_resolver_vectors(target, expected):
    assert resolve_link(target, NOTES) == expected
    # The note list's order never matters.
    assert resolve_link(target, list(reversed(NOTES))) == expected


def test_names_note_covers_every_way_a_link_can_reach_a_note():
    assert names_note("Plan", "b/Plan.md")
    assert names_note("PLAN.md", "b/Plan.md")
    assert names_note("b/plan", "b/Plan.md")
    assert not names_note("a/Plan", "b/Plan.md")
    assert not names_note("Planning", "b/Plan.md")


def test_qualified_target_uses_the_name_only_when_it_is_unique():
    assert qualified_target("work/Spec.md", NOTES) == "Spec"
    assert qualified_target("b/Plan.md", NOTES) == "b/Plan"
    assert qualified_target("a/Plan.md", NOTES) == "a/Plan"
    # A root note is reached by its exact path, which is its bare name.
    assert qualified_target("Journal.md", NOTES) == "Journal"
    assert qualified_target("archive/Journal.md", NOTES) == "archive/Journal"
    # Not yet in the list (just created): still counted.
    assert qualified_target("new/Thing.md", NOTES) == "Thing"
    # "x.md.md" is named "x.md", which a link reads as "x": spell it out.
    assert qualified_target("x.md.md", ["x.md.md"]) == "x.md.md"
    for path in NOTES:
        assert resolve_link(qualified_target(path, NOTES), NOTES) == path


# --- rewrite_links -----------------------------------------------------------


def test_rewrite_links_skips_frontmatter_and_code_and_keeps_link_parts():
    content = (
        '---\ntitle: "Hub"\nsee: "[[Plan]]"\n---\n\n'
        "Prose [[Plan]] and ![[Plan]] and [[ Plan #Goals|the plan]].\n"
        "Inline `[[Plan]]` and ``a ` [[Plan]]`` stay, but not this: [[Plan]]\n"
        "An unclosed ` backtick leaves [[Plan]] live.\n"
        "```\n[[Plan]]\n```\n"
        "~~~md\n[[Plan]]\n~~~\n"
        "[[Other]] [[Plan.md]]\n"
    )

    out, changed = rewrite_links(content, lambda t: "b/Budget" if t == "Plan" else None)

    assert changed == 5
    assert out == (
        '---\ntitle: "Hub"\nsee: "[[Plan]]"\n---\n\n'
        "Prose [[b/Budget]] and ![[b/Budget]] and [[ b/Budget #Goals|the plan]].\n"
        "Inline `[[Plan]]` and ``a ` [[Plan]]`` stay, but not this: [[b/Budget]]\n"
        "An unclosed ` backtick leaves [[b/Budget]] live.\n"
        "```\n[[Plan]]\n```\n"
        "~~~md\n[[Plan]]\n~~~\n"
        "[[Other]] [[Plan.md]]\n"
    )


def test_rewrite_links_without_frontmatter_or_changes():
    assert rewrite_links("[[A]]\n", lambda t: None) == ("[[A]]\n", 0)
    assert rewrite_links("[[A]]", lambda t: "B") == ("[[B]]", 1)


# --- the index -----------------------------------------------------------------


def add_note(state, path: str, content: str) -> None:
    created = state.store.create(path, content)
    asyncio.run(state.indexer.index_note(path, created))


def resolved(state, source: str) -> dict[str, str | None]:
    rows = asyncio.run(
        state.db.execute_fetchall(
            "SELECT target, resolved_path FROM links WHERE source = ?", (source,)
        )
    )
    return {r["target"]: r["resolved_path"] for r in rows}


def test_index_stores_the_note_each_link_opens(state):
    add_note(state, "work/Spec.md", "# Spec\n")
    add_note(state, "Index.md", "# Index\n\n[[work/Spec]] [[Spec.md]] [[Ghost]]\n")

    assert resolved(state, "Index.md") == {
        "work/Spec": "work/Spec.md",
        "Spec.md": "work/Spec.md",
        "Ghost": None,
    }


def test_links_repoint_when_their_note_is_created_or_removed(state):
    add_note(state, "Hub.md", "# Hub\n\n[[Plan]] [[b/Plan]]\n")
    assert resolved(state, "Hub.md") == {"Plan": None, "b/Plan": None}

    add_note(state, "b/Plan.md", "# Plan\n")
    assert resolved(state, "Hub.md") == {"Plan": "b/Plan.md", "b/Plan": "b/Plan.md"}

    # A shallower same-name note takes over the bare name, not the path.
    add_note(state, "Plan.md", "# Plan\n")
    assert resolved(state, "Hub.md") == {"Plan": "Plan.md", "b/Plan": "b/Plan.md"}

    asyncio.run(state.indexer.remove_note("Plan.md"))
    assert resolved(state, "Hub.md") == {"Plan": "b/Plan.md", "b/Plan": "b/Plan.md"}

    asyncio.run(state.indexer.remove_note("b/Plan.md"))
    assert resolved(state, "Hub.md") == {"Plan": None, "b/Plan": None}


def test_rename_repoints_links_to_the_new_path(state):
    add_note(state, "Old.md", "# Old\n")
    add_note(state, "Hub.md", "# Hub\n\n[[Old]] [[New]]\n")

    state.store.rename("Old.md", "New.md")
    asyncio.run(state.indexer.rename_note("Old.md", "New.md"))

    assert resolved(state, "Hub.md") == {"Old": None, "New": "New.md"}


def test_sync_all_fills_the_column_for_rows_indexed_before_it_existed(state):
    add_note(state, "work/Spec.md", "# Spec\n")
    add_note(state, "Index.md", "# Index\n\n[[Spec]]\n")
    # What the migration leaves behind: every row NULL, and no note changed on
    # disk, so the per-note reindex skips all of them.
    asyncio.run(state.db.execute("UPDATE links SET resolved_path = NULL"))
    asyncio.run(state.db.commit())

    assert asyncio.run(state.indexer.sync_all()) == 0

    assert resolved(state, "Index.md") == {"Spec": "work/Spec.md"}


def test_open_db_adds_the_column_to_an_old_links_table(tmp_path):
    index_dir = tmp_path / "index"
    index_dir.mkdir()
    old = sqlite3.connect(index_dir / "metadata.db")
    old.executescript(
        "CREATE TABLE links (source TEXT NOT NULL, target TEXT NOT NULL,"
        " target_lower TEXT NOT NULL);"
        "INSERT INTO links VALUES ('Index.md', 'Spec', 'spec');"
    )
    old.commit()
    old.close()

    async def run():
        db = await open_db(index_dir)
        try:
            cols = {r[1] for r in await db.execute_fetchall("PRAGMA table_info(links)")}
            indexes = {r[1] for r in await db.execute_fetchall("PRAGMA index_list(links)")}
            rows = await db.execute_fetchall("SELECT target, resolved_path FROM links")
            return cols, indexes, [tuple(r) for r in rows]
        finally:
            await db.close()

    cols, indexes, rows = asyncio.run(run())
    assert "resolved_path" in cols
    assert "idx_links_resolved" in indexes
    assert rows == [("Spec", None)]


# --- readers -------------------------------------------------------------------


def test_backlinks_and_graph_follow_folder_qualified_and_md_links(state):
    add_note(state, "work/Spec.md", "# Spec\n")
    add_note(state, "Index.md", "# Index\n\nSee [[work/Spec]].\n")
    add_note(state, "Other.md", "# Other\n\nSee [[Spec.md]].\n")

    client = make_client(state, meta_api.router)
    backlinks = {r["path"] for r in client.get("/api/backlinks/work/Spec.md").json()}
    edges = {(e["source"], e["target"]) for e in client.get("/api/graph").json()["edges"]}

    assert backlinks == {"Index.md", "Other.md"}
    assert {("Index.md", "work/Spec.md"), ("Other.md", "work/Spec.md")} <= edges


def test_same_name_notes_keep_their_own_backlinks(state):
    add_note(state, "a/Plan.md", "# Plan\n")
    add_note(state, "b/Plan.md", "# Plan\n")
    add_note(state, "Hub.md", "# Hub\n\n[[Plan]]\n")
    add_note(state, "Other.md", "# Other\n\n[[b/Plan]]\n")

    client = make_client(state, meta_api.router)

    def api_backlinks(path):
        return {r["path"] for r in client.get(f"/api/backlinks/{path}").json()}

    def tool_backlinks(path):
        result = asyncio.run(get_backlinks(state, "t", {"path": path}))
        return {r["path"] for r in result["backlinks"]}

    assert api_backlinks("a/Plan.md") == tool_backlinks("a/Plan.md") == {"Hub.md"}
    assert api_backlinks("b/Plan.md") == tool_backlinks("b/Plan.md") == {"Other.md"}
    edges = {(e["source"], e["target"]) for e in client.get("/api/graph").json()["edges"]}
    assert ("Hub.md", "a/Plan.md") in edges and ("Hub.md", "b/Plan.md") not in edges


def test_related_notes_flags_the_note_the_link_opens(state):
    add_note(state, "a/Plan.md", "# Plan\n")
    add_note(state, "b/Plan.md", "# Plan\n")
    add_note(state, "Hub.md", "# Hub\n\nSee [[b/Plan]].\n")

    async def search(q, k=8, path_prefix=None, exclude_prefix=None):
        return [
            {"path": "a/Plan.md", "score": 0.9, "text": "Plan\n\nalpha"},
            {"path": "b/Plan.md", "score": 0.8, "text": "Plan\n\nbeta"},
        ]

    state.semantic.available = True
    state.semantic.search = search
    related = make_client(state, meta_api.router).get("/api/related/Hub.md").json()

    linked = {r["path"]: r["linked"] for r in related["related"]}
    assert linked == {"a/Plan.md": False, "b/Plan.md": True}


def test_link_hygiene_tools_resolve_like_the_editor(acting_state):
    add_note(acting_state, "work/Spec.md", "# Spec\n")
    add_note(acting_state, "Index.md", "# Index\n\n[[work/Spec]] [[Spec.md]] [[Ghost]]\n")

    broken = asyncio.run(find_broken_links(acting_state, "t", {}))
    forward = asyncio.run(get_forward_links(acting_state, "t", {"path": "Index.md"}))
    orphans = asyncio.run(find_notes(acting_state, "t", {"filter": "orphan"}))

    # (Welcome.md's own [[Daily Notes]] is broken too, so look at Index.md.)
    assert [b["target"] for b in broken["broken_links"] if b["in_note"] == "Index.md"] == ["Ghost"]
    assert {f["target"]: f["path"] for f in forward["forward_links"]} == {
        "work/Spec": "work/Spec.md",
        "Spec.md": "work/Spec.md",
        "Ghost": None,
    }
    assert "work/Spec.md" not in {n["path"] for n in orphans["notes"]}


# --- writers -------------------------------------------------------------------


def test_add_link_qualifies_a_shared_name(state):
    add_note(state, "a/Plan.md", "# Plan\n")
    add_note(state, "b/Plan.md", "# Plan\n")
    add_note(state, "work/Spec.md", "# Spec\n")
    add_note(state, "Related.md", "# Related\n")

    client = make_client(state, notes_api.router)
    assert client.post("/api/notes/Related.md/add-link", json={"target": "b/Plan.md"}).status_code == 200
    assert client.post("/api/notes/Related.md/add-link", json={"target": "work/Spec.md"}).status_code == 200

    text = state.store.read("Related.md")
    assert "[[b/Plan]]" in text and "[[Spec]]" in text
    assert resolved(state, "Related.md") == {"b/Plan": "b/Plan.md", "Spec": "work/Spec.md"}


def test_omnia_link_writers_qualify_a_shared_name(acting_state):
    add_note(acting_state, "a/Plan.md", "# Plan\n")
    add_note(acting_state, "b/Plan.md", "# Plan\n")
    add_note(acting_state, "Hub.md", "# Hub\n\n[[Plan]]\n")

    rev = content_hash(acting_state.store.read("Hub.md"))
    asyncio.run(
        add_wikilink(acting_state, "t", {"path": "Hub.md", "target": "b/Plan.md", "base_revision": rev})
    )
    # [[Plan]] opens a/Plan.md, so it does not count as a link to b/Plan.md.
    assert "[[b/Plan]]" in acting_state.store.read("Hub.md")

    rev = content_hash(acting_state.store.read("Hub.md"))
    again = asyncio.run(
        add_wikilink(acting_state, "t", {"path": "Hub.md", "target": "b/Plan.md", "base_revision": rev})
    )
    assert again.get("already_present") is True

    asyncio.run(link_notes(acting_state, "t", {"path_a": "Hub.md", "path_b": "a/Plan.md"}))
    assert "[[a/Plan]]" in acting_state.store.read("Hub.md")
    assert "[[Hub]]" in acting_state.store.read("a/Plan.md")


# --- OMNIA rename_note ------------------------------------------------------------


def note(title: str, body: str) -> str:
    return f'---\ntitle: "{title}"\n---\n\n# {title}\n\n{body}\n'


def make_committed(state, path: str, title: str) -> None:
    # Renames trash the source through git, so create it through the tool.
    asyncio.run(create_note(state, "t", {"path": path, "content": note(title, "body")}))


def rename(state, path: str, new_title: str) -> dict:
    rev = content_hash(state.store.read(path))
    return asyncio.run(
        rename_note(state, "t", {"path": path, "new_title": new_title, "base_revision": rev})
    )


HUB = (
    "Prose [[Plan]] opens a/Plan.\n\n"
    "```\n[[Plan]] and [[b/Plan]] in a code sample\n```\n\n"
    "Inline `[[b/Plan]]` too.\n\n"
    "Qualified [[b/Plan]], [[b/Plan#Goals|the plan]] and ![[B/plan.md]]."
)


def test_rename_rewrites_only_links_that_open_the_renamed_note(acting_state):
    make_committed(acting_state, "a/Plan.md", "Plan")
    make_committed(acting_state, "b/Plan.md", "Plan")
    add_note(acting_state, "Hub.md", note("Hub", HUB))

    result = rename(acting_state, "b/Plan.md", "Budget")

    assert result["path"] == "b/Budget.md"
    body = acting_state.store.read("Hub.md").split("# Hub\n\n", 1)[1]
    assert body == (
        "Prose [[Plan]] opens a/Plan.\n\n"
        "```\n[[Plan]] and [[b/Plan]] in a code sample\n```\n\n"
        "Inline `[[b/Plan]]` too.\n\n"
        "Qualified [[b/Budget]], [[b/Budget#Goals|the plan]] and ![[b/Budget.md]].\n"
    )
    assert resolved(acting_state, "Hub.md")["Plan"] == "a/Plan.md"


def test_rename_keeps_a_bare_name_bare_unless_it_became_ambiguous(acting_state):
    make_committed(acting_state, "a/Plan.md", "Plan")
    make_committed(acting_state, "c/Budget.md", "Budget")
    add_note(acting_state, "Hub.md", note("Hub", "[[Plan]] and [[plan.md]]"))

    rename(acting_state, "a/Plan.md", "Draft")
    assert "[[Draft]] and [[Draft.md]]" in acting_state.store.read("Hub.md")

    rename(acting_state, "a/Draft.md", "Budget")
    # Another Budget exists, so a bare [[Budget]] would open c/Budget.md.
    assert "[[a/Budget]] and [[a/Budget.md]]" in acting_state.store.read("Hub.md")
    assert set(resolved(acting_state, "Hub.md").values()) == {"a/Budget.md"}


def test_rename_follows_a_link_to_itself(acting_state):
    asyncio.run(
        create_note(acting_state, "t", {"path": "Loop.md", "content": note("Loop", "Back to [[Loop]].")})
    )

    rename(acting_state, "Loop.md", "Cycle")

    assert "Back to [[Cycle]]." in acting_state.store.read("Cycle.md")
