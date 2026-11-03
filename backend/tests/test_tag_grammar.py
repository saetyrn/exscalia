"""The inline #tag grammar, shared with the frontend.

The note page builds its tag chips with extractNoteTags (frontend/src/lib/
markdown.ts) and links each one to the tag page this index serves; when the
two grammars disagree the chip opens an empty page ('#caf' for #café). Both
sides assert the same fixture — frontend/src/lib/markdown.test.ts runs it too.
"""
import json
from pathlib import Path

import pytest

from app.index.parse import extract_inline_tags, extract_tags

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "frontend" / "src" / "lib" / "testing" / "tag-grammar.json"
)
CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", CASES, ids=[c["body"] for c in CASES])
def test_inline_tags_match_the_shared_fixture(case):
    assert extract_inline_tags(case["body"]) == case["tags"]


def test_frontmatter_tags_stay_verbatim():
    # The tag panel writes 'résumé' and '日本語' into frontmatter; neither may
    # be filtered by the inline grammar on the way into the index.
    assert extract_tags({"tags": ["résumé", "日本語"]}, "#café") == [
        "résumé", "日本語", "café",
    ]
