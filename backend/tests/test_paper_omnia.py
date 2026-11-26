"""OMNIA's Paper tools: the two core entry points (list_notebooks,
search_handwriting) and the three per-page tools behind use_tools("paper").

The index these tools search is B2's; here it is a small fake, so nothing in
this file depends on the real PaperIndex existing.
"""
import asyncio
import json
import re

from app import media_theme
from app.overseer.provider import ProviderReply
from app.overseer.tools import (
    CORE_TOOL_NAMES,
    TOOL_DOMAINS,
    base_tool_names,
    execute_tool,
)


def run_tool(state, name: str, inp: dict) -> dict:
    return json.loads(asyncio.run(execute_tool(state, "s", name, inp)))


# --- fixtures ---------------------------------------------------------------


class FakeIndex:
    """B2's interface, and only it: search + the two counters."""

    def __init__(self, hits=None, pages=0, transcribed=0):
        self.hits = hits or []
        self.pages = pages
        self.transcribed = transcribed
        self.indexed: list[str] = []

    async def search(self, query, limit=10, notebook=None):
        rows = [h for h in self.hits if query.lower() in h["snippet"].lower()]
        if notebook:
            rows = [h for h in rows if h["notebook"] == notebook]
        return rows[:limit]

    async def count_pages(self):
        return self.pages

    async def count_transcribed(self):
        return self.transcribed

    async def index_page(self, path):
        self.indexed.append(path)


class VisionProvider:
    """One scripted vision reply, with a usage row so metering is observable."""

    model = "fake-vision"

    def __init__(self, text="Quarterly review\n- ship the [?] thing"):
        self.text = text
        self.calls: list[list[dict]] = []

    async def chat(self, system, messages, tools, on_text=None):
        self.calls.append(messages)
        return ProviderReply(
            text=self.text, stop_reason="end_turn", usage={"input": 900, "output": 30}
        )


class Ledger:
    def __init__(self, allow=True):
        self.allow = allow
        self.rows: list[tuple[str, dict]] = []

    def allow_background(self, budget):
        return self.allow

    def record(self, caller, usage):
        self.rows.append((caller, usage or {}))


PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def make_notebook(state, title="Ideas", folder=None, favorite=False):
    manifest = state.paper.create_notebook(title=title, folder=folder)
    if favorite:
        manifest = state.paper.update_notebook(manifest["path"], {"favorite": True})
    return manifest["path"]


def make_page(state, notebook, *, render=True, transcript="", ai=None):
    page = state.paper.create_page(notebook)
    page_id = page["id"]
    if render:
        state.paper.save_page(
            notebook, page_id, base_revision=1, render=PNG, for_revision=1
        )
    if transcript:
        state.paper.save_page(
            notebook,
            page_id,
            base_revision=1,
            text={"lines": [], "transcript": transcript},
            for_revision=1,
        )
    if ai:
        state.paper.set_transcript_ai(
            notebook, page_id, ai, "fake-vision", made_for=1
        )
    return f"{notebook}/pages/{page_id}", page_id


# --- budget ------------------------------------------------------------------


def test_the_two_entry_points_are_core_and_the_rest_is_one_domain():
    assert {"list_notebooks", "search_handwriting"} <= CORE_TOOL_NAMES
    assert TOOL_DOMAINS["paper"][1] == {
        "read_page",
        "transcribe_page",
        "note_from_page",
    }
    base = base_tool_names()
    assert {"list_notebooks", "search_handwriting"} <= base
    assert not TOOL_DOMAINS["paper"][1] & base


# --- list_notebooks ----------------------------------------------------------


def test_list_notebooks_returns_the_library(state):
    make_notebook(state, "Ideas")
    make_notebook(state, "Lectures", folder="School")
    out = run_tool(state, "list_notebooks", {})
    assert {nb["title"] for nb in out["notebooks"]} == {"Ideas", "Lectures"}
    assert "note" not in out


