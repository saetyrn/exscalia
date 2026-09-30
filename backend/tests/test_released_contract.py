"""Every API call a shipped client makes still has a route.

The desktop app runs the SPA bundled at its release tag until the user restarts
into an update, and the installed iPad build lags further. Removing or renaming
a route those clients call deploys cleanly and breaks them with no error on the
server. The fixtures in tests/contracts/ list what each shipped client calls
(extract_client_contract.py makes them and says when to regenerate them); this
checks every entry against the routes the app registers now.

Only the method and the path are checked. Routes return untyped dicts, so there
is no response schema to hold response keys against, and request bodies are
not compared either. The Paper wire shapes are pinned by
test_paper_wire_fixtures.py instead.

A call is matched the way the router matches it: the first route whose path
and method both match serves it. That route's parameters must line up with the
parameters the client fills in. A literal segment the client sends must not
land in a server parameter unless it is a file name, so removing
``/api/notes/manifest`` fails here even though ``/api/notes/{path:path}`` would
still take the request (and answer 404). The same rule catches a new catch-all
route declared ahead of a literal one.
"""

import json
import re
from pathlib import Path

import pytest
from fastapi import routing as fastapi_routing

from app.main import app

CONTRACTS = Path(__file__).parent / "contracts"
FIXTURES = sorted(CONTRACTS.glob("*.json"))


# Stands in for every value a client fills in; never part of a literal it sends.
PARAM = "\x00"
# What a route parameter may take: values the client fills in, or a file name
# the client spells out (`…/{page_id}/render.png` into `{name}`). No route
# segment here contains a dot, so a literal route name never passes.
FILLS_PARAM = re.compile(rf"(?:{PARAM}|[^/]*\.[^/]*)(?:/(?:{PARAM}|[^/]*\.[^/]*))*")


def _route_regex(path: str) -> re.Pattern[str]:
    pattern, last = "", 0
    for param in re.finditer(r"\{(\w+)(:\w+)?\}", path):
        pattern += re.escape(path[last : param.start()])
        pattern += f"(?P<{param[1]}>{'.+' if param[2] == ':path' else '[^/]+'})"
        last = param.end()
    return re.compile(pattern + re.escape(path[last:]))


def _api_routes() -> list[tuple[str, re.Pattern[str], set[str]]]:
    # FastAPI keeps included routers nested; iter_route_contexts flattens them
    # in dispatch order, as its OpenAPI generator does. Older versions list
    # routes flat.
    flatten = getattr(fastapi_routing, "iter_route_contexts", None)
    routes = []
    for route in flatten(app.routes) if flatten else app.routes:
        path = getattr(route, "path", None) or ""
        # Only /api: the SPA's catch-all GET /{path:path}, registered when
        # frontend/build exists, would otherwise match every GET.
        if path.startswith("/api/"):
            routes.append((path, _route_regex(path), set(getattr(route, "methods", None) or ())))
    return routes


def _unmatched(entries: list[dict]) -> list[str]:
    routes = _api_routes()
    missing = []
    for entry in entries:
        call = f'{entry["method"]} {entry["path_template"]}  ({entry["source"]})'
        sample = entry["path_template"].replace("{param}", PARAM)
        served = next(
            (
                (path, m)
                for path, rx, methods in routes
                if (m := rx.fullmatch(sample)) and entry["method"] in methods
            ),
            None,
        )
        if served is None:
            missing.append(call)
        elif not all(FILLS_PARAM.fullmatch(v) for v in served[1].groupdict().values()):
            missing.append(f"{call}  served by {served[0]}")
    return missing


def test_there_is_a_desktop_and_an_ipad_contract():
    names = [p.name for p in FIXTURES]
    assert any(n.startswith("desktop-") for n in names), names
    assert any(n.startswith("ipad-") for n in names), names


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda p: p.name)
def test_every_released_call_still_has_a_route(fixture: Path):
    entries = json.loads(fixture.read_text())
    assert entries, f"{fixture.name} lists no calls"
    missing = _unmatched(entries)
    assert not missing, (
        f"{fixture.name}: a shipped client still makes these calls and no route "
        "serves them ('served by' names the route that takes the call instead):\n  "
        + "\n  ".join(missing)
    )


def test_the_check_catches_a_missing_route_a_wrong_method_and_a_swallowed_literal():
    entries = [
        {"method": "GET", "path_template": "/api/paper/history/{param}/{param}", "source": "t"},
        {"method": "GET", "path_template": "/api/notes/manifest", "source": "t"},
        {
            "method": "GET",
            "path_template": "/api/paper/blob/{param}/{param}/render.png",
            "source": "t",
        },
        {"method": "GET", "path_template": "/api/no-such-route", "source": "t"},
        # The history route exists, but not for DELETE.
        {"method": "DELETE", "path_template": "/api/paper/history/{param}/{param}", "source": "t"},
        # One segment short of the restore route: no page id.
        {"method": "POST", "path_template": "/api/paper/history/{param}/restore", "source": "t"},
        # What a removed /api/notes/manifest would look like: the catch-all
        # note route takes it and reads "gone" as a note path.
        {"method": "GET", "path_template": "/api/notes/gone", "source": "t"},
    ]
    assert _unmatched(entries) == [
        "GET /api/no-such-route  (t)",
        "DELETE /api/paper/history/{param}/{param}  (t)",
        "POST /api/paper/history/{param}/restore  (t)",
        "GET /api/notes/gone  (t)  served by /api/notes/{path:path}",
    ]
