"""Guards on the tool registry itself, rather than on any one tool.

Every defect these catch shipped to production while the whole suite was green,
because the existing tests call executors as plain Python functions — they pass
whatever dict they like and never see the `ToolDef` the model is actually given.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

from app.overseer import tool_expansion, tools
from app.overseer.tools import EXECUTORS, TOOLS


def _declared_keys(tool) -> set[str]:
    return set((tool.input_schema.get("properties") or {}).keys())


def _keys_read(fn) -> set[str]:
    """Literal keys the implementation pulls off its input dict.

    Matches `inp.get("x")` / `inp["x"]`, which is how every executor reads its
    arguments. Dynamic access is ignored — this is a lint, not a proof.
    """
    try:
        source = inspect.getsource(fn)
    except (OSError, TypeError):  # pragma: no cover - not importable from source
        return set()
    tree = ast.parse(textwrap.dedent(source))

    # The input dict is whatever the executor named its third parameter.
    args = next(
        (n.args for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))),
        None,
    )
    if args is None or len(args.args) < 3:
        return set()
    param = args.args[2].arg

    keys: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == param
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            keys.add(node.args[0].value)
        elif (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id == param
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            keys.add(node.slice.value)
    return keys


@pytest.mark.parametrize("tool,fn", TOOLS, ids=[t.name for t, _ in TOOLS])
def test_every_argument_the_tool_reads_is_declared_in_its_schema(tool, fn):
    """A parameter the model is never told about may as well not exist.

    `read_structure` shipped with `_schema({}, [])` while its body read six
    filters, and the agent briefing told the model to use them — so the feature
    was dead in production while its unit tests passed by calling the executor
    directly.
    """
    undeclared = _keys_read(fn) - _declared_keys(tool)
    assert not undeclared, (
        f"{tool.name} reads {sorted(undeclared)} but does not declare "
        f"{'it' if len(undeclared) == 1 else 'them'} in input_schema, "
        "so the model can never send it"
    )


def _calls(fn, attr: str) -> bool:
    try:
        source = textwrap.dedent(inspect.getsource(fn))
    except (OSError, TypeError):  # pragma: no cover
        return False
    return any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == attr
        for n in ast.walk(ast.parse(source))
    )


@pytest.mark.parametrize("tool,fn", TOOLS, ids=[t.name for t, _ in TOOLS])
def test_a_tool_that_writes_is_not_registered_as_read_only(tool, fn):
    """`autocommit.touch()` is the tell that a tool changed something on disk.

    A write registered as read/suggestion gets no confirmation, no scoped
    commit, and no Undo — it just happens. This has now shipped twice:
    `update_habit` (fixed in 0020077) and `review_flashcard`.
    """
    if not _calls(fn, "touch"):
        return
    assert tool.mode != "read", (
        f"{tool.name} writes (it calls autocommit.touch()) but is registered "
        f"mode='read', so it bypasses confirmation and cannot be undone"
    )


@pytest.mark.parametrize("tool,fn", TOOLS, ids=[t.name for t, _ in TOOLS])
def test_a_write_tool_records_an_undoable_action(tool, fn):
    """Writes go through _record so they land in the action log with an Undo."""
    if tool.mode not in {"direct", "confirmed"} or not _calls(fn, "touch"):
        return
    assert _calls(fn, "record") or _calls(fn, "_record") or _record_helper_used(fn), (
        f"{tool.name} writes but never calls _record, so it leaves no "
        "action-log entry and no way back"
    )


def _record_helper_used(fn) -> bool:
    try:
        source = textwrap.dedent(inspect.getsource(fn))
    except (OSError, TypeError):  # pragma: no cover
        return False
    return any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_record"
        for n in ast.walk(ast.parse(source))
    )


def test_every_tool_name_resolves_to_an_executor():
    assert {t.name for t, _ in TOOLS} == set(EXECUTORS)


def test_tool_names_are_unique():
    names = [t.name for t, _ in TOOLS]
    duplicates = {n for n in names if names.count(n) > 1}
    assert not duplicates, f"duplicate tool names shadow each other: {sorted(duplicates)}"


def test_every_executor_a_tool_module_defines_is_registered():
    """A handler the registry never offers is dead code that reads as live.

    tools.py kept staged create_note/edit_note/delete_note/move_note/
    edit_frontmatter long after tool_expansion's direct versions replaced
    them: a name filter dropped them from the registry, so a grep for where
    OMNIA creates a note landed on a function the model could never call.
    """
    registered = {id(fn) for fn in EXECUTORS.values()}
    unregistered = []
    for module in (tools, tool_expansion):
        for name, fn in vars(module).items():
            if name.startswith("_") or not inspect.iscoroutinefunction(fn):
                continue
            if fn.__module__ != module.__name__:
                continue  # imported from elsewhere
            params = list(inspect.signature(fn).parameters)
            if len(params) == 3 and params[0] == "state" and params[2] == "inp":
                if id(fn) not in registered:
                    unregistered.append(f"{module.__name__}.{name}")
    assert not unregistered, (
        f"executor-shaped handlers no tool registers: {sorted(unregistered)}"
    )
