"""Golden /api/paper responses, checked in where the iPad package reads them.

The iPad's sync tests run against a hand-written server (MockPaperServer.swift)
that nothing ties to the real one. These fixtures are the tie: this test drives
the real routes and compares each response with a file under
``ipad/Packages/PaperCore/Tests/PaperCoreTests/Fixtures``, and PaperCore's
``WireFixtureTests`` decodes the same files with the PaperWire types. A shape
change on the server fails here first; regenerating the files moves the
failure to ``swift test`` if the client cannot read the new shape.

After an intended wire change:

    UPDATE_FIXTURES=1 pytest tests/test_paper_wire_fixtures.py

then commit the files and run ``swift test`` in ipad/Packages/PaperCore.
"""

import asyncio
import difflib
import json
import os
import re
from pathlib import Path

from test_paper_api import PNG, make_client, new_notebook, put_page

FIXTURES = (
    Path(__file__).resolve().parents[2]
    / "ipad/Packages/PaperCore/Tests/PaperCoreTests/Fixtures"
)

NOTEBOOK_ID = "5eed" * 8
PAGE = "a" * 32
OTHER = "b" * 32

# Values that change run to run are pinned to constants of the same shape, so
# the files still carry what a parser has to cope with: `iso_utc()` stamps keep
# their six fractional digits (the shape ISO8601DateFormatter refused, HANDOFF
# §7 Paper), git's commit times keep their numeric offset.
TIMESTAMP_KEYS = {"created", "modified", "deleted_at", "expires_at", "committed_at", "at"}
_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?P<fraction>\.\d+)?(?P<zone>Z|[+-]\d{2}:\d{2})?"
)
_CONFLICT_STAMP = re.compile(r"\.conflict-\d{14}")
EPOCH = "e0e0e0e0e0e0"
TRASH_ID = "7a54" * 8
ARCHIVE_ID = "a7c4" * 8
WHITEBOARD_ID = "b0a4" * 8
BOARD = "c" * 32
# What an empty board renders: 1,366 x 1,024 around the centre of the
# default 16,384 extent.
BOARD_RECT = {"x": 7509.0, "y": 7680.0, "width": 1366.0, "height": 1024.0}
BOARD_EXTENT = {"width": 16384.0, "height": 16384.0}


def _timestamp(value):
    match = _TIMESTAMP.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        return value  # not a timestamp any more: let the diff show it
    fraction, zone = match["fraction"] or "", match["zone"] or ""
    if zone == "Z" and not fraction:
        fraction = ".000000"  # isoformat() drops an all-zero microsecond
    digits = max(len(fraction) - 1, 0)
    out = "2026-09-02T10:00:00"
    if digits:
        out += "." + "123456789"[:digits].ljust(digits, "0")
    if zone:
        out += "Z" if zone == "Z" else "+00:00"
    return out


def canonical(value, substitutions: dict[str, str], key: str | None = None):
    if isinstance(value, dict):
        return {k: canonical(v, substitutions, k) for k, v in value.items()}
    if isinstance(value, list):
        return [canonical(item, substitutions) for item in value]
    if key in TIMESTAMP_KEYS:
        return _timestamp(value)
    if isinstance(value, str):
        value = _CONFLICT_STAMP.sub(".conflict-20260902100000", value)
        # Longest first, so a full sha is replaced before its short prefix.
        for real in sorted(substitutions, key=len, reverse=True):
            value = value.replace(real, substitutions[real])
    return value


def compare(name: str, payload, substitutions: dict[str, str]) -> str | None:
    """None when the file matches; otherwise what to tell the developer."""
    text = (
        json.dumps(canonical(payload, substitutions), sort_keys=True, indent=2) + "\n"
    )
    path = FIXTURES / f"{name}.json"
    if os.environ.get("UPDATE_FIXTURES") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return f"wrote {path.name}"
    if not path.is_file():
        return f"{path.name} is missing"
    stored = path.read_text(encoding="utf-8")
    if stored == text:
        return None
    diff = difflib.unified_diff(
        stored.splitlines(), text.splitlines(), f"{name}.json", "live", lineterm=""
    )
    return "\n".join(list(diff)[:60])


