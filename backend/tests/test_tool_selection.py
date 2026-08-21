"""OMNIA must not exceed the Anthropic API's 128-tool ceiling. select_tools
trims each request to a relevant slice while always offering the core."""

from app.overseer.tools import (
    CORE_TOOL_NAMES,
    MAX_TOOLS,
    SUGGESTION_TOOL_DEFS,
    TOOL_DEFS,
    select_tools,
)


def _names(defs):
    return {t.name for t in defs}


def test_full_registry_exceeds_the_api_cap():
    # The reason this module exists: the registry is over 128.
    assert len(TOOL_DEFS) > 128


def test_core_names_all_exist():
    # Guard against drift: a renamed/removed core tool should fail loudly here,
    # not silently drop out of every request.
    missing = CORE_TOOL_NAMES - _names(TOOL_DEFS)
    assert not missing, f"core names no longer in the registry: {sorted(missing)}"
    assert len(CORE_TOOL_NAMES) < MAX_TOOLS  # core must leave room to fit


def test_selection_respects_the_cap_and_keeps_the_core():
    chosen = select_tools(TOOL_DEFS, "help me plan my week")
    assert len(chosen) <= MAX_TOOLS
    assert CORE_TOOL_NAMES <= _names(chosen)


def test_relevance_surfaces_a_niche_tool():
    # "flashcard" tools are NOT core, so they only appear when relevant.
    generic = _names(select_tools(TOOL_DEFS, "tidy up my notes"))
    flashy = _names(select_tools(TOOL_DEFS, "make flashcards from this deck"))
    flashcard_tools = {t.name for t in TOOL_DEFS if "flashcard" in t.name}
    assert flashcard_tools & flashy, "a flashcard query should surface flashcard tools"
    # And the exact user scenario resolves the task tools it needs.
    delete = _names(select_tools(TOOL_DEFS, "can you delete all tasks in school"))
    assert {"delete_task", "list_tasks"} <= delete


def test_small_sets_pass_through_untouched():
    # The proactive/suggestion set is under the cap, so it is never trimmed.
    assert select_tools(SUGGESTION_TOOL_DEFS, "anything") is SUGGESTION_TOOL_DEFS
    assert len(SUGGESTION_TOOL_DEFS) <= MAX_TOOLS
