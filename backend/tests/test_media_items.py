"""Media items: slugs, the log-line grammar, and the shared update logic."""
import pytest

from app import media_items
from app.vault import fm


# -- identity ----------------------------------------------------------------


def test_only_known_media_types_mark_an_item():
    assert media_items.is_media_item({"media": "book"})
    assert media_items.is_media_item({"media": "tv"})
    # Stray values must leave the note an ordinary, visible note.
    assert not media_items.is_media_item({"media": True})
    assert not media_items.is_media_item({"media": "podcast"})
    assert not media_items.is_media_item({"media": ["book"]})
    assert not media_items.is_media_item({})


def test_slug_strips_windows_illegal_characters():
    assert media_items.media_slug("Elon Musk: A Biography") == "Elon Musk A Biography"
    assert media_items.media_slug('What <If?> "2/2"\\|*') == "What If 2 2"
    assert media_items.media_slug("Dots and spaces. . ") == "Dots and spaces"
    assert media_items.media_slug("x" * 200) == "x" * 80
    assert media_items.media_slug("???") == "Untitled"
    assert media_items.media_slug("") == "Untitled"


def test_media_path_steps_past_names_already_taken():
    assert media_items.next_media_path("Dune", set()) == "media/Dune.md"
    assert media_items.next_media_path("Dune", {"media/Dune.md"}) == "media/Dune 2.md"
    assert (
        media_items.next_media_path("Dune", {"media/dune.md", "media/Dune 2.md"})
        == "media/Dune 3.md"
    )


# -- log grammar -------------------------------------------------------------


def test_parse_log_reads_only_the_log_section():
    body = (
        "My review mentions - 2026-01-01 — page 5 inline.\n"
        "\n"
        "## Log\n"
        "- 2026-08-01 — started\n"
        "- 2026-08-03 — page 120\n"
        "\n"
        "## Afterthoughts\n"
        "- 2026-08-04 — page 999\n"
    )
    entries = media_items.parse_log(body)
    assert [(e.at, e.kind, e.value) for e in entries] == [
        ("2026-08-01", "started", None),
        ("2026-08-03", "progress", "page 120"),
    ]


def test_parse_log_accepts_all_three_dash_separators():
    body = "## Log\n- 2026-08-01 — started\n- 2026-08-02 – page 3\n- 2026-08-03 - page 4\n"
    entries = media_items.parse_log(body)
    assert [e.kind for e in entries] == ["started", "progress", "progress"]


def test_parse_log_skips_fences_and_bad_dates():
    body = (
        "## Log\n"
        "```\n"
        "- 2026-08-01 — page 1\n"
        "```\n"
        "- 2026-13-40 — page 2\n"
        "- 2026-08-03 — page 3\n"
    )
    entries = media_items.parse_log(body)
    assert [e.value for e in entries] == ["page 3"]


def test_classification_covers_the_grammar():
    body = (
        "## Log\n"
        "- 2026-08-01 — started\n"
        "- 2026-08-02 — page 120\n"
        "- 2026-08-03 — S2E5\n"
        "- 2026-08-04 — episode 7\n"
        "- 2026-08-05 — watched\n"
        "- 2026-08-06 — finished, rated 8\n"
        "- 2026-08-07 — rated 9\n"
        "- 2026-08-08 — beat the water temple\n"
    )
    entries = media_items.parse_log(body)
    assert [(e.kind, e.value) for e in entries] == [
        ("started", None),
        ("progress", "page 120"),
        ("progress", "S02E05"),
        ("progress", "episode 7"),
        ("watched", None),
        ("finished", "8"),
        ("rated", "9"),
        ("note", None),
    ]


def test_entry_text_canonicalizes_progress():
    assert media_items.entry_text("book", "progress", value=120) == "page 120"
    assert media_items.entry_text("tv", "progress", value="s2e5") == "S02E05"
    assert media_items.entry_text("tv", "progress", value="7") == "episode 7"
    assert media_items.entry_text("game", "progress", value="act 2") == "act 2"
    assert media_items.entry_text("book", "finished", rating=8) == "finished, rated 8"
    assert media_items.entry_text("book", "note", text="loved it") == "loved it"
    with pytest.raises(ValueError):
        media_items.entry_text("book", "progress")
    with pytest.raises(ValueError):
        media_items.entry_text("book", "note")
    with pytest.raises(ValueError):
        media_items.entry_text("book", "nonsense")


def test_append_creates_the_section_when_missing():
    out = media_items.append_log_entry("My review.\n", "- 2026-08-01 — started")
    assert out == "My review.\n\n## Log\n- 2026-08-01 — started\n"
    assert media_items.append_log_entry("", "- 2026-08-01 — started") == (
        "## Log\n- 2026-08-01 — started\n"
    )


def test_append_lands_at_the_end_of_the_section():
    body = "## Log\n- 2026-08-01 — started\n\n## Review\ntext\n"
    out = media_items.append_log_entry(body, "- 2026-08-02 — page 9")
    assert out == (
        "## Log\n- 2026-08-01 — started\n- 2026-08-02 — page 9\n\n## Review\ntext\n"
    )


