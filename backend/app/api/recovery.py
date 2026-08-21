import difflib
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel

from ..index import parse as note_parse
from ..index.db import content_hash
from ..vault import fm
from ..vault.notes import NoteError

router = APIRouter(prefix="/api", tags=["recovery"])


class RestoreHistoryBody(BaseModel):
    path: str
    sha: str
    version_path: str | None = None
    mode: str = "replace"  # replace | copy
    target_path: str | None = None


class RestoreTrashBody(BaseModel):
    target_path: str | None = None


def _restored_copy_path(state, path: str) -> str:
    original = Path(path)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H%M")
    stem = f"{original.stem} (restored {stamp})"
    candidate = str(original.with_name(stem + ".md"))
    counter = 2
    while state.store.exists(candidate):
        candidate = str(original.with_name(f"{stem} {counter}.md"))
        counter += 1
    return candidate


@router.get("/history")
async def history(
    request: Request,
    path: str = Query(min_length=1),
    limit: int = Query(default=100, ge=1, le=300),
):
    # Validation prevents history from becoming an internal-file escape hatch.
    request.app.state.store.resolve(path)
    import asyncio

    async with request.app.state.vault._git_lock:
        versions = await asyncio.to_thread(
            request.app.state.vault.history_sync, path, limit
        )
    return {"path": path, "versions": versions}


@router.get("/history/content")
async def history_content(
    request: Request, path: str, sha: str, version_path: str | None = None
):
    request.app.state.store.resolve(path)
    source_path = version_path or path
    request.app.state.store.resolve(source_path)
    import asyncio

    async with request.app.state.vault._git_lock:
        content = await asyncio.to_thread(
            request.app.state.vault.commit_content_sync, sha, source_path
        )
    if content is None:
        raise NoteError(404, "That version does not contain this note")
    return {"path": path, "sha": sha, "content": content}


@router.get("/history/diff")
async def history_diff(
    request: Request, path: str, sha: str, version_path: str | None = None
):
    request.app.state.store.resolve(path)
    source_path = version_path or path
    request.app.state.store.resolve(source_path)
    import asyncio

    async with request.app.state.vault._git_lock:
        old = await asyncio.to_thread(
            request.app.state.vault.commit_content_sync, sha, source_path
        )
    if old is None:
        raise NoteError(404, "That version does not contain this note")
    current = request.app.state.store.read(path)
    diff = "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            current.splitlines(keepends=True),
            fromfile=f"{path}@{sha[:8]}",
            tofile=f"{path}@current",
        )
    )
    return {"path": path, "sha": sha, "diff": diff}


@router.post("/history/restore")
async def restore_history(request: Request, body: RestoreHistoryBody):
    state = request.app.state
    state.store.resolve(body.path)
    source_path = body.version_path or body.path
    state.store.resolve(source_path)
    import asyncio

    async with state.vault._git_lock:
        content = await asyncio.to_thread(
            state.vault.commit_content_sync, body.sha, source_path
        )
    if content is None:
        raise NoteError(404, "That version does not contain this note")

    # Capture the exact current working state before replacing it.
    await state.vault.commit_all(f"Snapshot before restoring {body.path}")
    if body.mode == "copy":
        target = body.target_path or _restored_copy_path(state, body.path)
        prepared, _added = note_parse.normalize_task_metadata(content)
        normalized = state.store.create(target, prepared)
    elif body.mode == "replace":
        target = body.path
        current = state.store.read(target)
        prepared, _added = note_parse.normalize_task_metadata(
            content,
            previous_content=current,
        )
        normalized = state.store.write(target, prepared)
    else:
        raise NoteError(400, "Restore mode must be 'replace' or 'copy'")
    await state.indexer.index_note(target, normalized)
    commit = await state.vault.commit_all(
        f"Restore {body.path} from {body.sha[:8]}"
    )
    state.live.note_changed(target, content_hash(normalized))
    meta, _ = fm.parse(normalized)
    return {
        "path": target,
        "content": normalized,
        "meta": meta,
        "revision": content_hash(normalized),
        "commit": commit,
    }


@router.get("/trash")
async def list_trash(request: Request):
    removed = request.app.state.trash.purge_expired()
    if removed:
        request.app.state.autocommit.touch()
    return {"items": request.app.state.trash.list(), "retention_days": 30}


@router.get("/trash/{item_id}")
async def preview_trash(request: Request, item_id: str):
    return request.app.state.trash.preview(item_id)


@router.post("/trash/{item_id}/restore")
async def restore_trash(request: Request, item_id: str, body: RestoreTrashBody | None = None):
    state = request.app.state
    restored = state.trash.restore(
        item_id, body.target_path if body is not None else None
    )
    for path in restored["notes"]:
        content = state.store.read(path)
        await state.indexer.index_note(path, content)
        state.live.note_changed(path, content_hash(content))
    state.autocommit.touch()
    return restored


@router.delete("/trash/{item_id}")
async def purge_trash(request: Request, item_id: str):
    removed = request.app.state.trash.purge(item_id)
    request.app.state.autocommit.touch()
    return {"ok": True, "item": removed}


@router.delete("/trash")
async def empty_trash(request: Request):
    count = request.app.state.trash.empty()
    request.app.state.autocommit.touch()
    return {"ok": True, "removed": count}
