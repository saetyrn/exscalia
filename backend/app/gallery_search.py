"""Related pictures and Gallery search: what a picture is about first.

The owner collects by theme: a 1990s game screenshot should lead to the
other 1990s games they saved, not to whatever shares its colours. So the
signals, in the order they count:

    themes  the hidden readings' kind, era, works and themes, shared
            (gallery.weighted_themes). Each shared theme counts by its
            weight (a work 2, an era 1.5, a kind 1.2, a theme 1) and by how
            rare it is in the Gallery: "playstation" says more than
            "illustration", which half the pictures share.
    meaning the readings as text vectors (gallery_vectors), close when two
            pictures are described as the same sort of thing in other words
    tags    the owner's own tags, shared
    look    CLIP image vectors: only a strong likeness counts, and lightly
    words   for a search: every word of the query in the title, board,
            tags, description, source or reading

Ranks are fused by weighted reciprocal-rank fusion: a picture's score is
the sum over signals of weight / (RRF_K + rank). A signal with nothing to
say (no reading yet, the image model off) adds nothing. A picture on the
same board gets a nudge worth about two places.
"""
from __future__ import annotations

import json
import math
import unicodedata

from . import gallery

RRF_K = 60
RELATED_WEIGHTS = {"themes": 1.0, "meaning": 0.9, "tags": 0.8, "look": 0.3}
SEARCH_WEIGHTS = {"words": 1.2, "meaning": 1.0, "look": 0.6}
SAME_BOARD = 0.03 / (RRF_K + 1)
DEPTH = 120
# Shared themes worth less than this share of a picture's own (by weight and
# rarity) are a coincidence ("girl", "illustration"), not a theme in common.
THEME_FLOOR = 0.12
# Reading to reading, cosine with the notes' model (bge-small), measured
# Nov 8: three 1990s game readings 0.715 to 0.77 with each other, every
# other pair 0.64 and under.
MEANING_FLOOR = 0.68
# A query to a reading: matches 0.70 and up, non-matches 0.62 and under (the
# semantic index's 0.585, as a cosine).
QUERY_FLOOR = 0.645
# Picture to picture with CLIP: a near copy or the same series looks 0.8 and
# up; below, likeness is mostly palette and layout.
IMAGE_FLOOR = 0.8
# A text to image match with CLIP ViT-B/32 (measured Nov 8: true matches
# 0.245 to 0.27, the best for a query with no match 0.218).
LOOK_FLOOR = 0.225
LOOK_BAND = 0.035


def _fold(text: str) -> str:
    return unicodedata.normalize("NFC", text).casefold()


def _json_list(text: str | None) -> list:
    try:
        value = json.loads(text or "[]")
    except ValueError:
        return []
    return value if isinstance(value, list) else []


def _themes(row) -> dict[str, float]:
    out: dict[str, float] = {}
    for item in _json_list(row["themes"]):
        if isinstance(item, list) and len(item) == 2 and isinstance(item[0], str):
            try:
                out[item[0]] = float(item[1])
            except (TypeError, ValueError):
                continue
    return out


def _tags(row) -> set[str]:
    return {t for t in _json_list(row["tags"]) if isinstance(t, str)}


def _fuse(rankings: dict[str, list[str]], weights: dict[str, float]) -> dict[str, float]:
    scores: dict[str, float] = {}
    for signal, ranked in rankings.items():
        weight = weights.get(signal, 0.0)
        for rank, path in enumerate(ranked):
            scores[path] = scores.get(path, 0.0) + weight / (RRF_K + rank + 1)
    return scores


def theme_scores(
    mine: dict[str, float], others: dict[str, dict[str, float]]
) -> list[tuple[str, float]]:
    """(path, share) for every picture sharing a theme with `mine`, best
    first. The share is the weighted, rarity-scaled part of `mine` the other
    picture also carries."""
    if not mine:
        return []
    count = len(others) + 1
    df: dict[str, int] = {}
    for themes in others.values():
        for theme in themes:
            df[theme] = df.get(theme, 0) + 1

    def idf(theme: str) -> float:
        # Pictures carrying it, this one included: a theme every picture has
        # tends to nothing, and two pictures alone still relate.
        return math.log((count + 1) / (df.get(theme, 0) + 1))

    total = sum(weight * idf(theme) for theme, weight in mine.items())
    if total <= 0:
        return []
    scored = []
    for path, themes in others.items():
        shared = sum(weight * idf(theme) for theme, weight in mine.items() if theme in themes)
        if shared:
            scored.append((path, shared / total))
    scored.sort(key=lambda item: (-item[1], item[0]))
    return scored