def test_empty_library_says_notebooks_come_from_the_ipad(state):
    out = run_tool(state, "list_notebooks", {})
    assert out["notebooks"] == []
    assert "iPad" in out["note"]
    assert "do not retry" in out["note"]


def test_folder_filter_miss_names_where_the_notebooks_actually_are(state):
    make_notebook(state, "Ideas", folder="Work")
    out = run_tool(state, "list_notebooks", {"folder": "School"})
    assert out["notebooks"] == []
    assert "paper/School" in out["note"]
    assert "paper/Work" in out["note"]
    assert "1 exist elsewhere" in out["note"]


def test_favorites_filter_miss_says_the_filter_is_the_problem(state):
    make_notebook(state, "Ideas")
    out = run_tool(state, "list_notebooks", {"favorites_only": True})
    assert out["notebooks"] == []
    assert "favourites only" in out["note"]


def test_favorites_filter_keeps_starred_notebooks(state):
    make_notebook(state, "Ideas")
    make_notebook(state, "Starred", favorite=True)
    out = run_tool(state, "list_notebooks", {"favorites_only": True})
    assert [nb["title"] for nb in out["notebooks"]] == ["Starred"]


# --- search_handwriting ------------------------------------------------------


def _hit(notebook="paper/Ideas.paper", snippet="quarterly review"):
    return {
        "path": f"{notebook}/pages/p1",
        "notebook": notebook,
        "notebook_title": notebook.split("/")[-1].removesuffix(".paper"),
        "page_id": "p1",
        "position": 1,
        "snippet": snippet,
        "highlights": ["quarterly"],
    }


def test_search_handwriting_returns_index_hits(state):
    state.paper_index = FakeIndex([_hit()], pages=1, transcribed=1)
    out = run_tool(state, "search_handwriting", {"query": "quarterly"})
    assert len(out["results"]) == 1
    assert "note" not in out


def test_no_pages_at_all_is_a_fact_about_the_vault(state):
    state.paper_index = FakeIndex([], pages=0, transcribed=0)
    out = run_tool(state, "search_handwriting", {"query": "quarterly"})
    assert out["results"] == []
    assert "not a problem with the query" in out["note"]


def test_pages_without_transcripts_point_at_the_paper_domain(state):
    state.paper_index = FakeIndex([], pages=5, transcribed=0)
    out = run_tool(state, "search_handwriting", {"query": "quarterly"})
    assert out["results"] == []
    assert "5 Paper page(s)" in out["note"]
    assert 'use_tools("paper")' in out["note"]


def test_notebook_filter_that_excludes_the_hits_says_so(state):
    state.paper_index = FakeIndex([_hit("paper/Work.paper")], pages=3, transcribed=3)
    out = run_tool(
        state,
        "search_handwriting",
        {"query": "quarterly", "notebook": "Other.paper"},
    )
    assert out["results"] == []
    assert "notebook filter is what excluded them" in out["note"]
    assert "Work" in out["note"]


def test_a_plain_miss_suggests_semantic_search(state):
    state.paper_index = FakeIndex([_hit()], pages=3, transcribed=3)
    out = run_tool(state, "search_handwriting", {"query": "zzzunfindable"})
    assert out["results"] == []
    assert "semantic_search" in out["note"]


# --- read_page ---------------------------------------------------------------


def test_read_page_carries_the_render_as_an_image_block(state):
    nb = make_notebook(state)
    path, _ = make_page(state, nb, render=True, transcript="quarterly review")
    out = run_tool(state, "read_page", {"path": path})
    assert out["transcript"] == "quarterly review"
    assert len(out["_images"]) == 1
    assert out["_images"][0]["media_type"] == "image/png"
    assert out["_images"][0]["kind"] == "image"
    assert "read the handwriting yourself" in out["note"]
    assert out["page"] == 1 and out["pages"] == 1


def test_read_page_without_a_render_returns_no_images_and_explains(state):
    nb = make_notebook(state)
    path, _ = make_page(state, nb, render=False, transcript="typed on device")
    out = run_tool(state, "read_page", {"path": path})
    assert "_images" not in out
    assert "no render.png yet" in out["note"]
    assert out["transcript"] == "typed on device"


