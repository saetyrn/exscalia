"""Flashcard deck endpoints backing the study screen.

Decks are created and pulled up through OMNIA; these routes are what the study
GUI itself talks to once it is open.
"""

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from ..flashcards import GRADES, public_deck
from ..vault.notes import NoteError

router = APIRouter(prefix="/api/flashcards", tags=["flashcards"])


class ReviewBody(BaseModel):
    card_id: str
    grade: str = Field(description="again | hard | good | easy")


@router.get("")
async def list_decks(request: Request):
    return request.app.state.flashcards.summaries()


@router.get("/{deck_id}")
async def get_deck(request: Request, deck_id: str, order: str = Query(default="due")):
    if order not in ("due", "least_known", "most_known"):
        raise NoteError(400, "Unknown study order")
    return public_deck(request.app.state.flashcards.get(deck_id), order)


@router.post("/{deck_id}/review")
async def review_card(request: Request, deck_id: str, body: ReviewBody):
    if body.grade not in GRADES:
        raise NoteError(400, f"Grade must be one of {', '.join(GRADES)}")
    state = request.app.state
    card = state.flashcards.review(deck_id, body.card_id, body.grade)
    state.autocommit.touch()
    return card