def drive(client, state) -> tuple[dict, dict[str, str]]:
    """One notebook's life through the real routes: two committed saves, a
    second page, a lost race, a delete. Returns each fixture's response."""
    nb = new_notebook(client, "Ideas", folder="Work", id=NOTEBOOK_ID)
    path = nb["path"]
    out: dict = {}

    first = put_page(client, path, PAGE, 0, drawing=b"first", client_id="ipad-1")
    assert first.status_code == 200, first.text
    asyncio.run(state.vault.commit_all("paper: first"))

    # All four parts, the page part shaped the way `pagePartJSON` sends it:
    # an infinite page that has grown past its preset carries `size_points`.
    saved = put_page(
        client,
        path,
        PAGE,
        1,
        page={
            "template": "grid",
            "size": {"width": 1024.0, "height": 1366.0},
            "size_points": {"width": 1024.0, "height": 2048.5},
            "objects": [],
            "bookmarked": True,
            "title": "Sketch",
        },
        drawing=b"second",
        render=PNG,
        text={"lines": [], "transcript": "hello"},
        client_id="ipad-1",
    )
    assert saved.status_code == 200, saved.text
    out["save-200"] = saved.json()
    asyncio.run(state.vault.commit_all("paper: second"))

    other = put_page(client, path, OTHER, 0, drawing=b"other", after=PAGE)
    assert other.status_code == 200, other.text

    # A device that last saw revision 1 loses the race.
    late = put_page(client, path, PAGE, 1, drawing=b"stale-device", client_id="ipad-2")
    assert late.status_code == 409, late.text
    out["save-409"] = late.json()

    for name, url in (
        ("library", "/api/paper"),
        ("notebook", f"/api/paper/notebooks/{path}"),
        ("page", f"/api/paper/pages/{path}/{PAGE}"),
        ("history", f"/api/paper/history/{path}/{PAGE}"),
        ("changes-full", "/api/paper/changes"),
    ):
        response = client.get(url)
        assert response.status_code == 200, (url, response.text)
        out[name] = response.json()

    cursor, epoch = out["changes-full"]["seq"], out["changes-full"]["epoch"]
    moved = put_page(client, path, PAGE, 2, drawing=b"third", client_id="ipad-1")
    assert moved.status_code == 200, moved.text
    assert client.delete(f"/api/paper/pages/{path}/{OTHER}").status_code == 200
    out["changes"] = client.get(
        f"/api/paper/changes?since={cursor}&epoch={epoch}"
    ).json()
    out["trash"] = client.get("/api/paper/trash").json()

    # Folders, after the nine above so those change only by added keys.
    # `paper/Work` came with the notebook and holds its legacy record.
    archive = client.post(
        "/api/paper/folders",
        json={
            "parent": "paper",
            "name": "Archive",
            "id": ARCHIVE_ID,
            "color": "orange",
            "icon": "symbol:briefcase",
            "client_id": "ipad-1",
        },
    )
    assert archive.status_code == 200, archive.text
    out["folders"] = client.get("/api/paper/folders").json()
    cursor = client.get("/api/paper/changes").json()["seq"]
    renamed = client.post(
        "/api/paper/folders/paper/Work/rename",
        json={"new_path": "paper/Work 2026", "name": "Work 2026", "client_id": "ipad-1"},
    )
    assert renamed.status_code == 200, renamed.text
    out["folder-rename"] = renamed.json()
    out["changes-folders"] = client.get(
        f"/api/paper/changes?since={cursor}&epoch={epoch}"
    ).json()

    # Documents, after the folder steps: a whiteboard and its first board,
    # saved the way the iPad sends one (`pagePartJSON`: the extent always,
    # never `size_points`; the render's frame as its own field).
    whiteboard = new_notebook(
        client,
        "Plans",
        id=WHITEBOARD_ID,
        kind="whiteboard",
        paperColor="cream",
        template="grid",
        client_id="ipad-1",
    )
    board = put_page(
        client,
        whiteboard["path"],
        BOARD,
        0,
        page={
            "template": "grid",
            "size": BOARD_EXTENT,
            "size_name": "board",
            "board_extent": BOARD_EXTENT,
            "paper_color": "cream",
            "objects": [],
            "bookmarked": False,
            "title": "Board 1",
        },
        drawing=b"board",
        render=PNG,
        render_rect=BOARD_RECT,
        client_id="ipad-1",
    )
    assert board.status_code == 200, board.text
    for name, url in (
        ("notebook-whiteboard", f"/api/paper/notebooks/{whiteboard['path']}"),
        ("page-board", f"/api/paper/pages/{whiteboard['path']}/{BOARD}"),
    ):
        response = client.get(url)
        assert response.status_code == 200, (url, response.text)
        out[name] = response.json()

    substitutions = {epoch: EPOCH}
    for number, version in enumerate(out["history"]["versions"], start=1):
        placeholder = (f"{number:x}" * 40)[:40]
        substitutions[version["sha"]] = placeholder
        substitutions[version["short_sha"]] = placeholder[: len(version["short_sha"])]
    for item in out["trash"]["items"]:
        substitutions[item["id"]] = TRASH_ID
    return out, substitutions