def test_read_page_rejects_a_path_that_is_not_a_page_dir(state):
    nb = make_notebook(state)
    out = run_tool(state, "read_page", {"path": nb})
    assert "page directory" in out["error"]


# --- transcribe_page ---------------------------------------------------------


def _wire_provider(state, monkeypatch, provider):
    monkeypatch.setattr(media_theme, "build_media_provider", lambda _s: provider)


def test_transcribe_page_writes_transcript_ai_and_meters_paper(state, monkeypatch):
    nb = make_notebook(state)
    path, page_id = make_page(state, nb, render=True)
    provider = VisionProvider()
    _wire_provider(state, monkeypatch, provider)
    state.usage = Ledger()
    state.paper_index = FakeIndex()
    published: list[dict] = []
    state.live.publish = lambda event: published.append(event)

    out = run_tool(state, "transcribe_page", {"path": path})

    assert out["transcribed"] is True
    assert out["transcript"].startswith("Quarterly review")
    # Exactly one call, carrying the image block and the instruction.
    assert len(provider.calls) == 1
    content = provider.calls[0][0]["content"]
    assert content[0]["type"] == "image"
    assert "[?]" in content[1]["text"]
    # …metered under its own caller, stored, reindexed and announced.
    assert state.usage.rows == [("paper", {"input": 900, "output": 30})]
    stored = state.paper.read_blob(nb, page_id, "text.json")[0]
    ai = json.loads(stored.read_text())["transcript_ai"]
    assert "Quarterly review" in ai["text"]
    # Filed under the render it read; the device OCR's revision is not its to move.
    page = state.paper.read_page(nb, page_id)
    assert ai["revision"] == page["render_revision"] == 1
    assert page["text_revision"] == 0
    assert state.paper_index.indexed == [path]
    assert published and published[-1]["type"] == "paper"
    assert published[-1]["path"] == path


def test_over_budget_returns_the_note_without_calling_the_provider(
    state, monkeypatch
):
    nb = make_notebook(state)
    path, page_id = make_page(state, nb, render=True)
    provider = VisionProvider()
    _wire_provider(state, monkeypatch, provider)
    state.usage = Ledger(allow=False)

    out = run_tool(state, "transcribe_page", {"path": path})

    assert out["transcribed"] is False
    assert "budget" in out["note"]
    assert provider.calls == []
    assert state.usage.rows == []
    page = state.paper.read_page(nb, page_id)
    assert page["text_revision"] == 0


def test_a_page_without_a_render_is_not_transcribable(state, monkeypatch):
    nb = make_notebook(state)
    path, _ = make_page(state, nb, render=False)
    provider = VisionProvider()
    _wire_provider(state, monkeypatch, provider)
    state.usage = Ledger()

    out = run_tool(state, "transcribe_page", {"path": path})
    assert out["transcribed"] is False
    assert "do not retry" in out["note"]
    assert provider.calls == []


def test_a_render_older_than_the_ink_is_not_sent_to_the_model(state, monkeypatch):
    nb = make_notebook(state)
    path, page_id = make_page(state, nb, render=True)
    # More ink after the render: revision 2, render still for 1.
    state.paper.save_page(nb, page_id, base_revision=1, drawing=b"more ink")
    provider = VisionProvider()
    _wire_provider(state, monkeypatch, provider)
    state.usage = Ledger()

    out = run_tool(state, "transcribe_page", {"path": path})

    assert out["transcribed"] is False
    assert "older than the ink" in out["note"]
    assert provider.calls == []
    assert state.usage.rows == []
    assert not (state.paper.dir / path / "text.json").exists()


class InkDuringTheCall(VisionProvider):
    """The user keeps writing while the model reads the page."""

    def __init__(self, write):
        super().__init__()
        self.write = write

    async def chat(self, system, messages, tools, on_text=None):
        self.write()
        return await super().chat(system, messages, tools, on_text)