async def related(state, path: str, *, limit: int = 60) -> list[gallery.PinCard]:
    """Pictures about what the one at `path` is about, best first, never
    itself; then the rest, same board first, so the grid never runs dry."""
    pin = await gallery.get_pin(state, path)
    rows = await gallery.rows_by_path(state)
    mine_row = rows.pop(path, None)
    if not rows:
        return []
    rankings: dict[str, list[str]] = {}

    if mine_row is not None:
        shares = theme_scores(_themes(mine_row), {p: _themes(r) for p, r in rows.items()})
        rankings["themes"] = [p for p, share in shares if share >= THEME_FLOOR][:DEPTH]

    vectors = getattr(state, "gallery_vectors", None)
    if vectors is not None and mine_row is not None and mine_row["meaning"]:
        candidates = {p: r["meaning"] for p, r in rows.items() if r["meaning"]}
        close = await vectors.like_text_key(mine_row["meaning"], candidates, DEPTH)
        rankings["meaning"] = [p for p, score in close if score >= MEANING_FLOOR]

    mine = set(pin.tags)
    if mine:
        overlaps = []
        for p, row in rows.items():
            theirs = _tags(row)
            shared = len(mine & theirs)
            if shared:
                overlaps.append((shared / len(mine | theirs), row["created"] or "", p))
        overlaps.sort(reverse=True)
        rankings["tags"] = [p for _j, _c, p in overlaps[:DEPTH]]

    if vectors is not None:
        digest = gallery.image_digest(pin.image[len(gallery.ATTACHMENT_PREFIX):])
        looks = await vectors.like_image(digest, {p: r["digest"] for p, r in rows.items()}, DEPTH)
        rankings["look"] = [p for p, score in looks if score >= IMAGE_FLOOR]

    scores = _fuse(rankings, RELATED_WEIGHTS)
    if pin.board:
        for p in list(scores):
            if rows[p]["board"] == pin.board:
                scores[p] += SAME_BOARD
    ranked = sorted(scores, key=lambda p: (-scores[p], p))
    if len(ranked) < limit:
        rest = sorted(
            (p for p in rows if p not in scores),
            key=lambda p: (rows[p]["board"] != pin.board, _neg(rows[p]["created"]), p),
        )
        ranked += rest
    return await gallery.cards_for(state, ranked[:limit])


def _neg(stamp: str | None) -> str:
    # Newest first inside an ascending sort.
    return "".join(chr(0x10FFFF - ord(c)) for c in (stamp or ""))


async def search(
    state, q: str, *, board: str | None = None, limit: int = 120
) -> list[gallery.PinCard]:
    """Pictures for a query, best first; nothing when nothing matches.
    `board` None searches every board."""
    query = q.strip()
    rows = await gallery.rows_by_path(state)
    if board is not None:
        rows = {p: row for p, row in rows.items() if row["board"] == board}
    if not query or not rows:
        return []
    rankings: dict[str, list[str]] = {}

    terms = _fold(query).split()
    matched = [
        (row["created"] or "", p)
        for p, row in rows.items()
        if all(term in (row["search"] or "") for term in terms)
    ]
    matched.sort(reverse=True)
    rankings["words"] = [p for _c, p in matched]

    vectors = getattr(state, "gallery_vectors", None)
    if vectors is not None:
        qv = await vectors.query_text_vector(query)
        if qv is not None:
            candidates = {p: r["meaning"] for p, r in rows.items() if r["meaning"]}
            close = await vectors.nearest_text(qv, candidates, DEPTH)
            rankings["meaning"] = [p for p, score in close if score >= QUERY_FLOOR]
        looks = await vectors.like_text(query, {p: r["digest"] for p, r in rows.items()}, DEPTH)
        if looks:
            floor = max(LOOK_FLOOR, looks[0][1] - LOOK_BAND)
            rankings["look"] = [p for p, score in looks if score >= floor]

    scores = _fuse(rankings, SEARCH_WEIGHTS)
    ranked = sorted(scores, key=lambda p: (-scores[p], p))
    return await gallery.cards_for(state, ranked[:limit])