def test_the_wire_fixtures_match_what_the_server_answers(state):
    responses, substitutions = drive(make_client(state), state)

    # The fixtures are only worth having if they hold the edges the client
    # has to decode; pin those before comparing files.
    assert responses["save-409"]["conflict"]["sibling"]["conflict_of"] == PAGE
    assert responses["changes-full"]["full"] is True
    assert responses["changes"]["full"] is False
    assert [row["kind"] for row in responses["changes"]["deleted"]] == ["page"]
    assert responses["page"]["size_points"] == {"width": 1024.0, "height": 2048.5}
    assert len(responses["history"]["versions"]) == 2
    assert [r["path"] for r in responses["folders"]["folders"]] == [
        "paper/Archive",
        "paper/Work",
    ]
    assert responses["folder-rename"]["path"] == "paper/Work 2026"
    moves = responses["changes-folders"]["folder_moves"]
    assert [(m["from"], m["to"]) for m in moves] == [("paper/Work", "paper/Work 2026")]
    # A folder rename restamps nothing inside it.
    assert responses["changes-folders"]["pages"] == []
    assert responses["changes-folders"]["notebooks"] == []
    # A notebook made by a client that names no kind still reads as one.
    assert responses["library"]["notebooks"][0]["kind"] == "notebook"
    whiteboard = responses["notebook-whiteboard"]
    assert (whiteboard["kind"], whiteboard["paperSize"]) == ("whiteboard", "board")
    assert whiteboard["cover"] == BOARD == whiteboard["pages"][0]
    # A board reads as the area its render shows, with no size_points: what
    # the v0.1.30 viewer sizes the image by.
    board = responses["page-board"]
    assert board["size_name"] == "board"
    assert board["size"] == {"width": 1366.0, "height": 1024.0}
    assert "size_points" not in board
    assert board["board_extent"] == BOARD_EXTENT
    assert board["render_rect"] == BOARD_RECT
    assert whiteboard["pages_detail"] == [board]

    problems = {
        name: problem
        for name, payload in sorted(responses.items())
        if (problem := compare(name, payload, substitutions)) is not None
    }
    if os.environ.get("UPDATE_FIXTURES") == "1":
        raise AssertionError(
            f"Rewrote {len(problems)} fixture(s) in {FIXTURES}. Commit them, then run "
            "`swift test` in ipad/Packages/PaperCore. Unset UPDATE_FIXTURES to compare."
        )
    stray = sorted(
        path.name
        for path in FIXTURES.glob("*.json")
        if path.stem not in responses
    )
    assert not stray, f"fixtures no test writes: {stray}"
    assert not problems, (
        "The /api/paper wire shape changed. If that was intended, rerun with "
        "UPDATE_FIXTURES=1, commit the files and run `swift test` in "
        "ipad/Packages/PaperCore.\n\n" + "\n\n".join(problems.values())
    )
