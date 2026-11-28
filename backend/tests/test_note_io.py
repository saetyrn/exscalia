"""Note file I/O: one atomic writer, one lenient UTF-8 decoder.

Files that arrive out of band (a Windows editor's BOM, a Latin-1 note or
filename from a hand-restored tree) must degrade to one flagged note, never a
lost frontmatter block, a 500 on the whole manifest or a boot crash. Every
write replaces the file rather than truncating it.
"""
import ast
import asyncio
import json
import logging
import os
import threading
from pathlib import Path

import pytest

from app.api import notes as notes_api
from app.index.db import content_hash
from app.vault import fm
from app.vault import notes as notes_mod
from app.vault.internal import atomic_json
from app.vault.notes import NoteError, NoteStore

from conftest import make_client

APP_DIR = Path(__file__).resolve().parents[1] / "app"

BOM_NOTE = (
    "\ufeff---\n"
    "title: Bom\n"
    "created: 2020-01-01\n"
    "tags: [work]\n"
    "pinned: true\n"
    "---\n"
    "\n"
    "Body text.\n"
)


def temps(root: Path) -> list[str]:
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        found += [os.path.join(dirpath, f) for f in filenames if f.endswith(".tmp")]
    return found


def test_bom_note_keeps_its_frontmatter_through_index_and_save(state):
    path = state.store.dir / "Bom.md"
    path.write_bytes(BOM_NOTE.encode("utf-8"))

    content = state.store.read("Bom.md")
    assert not content.startswith("\ufeff")
    meta, body = fm.parse(content)
    assert str(meta["created"]) == "2020-01-01"
    assert meta["pinned"] is True
    assert meta["tags"] == ["work"]
    assert body == "Body text.\n"

    asyncio.run(state.indexer.index_note("Bom.md", content))
    row = asyncio.run(
        state.db.execute_fetchall(
            "SELECT created, pinned FROM notes WHERE path = ?", ("Bom.md",)
        )
    )[0]
    assert str(row["created"]).startswith("2020-01-01")
    assert row["pinned"] == 1
    tags = asyncio.run(
        state.db.execute_fetchall("SELECT tag FROM tags WHERE path = ?", ("Bom.md",))
    )
    assert "work" in {t["tag"] for t in tags}

    # One unchanged save used to stamp a fresh block on top and push the old
    # one (created 2020, pinned) into the body.
    state.store.write("Bom.md", state.store.read("Bom.md"))
    raw = path.read_bytes()
    assert not raw.startswith("\ufeff".encode("utf-8"))
    saved = raw.decode("utf-8")
    assert saved.count("---\n") == 2
    saved_meta, saved_body = fm.parse(saved)
    assert str(saved_meta["created"]) == "2020-01-01"
    assert saved_meta["pinned"] is True
    assert saved_meta["tags"] == ["work"]
    assert saved_body == "Body text.\n"


def test_reading_keeps_universal_newlines(state):
    (state.store.dir / "Crlf.md").write_bytes(b"---\r\ntitle: Crlf\r\n---\r\n\r\nline\r\n")
    assert state.store.read("Crlf.md") == "---\ntitle: Crlf\n---\n\nline\n"


def test_latin1_note_is_a_422_and_the_manifest_still_lists_everything(state):
    (state.store.dir / "Latin.md").write_bytes("---\ntitle: Latin\n---\n\ncaf\xe9\n".encode("latin-1"))
    good = state.store.create("Good.md", "Fine.\n")
    asyncio.run(state.indexer.sync_all())

    with pytest.raises(NoteError) as err:
        state.store.read("Latin.md")
    assert err.value.status == 422
    assert "Latin.md" in err.value.detail
    for write in (
        lambda: state.store.write("Latin.md", "new"),
        lambda: state.store.set_pinned("Latin.md", True),
        lambda: state.store.set_title("Latin.md", "Latin"),
    ):
        with pytest.raises(NoteError) as err:
            write()
        assert err.value.status == 422

    client = make_client(state, notes_api.router)
    assert client.get("/api/notes/Latin.md").status_code == 422
    listed = client.get("/api/notes")
    assert listed.status_code == 200
    assert {n["path"] for n in listed.json()} >= {"Latin.md", "Good.md"}

    response = client.get("/api/notes/manifest")
    assert response.status_code == 200
    revisions = {n["path"]: n["revision"] for n in response.json()["notes"]}
    assert revisions["Latin.md"] is None
    assert revisions["Good.md"] == content_hash(good)


def test_renaming_an_undecodable_note_still_moves_it(state):
    raw = "caf\xe9\n".encode("latin-1")
    (state.store.dir / "Old.md").write_bytes(raw)
    state.store.rename("Old.md", "New.md")
    assert not (state.store.dir / "Old.md").exists()
    assert (state.store.dir / "New.md").read_bytes() == raw


def test_a_failed_replace_keeps_the_original_and_leaves_no_temp(state, monkeypatch):
    original = state.store.create("Keep.md", "Original body.\n")
    real_replace = os.replace
    seen: list[str] = []

    def replace_once(src, dst):
        seen.append(os.fspath(src))
        if len(seen) == 1:
            raise OSError(28, "No space left on device")
        return real_replace(src, dst)

    monkeypatch.setattr(notes_mod.os, "replace", replace_once)
    with pytest.raises(OSError):
        state.store.write("Keep.md", "Replacement body.\n")
    assert state.store.read("Keep.md") == original
    assert temps(state.store.dir) == []
    # Hidden from *.md listings and matched by the vault's `*.tmp` exclude.
    name = Path(seen[0]).name
    assert name.startswith(".Keep.md.") and name.endswith(".tmp")

    saved = state.store.write("Keep.md", "Replacement body.\n")
    assert state.store.read("Keep.md") == saved
    assert "Replacement body." in saved