# -- shared update logic -----------------------------------------------------


def item(status="active", media="book", extra=""):
    return (
        f"---\ntitle: Dune\nmedia: {media}\nmedia_status: {status}\n{extra}---\n\n"
        "Review so far.\n"
    )


def test_apply_update_rejects_bad_input():
    with pytest.raises(ValueError):
        media_items.apply_update("---\ntitle: x\n---\n\nplain\n", rating=8, today="2026-08-12")
    with pytest.raises(ValueError):
        media_items.apply_update(item(), rating=11, today="2026-08-12")
    with pytest.raises(ValueError):
        media_items.apply_update(item(), rating=8.5, today="2026-08-12")
    with pytest.raises(ValueError):
        media_items.apply_update(item(), status="reading", today="2026-08-12")
    with pytest.raises(ValueError):
        media_items.apply_update(item(), entry={"kind": "progress", "value": 3, "date": "nope"}, today="2026-08-12")
    with pytest.raises(ValueError):
        media_items.apply_update(item(), today="2026-08-12")


def test_progress_entry_appends_a_line():
    content, summary = media_items.apply_update(
        item(), entry={"kind": "progress", "value": 120}, today="2026-08-12"
    )
    meta, body = fm.parse(content)
    assert "## Log\n- 2026-08-12 — page 120" in body
    assert meta["media_status"] == "active"
    assert summary == "Logged page 120"


def test_started_entry_stamps_date_and_activates():
    content, _ = media_items.apply_update(
        item(status="planned"), entry={"kind": "started"}, today="2026-08-12"
    )
    meta, body = fm.parse(content)
    assert str(meta["started"]) == "2026-08-12"
    assert meta["media_status"] == "active"
    assert "- 2026-08-12 — started" in body


def test_finishing_stamps_and_logs_once():
    content, summary = media_items.apply_update(
        item(), rating=8, status="finished", today="2026-08-12"
    )
    meta, body = fm.parse(content)
    assert meta["media_status"] == "finished"
    assert str(meta["finished"]) == "2026-08-12"
    assert meta["rating"] == 8
    assert body.count("finished, rated 8") == 1
    assert "Marked finished" in summary and "Rated 8/10" in summary

    # An explicit finished entry plus status=finished must not double-log.
    content, _ = media_items.apply_update(
        item(),
        rating=9,
        status="finished",
        entry={"kind": "finished"},
        today="2026-08-12",
    )
    _, body = fm.parse(content)
    assert body.count("finished, rated 9") == 1


def test_watched_entry_finishes_a_movie():
    content, _ = media_items.apply_update(
        item(media="movie"), entry={"kind": "watched"}, today="2026-08-12"
    )
    meta, body = fm.parse(content)
    assert meta["media_status"] == "finished"
    assert str(meta["finished"]) == "2026-08-12"
    assert "- 2026-08-12 — watched" in body


def test_rating_alone_touches_only_frontmatter():
    content, summary = media_items.apply_update(item(), rating=7, today="2026-08-12")
    meta, body = fm.parse(content)
    assert meta["rating"] == 7
    assert "## Log" not in body
    assert summary == "Rated 7/10"


# -- new-item content --------------------------------------------------------


def test_build_item_content_round_trips():
    content = media_items.build_item_content(
        title="Elon Musk: A Biography",
        media_type="book",
        status="active",
        creator="Walter Isaacson",
        year=2023,
        total=688,
        cover="/api/attachments/elon-1a2b.jpg",
        source="openlibrary:OL123W",
        today="2026-08-12",
    )
    meta, body = fm.parse(content)
    assert meta["title"] == "Elon Musk: A Biography"
    assert meta["media"] == "book"
    assert meta["media_status"] == "active"
    assert meta["creator"] == "Walter Isaacson"
    assert meta["year"] == 2023
    assert meta["total"] == 688
    assert str(meta["started"]) == "2026-08-12"
    assert body == "## Log\n- 2026-08-12 — started\n"


def test_build_item_content_validates_and_degrades():
    with pytest.raises(ValueError):
        media_items.build_item_content(title="x", media_type="podcast", today="2026-08-12")
    with pytest.raises(ValueError):
        media_items.build_item_content(title="x", media_type="book", rating=0, today="2026-08-12")
    content = media_items.build_item_content(
        title="x", media_type="game", year="not a year", today="2026-08-12"
    )
    meta, body = fm.parse(content)
    assert "year" not in meta
    assert meta["media_status"] == "planned"
    assert body == ""


def test_finished_item_content_logs_the_finish():
    content = media_items.build_item_content(
        title="Dune", media_type="movie", status="finished", rating=9, today="2026-08-12"
    )
    meta, body = fm.parse(content)
    assert str(meta["finished"]) == "2026-08-12"
    assert meta["rating"] == 9
    assert "- 2026-08-12 — finished, rated 9" in body
