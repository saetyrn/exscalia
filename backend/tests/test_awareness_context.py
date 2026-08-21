"""OMNIA's "current moment" awareness: the user's selection, view, and open
tabs reach the system prompt so "rewrite this" / "what's on here?" resolve."""
import asyncio

from app.overseer.agent import Overseer
from app.overseer.provider import FakeProvider, ProviderReply


def _run(state, **kwargs):
    provider = FakeProvider([ProviderReply(text="ok", stop_reason="end_turn")])
    overseer = Overseer(provider, state)
    asyncio.run(overseer.chat("do the thing", **kwargs))
    return provider.systems[-1]


def test_selection_view_and_tabs_reach_the_prompt(state):
    system = _run(
        state,
        ui_context={
            "selection": "The quick brown fox jumps.",
            "view": "Calendar",
            "open_tabs": ["Notes", "Calendar", "Media"],
        },
    )
    assert "The quick brown fox jumps." in system
    assert "selected in the editor" in system
    assert "Calendar" in system
    assert "Notes, Calendar, Media" in system


def test_long_selection_is_capped(state):
    system = _run(state, ui_context={"selection": "x" * 5000})
    # Capped at 2000 chars in the prompt block.
    assert "x" * 2000 in system
    assert "x" * 2001 not in system


def test_no_ui_context_adds_no_awareness_block(state):
    system = _run(state)
    assert "selected in the editor" not in system
    assert "currently looking at" not in system