def test_every_note_writer_leaves_no_temp_behind(state):
    state.store.create("A.md", "one\n")
    state.store.write("A.md", "two\n")
    state.store.set_pinned("A.md", True)
    state.store.write_raw("Deep/Folder/B.md", "raw\n")
    assert (state.store.dir / "Deep/Folder/B.md").read_text(encoding="utf-8") == "raw\n"
    atomic_json(state.store.dir / ".exscalia" / "probe.json", {"ok": True})
    assert temps(state.store.dir) == []
    assert "A.md" in {n.path for n in state.store.list_notes()}


def test_new_notes_keep_ordinary_permissions(state):
    state.store.create("Mode.md", "x\n")
    reference = state.store.dir / "reference.txt"
    reference.write_text("x\n", encoding="utf-8")
    mode = (state.store.dir / "Mode.md").stat().st_mode & 0o777
    assert mode == reference.stat().st_mode & 0o777


def test_atomic_json_writers_never_share_a_temp(tmp_path, monkeypatch):
    target = tmp_path / "store.json"
    real_replace = os.replace
    temps_used: list[str] = []

    def recording(src, dst):
        temps_used.append(os.fspath(src))
        return real_replace(src, dst)

    monkeypatch.setattr(notes_mod.os, "replace", recording)
    atomic_json(target, {"n": 1})
    atomic_json(target, {"n": 2})
    assert len(set(temps_used)) == 2
    monkeypatch.undo()

    # Two threads hammering one store: with one fixed temp name, one thread's
    # replace moved the other's half-written temp or found it gone.
    errors: list[BaseException] = []
    start = threading.Barrier(2)

    def hammer(tag: str) -> None:
        start.wait()
        try:
            for i in range(200):
                atomic_json(target, {"writer": tag, "i": i})
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=hammer, args=(t,)) for t in "ab"]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert json.loads(target.read_text(encoding="utf-8"))["i"] == 199
    assert temps(tmp_path) == []


class SurrogateStore(NoteStore):
    """What Linux os.walk yields for Latin-1 bytes in a name (APFS refuses to
    create such a file, so the walk is doctored instead)."""

    def _walk(self, skip_everywhere, skip_root=frozenset()):
        for root, dirnames, filenames in super()._walk(skip_everywhere, skip_root):
            if root == self.dir:
                dirnames = dirnames + ["d\udce9jà"]
                filenames = filenames + ["caf\udce9.md"]
            yield root, dirnames, filenames


def test_non_utf8_names_are_skipped_and_logged_once(state, caplog):
    store = SurrogateStore(state.store.dir)
    store.create("Good.md", "Fine.\n")
    state.store = store
    state.indexer.store = store

    with caplog.at_level(logging.WARNING, logger="app.vault.notes"):
        paths = {n.path for n in store.list_notes()}
        store.list_notes()
        folders = store.list_folders()
    assert "Good.md" in paths
    assert not any("\udce9" in p for p in paths)
    assert not any("\udce9" in f for f in folders)
    skipped = [r for r in caplog.records if "not UTF-8" in r.getMessage()]
    assert len(skipped) == 2  # the note and the folder, once each

    asyncio.run(state.indexer.sync_all(force=True))
    client = make_client(state, notes_api.router)
    assert client.get("/api/notes").status_code == 200
    assert client.get("/api/notes/folders").status_code == 200
    manifest = client.get("/api/notes/manifest")
    assert manifest.status_code == 200
    assert "Good.md" in {n["path"] for n in manifest.json()["notes"]}


def _raw_note_writes(source: str) -> list[int]:
    """Lines that write through `<x>.resolve(path)`: chained, or via a name
    bound to it earlier in the same function."""
    hits = []
    tree = ast.parse(source)

    def is_resolve(node) -> bool:
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "resolve"
            and bool(node.args)
        )

    scopes = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for scope in [tree, *scopes]:
        bound = {
            target.id
            for node in ast.walk(scope)
            if isinstance(node, ast.Assign) and is_resolve(node.value)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        for node in ast.walk(scope):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("write_text", "write_bytes")
            ):
                continue
            receiver = node.func.value
            if is_resolve(receiver) or (isinstance(receiver, ast.Name) and receiver.id in bound):
                hits.append(node.lineno)
    return sorted(set(hits))


def test_the_guard_catches_both_bypass_shapes():
    chained = "def f(store, p, t):\n    store.resolve(p).write_text(t)\n"
    two_step = "def f(store, p, t):\n    q = store.resolve(p)\n    q.write_text(t)\n"
    fine = "def f(root, t):\n    (root.resolve() / 'x').write_text(t)\n"
    assert _raw_note_writes(chained) == [2]
    assert _raw_note_writes(two_step) == [3]
    assert _raw_note_writes(fine) == []


def test_only_note_store_writes_note_files():
    """Every note write goes through NoteStore (write/create/write_raw), which
    is where the atomic replace lives."""
    offenders = []
    for path in sorted(APP_DIR.rglob("*.py")):
        if path == APP_DIR / "vault" / "notes.py":
            continue
        for line in _raw_note_writes(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.relative_to(APP_DIR)}:{line}")
    assert offenders == []
