from datetime import datetime, timedelta, timezone

import pytest

from app.flashcards import (
    MIN_EASE,
    STARTING_EASE,
    FlashcardStore,
    card_state,
    cards_from_markdown,
    deck_stats,
    new_card,
    order_cards,
    schedule,
)
from app.vault.internal import parse_utc
from app.vault.notes import NoteError

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)


def when_due(card: dict) -> datetime:
    """The moment the card falls due, i.e. when the due queue would serve it."""
    return parse_utc(card["due"])


def test_good_grades_grow_the_interval_geometrically():
    card = new_card("OLS estimator", "(X'X)^-1 X'y")
    assert card["interval"] == 0

    schedule(card, "good", now=NOW)
    assert card["interval"] == 1.0  # first success is always a day out
    assert card["reps"] == 1

    schedule(card, "good", now=when_due(card))
    # Second success multiplies by ease rather than adding a fixed step.
    assert card["interval"] == pytest.approx(STARTING_EASE)
    assert card["ease"] == STARTING_EASE  # `good` leaves ease alone


def test_again_resets_interval_costs_ease_and_counts_a_lapse():
    card = new_card("q", "a", now=NOW)
    schedule(card, "good", now=NOW)
    schedule(card, "good", now=when_due(card))
    assert card["interval"] > 1

    lapsed_at = when_due(card)
    schedule(card, "again", now=lapsed_at)
    assert card["interval"] == 0.0
    assert card["lapses"] == 1
    assert card["ease"] < STARTING_EASE
    # A lapse comes back inside the session, not tomorrow.
    assert parse_utc(card["due"]) == lapsed_at + timedelta(minutes=1)


def test_ease_never_falls_below_the_floor():
    card = new_card("q", "a")
    for _ in range(30):
        schedule(card, "again", now=NOW)
    assert card["ease"] == MIN_EASE


def test_easy_outpaces_good_and_hard_lags_it():
    def run(grade: str) -> float:
        card = new_card("q", "a")
        schedule(card, "good", now=NOW)
        schedule(card, grade, now=when_due(card))
        return card["interval"]

    assert run("hard") < run("good") < run("easy")


def test_intervals_are_capped_at_a_year():
    card = new_card("q", "a")
    for _ in range(40):
        schedule(card, "easy", now=when_due(card))
    assert card["interval"] == 365.0


def test_early_reviews_do_not_grow_the_interval():
    # Cram orders and "Study again" grade every card whatever its due date.
    card = new_card("q", "a", now=NOW)
    for _ in range(6):
        schedule(card, "good", now=NOW)

    assert card["interval"] == 1.0
    assert parse_utc(card["due"]) == NOW + timedelta(days=1)
    assert card["reps"] == 6  # still counts as seen
    assert card["cram_reps"] == 5
    assert card["last_grade"] == "good"
    assert card_state(card) == "learning"


def test_a_due_review_after_cramming_grows_normally():
    card = new_card("q", "a", now=NOW)
    schedule(card, "good", now=NOW)
    schedule(card, "good", now=NOW + timedelta(hours=3))  # early, ignored
    schedule(card, "good", now=NOW + timedelta(days=2))
    assert card["interval"] == pytest.approx(STARTING_EASE)


def test_early_easy_leaves_ease_but_early_again_still_lapses():
    card = new_card("q", "a", now=NOW)
    schedule(card, "good", now=NOW)
    due_before = card["due"]

    schedule(card, "easy", now=NOW + timedelta(minutes=5))
    assert card["ease"] == STARTING_EASE
    assert card["interval"] == 1.0
    assert card["due"] == due_before

    schedule(card, "again", now=NOW + timedelta(minutes=6))
    assert card["interval"] == 0.0
    assert card["lapses"] == 1
    assert card["ease"] < STARTING_EASE


def test_the_in_session_reshow_after_a_lapse_still_counts():
    # "Again" puts the card back at the end of the session; its one-minute due
    # is not a real interval, so recalling it there graduates it as before.
    card = new_card("q", "a", now=NOW)
    schedule(card, "again", now=NOW)
    schedule(card, "good", now=NOW + timedelta(seconds=30))
    assert card["interval"] == 1.0
    assert card.get("cram_reps", 0) == 0