def test_a_reading_is_filed_under_the_render_it_was_read_from(state, monkeypatch):
    nb = make_notebook(state)
    path, page_id = make_page(state, nb, render=True)
    provider = InkDuringTheCall(
        lambda: state.paper.save_page(nb, page_id, base_revision=1, drawing=b"more")
    )
    _wire_provider(state, monkeypatch, provider)
    state.usage = Ledger()

    out = run_tool(state, "transcribe_page", {"path": path})

    assert out["transcribed"] is True
    stored = state.paper.read_blob(nb, page_id, "text.json")[0]
    assert json.loads(stored.read_text())["transcript_ai"]["revision"] == 1
    page = state.paper.read_page(nb, page_id)
    assert page["revision"] == 2 and page["text_revision"] == 0


def test_an_empty_model_reply_is_not_stored(state, monkeypatch):
    nb = make_notebook(state)
    path, page_id = make_page(state, nb, render=True)
    provider = VisionProvider(text="   ")
    _wire_provider(state, monkeypatch, provider)
    state.usage = Ledger()

    out = run_tool(state, "transcribe_page", {"path": path})
    assert out["transcribed"] is False
    assert "returned nothing" in out["note"]
    assert state.paper.read_page(nb, page_id)["text_revision"] == 0


# --- note_from_page ----------------------------------------------------------


def test_note_from_page_records_an_action_with_navigate(acting_state):
    state = acting_state
    nb = make_notebook(state, "Ideas")
    path, page_id = make_page(state, nb, render=True, ai="Quarterly review")

    out = run_tool(state, "note_from_page", {"path": path})

    target = out["path"]
    assert target == "Ideas — Page 1.md"
    assert out["_action"]["navigate"] == f"/note/{target}"
    assert out["_action"]["refresh"] == ["notes", "search"]
    content = state.store.read(target)
    assert "Quarterly review" in content
    assert f"/api/paper/blob/{nb}/{page_id}/render.png" in content
    notebook_id = state.paper.read_notebook(nb)["id"]
    assert f"](/paper/id/{notebook_id}/{page_id})" in content
    # Only the durable link: the path one broke on the first rename and no
    # client needs it.
    assert "Legacy Paper path" not in content
    assert f"/paper/Ideas.paper#{page_id}" not in content
    # It is a real recorded action, so Undo can reach it.
    assert out["_action"]["undoable"] is True
    assert out["source"] == path


def test_note_from_page_embeds_a_render_url_markdown_can_parse(acting_state):
    """The iPad's first notebook is "Notebook 1". A raw space in a markdown
    image destination renders as literal text, and the editor's inline-image
    match stops at the first ')' or whitespace."""
    state = acting_state
    nb = make_notebook(state, "Notebook 1", folder="School (old)")
    path, page_id = make_page(state, nb, render=True, ai="Quarterly review")

    out = run_tool(state, "note_from_page", {"path": path})

    content = state.store.read(out["path"])
    image = re.search(r"!\[([^\]]*)\]\(([^)\s]+)\)", content)
    assert image is not None
    assert "Notebook%201.paper" in image.group(2)
    assert "School%20%28old%29" in image.group(2)
    assert image.group(2).startswith("/api/paper/blob/")
    assert f"/{page_id}/render.png" in image.group(2)


def test_note_from_page_takes_a_target_path(acting_state):
    state = acting_state
    nb = make_notebook(state, "Ideas")
    path, _ = make_page(state, nb, render=True, ai="Quarterly review")
    out = run_tool(state, "note_from_page", {"path": path, "target_path": "Review"})
    assert out["_action"]["navigate"] == "/note/Review.md"
    assert state.store.exists("Review.md")


def test_note_from_page_without_a_transcript_explains_itself(acting_state):
    state = acting_state
    nb = make_notebook(state, "Ideas")
    path, _ = make_page(state, nb, render=True)
    out = run_tool(state, "note_from_page", {"path": path})
    assert out["created"] is False
    assert "no transcript" in out["note"]
    assert not state.store.exists("Ideas — Page 1.md")
