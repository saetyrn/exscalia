"""Flashcard decks with SM-2 spaced repetition.

Decks are Git-backed JSON under `.exscalia/flashcards/`, so they are versioned
and carried by the off-site backup exactly like notes and OMNIA threads.

Discovery is deliberately OMNIA-driven: there is no deck browser in the UI. You
ask OMNIA to make a deck or pull one up, and it navigates you into the study
screen.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .vault.internal import atomic_json, iso_utc, parse_utc, read_json
from .vault.notes import NoteError, NoteStore

# SM-2 constants. Ease is a multiplier on the interval; 2.5 is the classic
# starting value and 1.3 the floor that keeps hard cards from stalling forever.
STARTING_EASE = 2.5
MIN_EASE = 1.3
GRADES = ("again", "hard", "good", "easy")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def new_card(front: str, back: str, now: datetime | None = None) -> dict:
    return {
        "id": uuid.uuid4().hex,
        "front": front.strip(),
        "back": back.strip(),
        "ease": STARTING_EASE,
        "interval": 0.0,  # days; 0 = never successfully reviewed
        "due": iso_utc(now or _now()),
        "reps": 0,
        "cram_reps": 0,
        "lapses": 0,
        "last_grade": None,
        "last_reviewed": None,
    }


def schedule(card: dict, grade: str, now: datetime | None = None) -> dict:
    """Apply one review to a card, returning it updated in place.

    Deliberately Anki-like: `again` resets the interval and costs ease, `hard`
    grows it slowly, `good` multiplies by ease, `easy` gives a bonus.

    Only a review of a card that is due moves the schedule. The cram orders
    (`least_known`/`most_known`) and "Study again" grade every card whatever
    its due date, and a card already spaced out was re-multiplied on every
    pass: six same-minute `good` grades pushed a new card to a 97-day
    interval and out of the queue for months. An early pass/`hard`/`easy` is
    now recorded (reps, `cram_reps`, last grade) and changes nothing else.
    `again` still lapses, since forgetting a card early is real information.
    A card at interval 0 is never early: its one-minute due is only the
    "back this session" marker, so the re-show after a lapse still counts.
    """
    if grade not in GRADES:
        raise NoteError(400, f"Grade must be one of {', '.join(GRADES)}")
    moment = now or _now()
    ease = float(card.get("ease") or STARTING_EASE)
    interval = float(card.get("interval") or 0.0)
    reps = int(card.get("reps") or 0)
    lapses = int(card.get("lapses") or 0)

    if grade != "again" and interval > 0 and not _is_due(card, moment):
        card.update(
            {
                "reps": reps + 1,
                "cram_reps": int(card.get("cram_reps") or 0) + 1,
                "last_grade": grade,
                "last_reviewed": iso_utc(moment),
            }
        )
        return card

    if grade == "again":
        ease = max(MIN_EASE, ease - 0.20)
        interval = 0.0
        lapses += 1
    elif grade == "hard":
        ease = max(MIN_EASE, ease - 0.15)
        interval = 1.0 if interval <= 0 else interval * 1.2
    elif grade == "good":
        interval = 1.0 if interval <= 0 else interval * ease
    else:  # easy
        ease = ease + 0.15
        interval = 4.0 if interval <= 0 else interval * ease * 1.3

    reps += 1
    interval = min(interval, 365.0)
    # A lapsed card comes back in the same session rather than tomorrow.
    due = moment + (timedelta(minutes=1) if interval <= 0 else timedelta(days=interval))
    card.update(
        {
            "ease": round(ease, 3),
            "interval": round(interval, 3),
            "reps": reps,
            "lapses": lapses,
            "due": iso_utc(due),
            "last_grade": grade,
            "last_reviewed": iso_utc(moment),
        }
    )
    return card


def card_state(card: dict) -> str:
    """Coarse bucket used for deck stats and the least-known ordering."""
    if not card.get("reps"):
        return "new"
    return "known" if float(card.get("interval") or 0) >= 21 else "learning"


def _knowledge_rank(card: dict) -> tuple:
    """Least known first: unseen cards, then most-lapsed, then lowest ease."""
    return (
        0 if not card.get("reps") else 1,
        -int(card.get("lapses") or 0),
        float(card.get("ease") or STARTING_EASE),
        float(card.get("interval") or 0.0),
    )


def order_cards(cards: list[dict], order: str, now: datetime | None = None) -> list[dict]:
    """`due` = spaced repetition queue. `least_known`/`most_known` = cram modes."""
    moment = now or _now()
    if order == "least_known":
        return sorted(cards, key=_knowledge_rank)
    if order == "most_known":
        return sorted(cards, key=_knowledge_rank, reverse=True)
    due = [c for c in cards if _is_due(c, moment)]
    return sorted(due, key=lambda c: str(c.get("due") or ""))


def _is_due(card: dict, moment: datetime) -> bool:
    raw = card.get("due")
    if not raw:
        return True
    try:
        return parse_utc(str(raw)) <= moment
    except ValueError:
        return True


def deck_stats(deck: dict, now: datetime | None = None) -> dict:
    moment = now or _now()
    cards = deck.get("cards") or []
    buckets = {"new": 0, "learning": 0, "known": 0}
    for card in cards:
        buckets[card_state(card)] += 1
    return {
        **buckets,
        "total": len(cards),
        "due": sum(1 for c in cards if _is_due(c, moment)),
    }


# Q :: A on one line, or a markdown list item of the same shape.
CARD_LINE_RE = re.compile(r"^\s*(?:[-*]\s+)?(.+?)\s*::\s*(.+?)\s*$")


def cards_from_markdown(content: str) -> list[dict]:
    """Extract `Question :: Answer` pairs, skipping fenced code."""
    cards: list[dict] = []
    fenced = False
    for line in content.splitlines():
        if re.match(r"^\s*(```|~~~)", line):
            fenced = not fenced
            continue
        if fenced or "::" not in line:
            continue
        match = CARD_LINE_RE.match(line)
        if match and match.group(1) and match.group(2):
            cards.append(new_card(match.group(1), match.group(2)))
    return cards


class FlashcardStore:
    """One JSON file per deck inside the Git-backed internal directory."""

    def __init__(self, vault_dir: Path):
        self.dir = vault_dir / NoteStore.INTERNAL_DIR / "flashcards"

    def _path(self, deck_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", deck_id or ""):
            raise NoteError(400, "Invalid deck id")
        return self.dir / f"{deck_id}.json"

    def list(self) -> list[dict]:
        decks = []
        if self.dir.is_dir():
            for path in sorted(self.dir.glob("*.json")):
                deck = read_json(path, None, skip_corrupt=True)
                if isinstance(deck, dict) and deck.get("id"):
                    decks.append(deck)
        decks.sort(key=lambda d: str(d.get("updated_at") or ""), reverse=True)
        return decks

    def summaries(self) -> list[dict]:
        return [
            {
                "id": d["id"],
                "name": d.get("name") or d["id"],
                "source_notes": d.get("source_notes") or [],
                "created_at": d.get("created_at"),
                "updated_at": d.get("updated_at"),
                "stats": deck_stats(d),
            }
            for d in self.list()
        ]

    def get(self, deck_id: str) -> dict:
        deck = read_json(self._path(deck_id), None)
        if not isinstance(deck, dict):
            raise NoteError(404, f"Flashcard deck not found: {deck_id}")
        return deck

    def find_by_name(self, query: str) -> dict | None:
        """Loose lookup so 'microecon' finds 'Microeconomics — Midterm'.

        Only that direction is loose. Also matching `name in needle` meant a
        short deck name matched almost any query — a deck called "AP" answered
        a lookup for "grapes" — which let a delete land on the wrong deck. An
        ambiguous partial match now returns None instead of guessing.
        """
        needle = (query or "").strip().lower()
        if not needle:
            return None
        decks = self.list()
        for deck in decks:
            if str(deck.get("name", "")).lower() == needle or deck.get("id") == query:
                return deck
        partial = [d for d in decks if needle in str(d.get("name", "")).lower()]
        return partial[0] if len(partial) == 1 else None

    def save(self, deck: dict) -> dict:
        deck["updated_at"] = iso_utc()
        self.dir.mkdir(parents=True, exist_ok=True)
        atomic_json(self._path(deck["id"]), deck)
        return deck

    def create(
        self, name: str, cards: list[dict], source_notes: list[str] | None = None
    ) -> dict:
        clean_name = " ".join((name or "").split())[:80]
        if not clean_name:
            raise NoteError(400, "Deck name is required")
        deck = {
            "id": uuid.uuid4().hex[:12],
            "name": clean_name,
            "source_notes": source_notes or [],
            "created_at": iso_utc(),
            "updated_at": iso_utc(),
            "cards": cards,
        }
        return self.save(deck)

    def add_cards(self, deck_id: str, cards: list[dict]) -> dict:
        deck = self.get(deck_id)
        existing = {(c["front"].strip().lower()) for c in deck.get("cards") or []}
        fresh = [c for c in cards if c["front"].strip().lower() not in existing]
        deck.setdefault("cards", []).extend(fresh)
        return self.save(deck)

    def rename(self, deck_id: str, name: str) -> dict:
        deck = self.get(deck_id)
        clean = " ".join((name or "").split())[:80]
        if not clean:
            raise NoteError(400, "Deck name is required")
        deck["name"] = clean
        return self.save(deck)

    def review(self, deck_id: str, card_id: str, grade: str) -> dict:
        deck = self.get(deck_id)
        for card in deck.get("cards") or []:
            if card.get("id") == card_id:
                schedule(card, grade)
                self.save(deck)
                return card
        raise NoteError(404, f"Card not found: {card_id}")

    def delete(self, deck_id: str) -> dict:
        deck = self.get(deck_id)
        self._path(deck_id).unlink(missing_ok=True)
        return {"ok": True, "id": deck_id, "name": deck.get("name")}

    def delete_cards(self, deck_id: str, card_ids: list[str]) -> dict:
        deck = self.get(deck_id)
        drop = set(card_ids)
        deck["cards"] = [c for c in deck.get("cards") or [] if c.get("id") not in drop]
        return self.save(deck)


def public_deck(deck: dict, order: str = "due") -> dict[str, Any]:
    """Deck plus a ready-to-study queue for the given order."""
    return {
        "id": deck["id"],
        "name": deck.get("name") or deck["id"],
        "source_notes": deck.get("source_notes") or [],
        "created_at": deck.get("created_at"),
        "updated_at": deck.get("updated_at"),
        "stats": deck_stats(deck),
        "cards": deck.get("cards") or [],
        "queue": [c["id"] for c in order_cards(deck.get("cards") or [], order)],
    }