def test_unknown_grade_is_rejected():
    with pytest.raises(NoteError):
        schedule(new_card("q", "a"), "brilliant", now=NOW)


def test_least_known_puts_unseen_then_lapsed_cards_first():
    unseen = new_card("unseen", "a", now=NOW)
    lapsed = new_card("lapsed", "a", now=NOW)
    solid = new_card("solid", "a", now=NOW)
    for _ in range(3):
        schedule(lapsed, "again", now=NOW)
    for _ in range(3):
        schedule(solid, "easy", now=max(NOW, when_due(solid)))

    order = [c["front"] for c in order_cards([solid, lapsed, unseen], "least_known")]
    assert order[0] == "unseen"
    assert order[1] == "lapsed"
    assert order[-1] == "solid"

    reverse = [c["front"] for c in order_cards([solid, lapsed, unseen], "most_known")]
    assert reverse[0] == "solid"


def test_due_order_only_returns_cards_that_are_actually_due():
    fresh = new_card("fresh", "a", now=NOW)
    scheduled = new_card("scheduled", "a", now=NOW)
    schedule(scheduled, "easy", now=NOW)  # pushed days out

    due = [c["front"] for c in order_cards([fresh, scheduled], "due", now=NOW)]
    assert due == ["fresh"]


def test_card_state_and_deck_stats_track_maturity():
    new = new_card("new", "a", now=NOW)
    learning = new_card("learning", "a", now=NOW)
    schedule(learning, "good", now=NOW)
    known = new_card("known", "a", now=NOW)
    for _ in range(6):
        schedule(known, "easy", now=when_due(known))

    assert card_state(new) == "new"
    assert card_state(learning) == "learning"
    assert card_state(known) == "known"

    # Only the unseen card is due right now: `learning` was pushed a day out
    # and `known` is months away.
    stats = deck_stats({"cards": [new, learning, known]}, now=NOW)
    assert stats == {"new": 1, "learning": 1, "known": 1, "total": 3, "due": 1}


def test_cards_parse_from_markdown_and_skip_code_fences():
    content = (
        "# Micro\n"
        "Elasticity :: %ΔQ / %ΔP\n"
        "- Consumer surplus :: area under demand above price\n"
        "```python\n"
        "d = {'a' :: 1}\n"
        "```\n"
        "Not a card line\n"
    )
    cards = cards_from_markdown(content)
    assert [c["front"] for c in cards] == ["Elasticity", "Consumer surplus"]
    assert cards[0]["back"] == "%ΔQ / %ΔP"


def test_store_round_trip_dedupes_and_finds_by_fuzzy_name(tmp_path):
    store = FlashcardStore(tmp_path)
    deck = store.create("Microeconomics — Midterm", cards_from_markdown("A :: B"))

    assert store.get(deck["id"])["name"] == "Microeconomics — Midterm"
    assert store.find_by_name("microecon")["id"] == deck["id"]
    assert store.find_by_name("nothing like this") is None

    store.add_cards(deck["id"], [new_card("A", "duplicate"), new_card("C", "D")])
    fronts = [c["front"] for c in store.get(deck["id"])["cards"]]
    assert fronts == ["A", "C"]  # existing front is not re-added

    card_id = store.get(deck["id"])["cards"][0]["id"]
    store.review(deck["id"], card_id, "good")
    assert store.get(deck["id"])["cards"][0]["reps"] == 1
    # A repeat (double-submitted or crammed) review does not re-schedule it.
    store.review(deck["id"], card_id, "good")
    assert store.get(deck["id"])["cards"][0]["interval"] == 1.0

    store.delete(deck["id"])
    with pytest.raises(NoteError):
        store.get(deck["id"])


def test_deck_ids_are_validated(tmp_path):
    store = FlashcardStore(tmp_path)
    for bad in ("../escape", "with/slash", "", "a" * 65):
        with pytest.raises(NoteError):
            store.get(bad)
