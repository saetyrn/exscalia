"""Music API: the library, streaming, and the drop folder.

Thin: the records and the import live in ``app/music.py``. Streaming answers
Range requests (Starlette's FileResponse) from a URL with no file extension,
so Cloudflare never treats the audio as a cacheable file and never strips the
Range header on a miss (MUSIC.md section 2).
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .. import music, music_convert, music_edit, music_inbox, music_links, music_session, music_upload
from ..music_history import history
from ..vault.notes import NoteError

router = APIRouter(prefix="/api/music", tags=["music"])

# A song id is its audio's digest, so the bytes behind a URL never change.
IMMUTABLE = "private, max-age=31536000, immutable"


@router.get("/library")
async def library(request: Request):
    return music.library(request.app.state).snapshot()


@router.get("/stream/{song_id}")
async def stream(song_id: str, request: Request, lossless: bool = False):
    """The MP3 copy by default; the FLAC copy (or the original) when this
    device's Lossless switch is on. Either falls back to the original."""
    lib = music.library(request.app.state)
    song = lib.songs.get(song_id)
    if song is None:
        raise NoteError(404, "No such song")
    if not lib.audio_path(song).is_file():
        song["missing"] = True
        raise NoteError(404, "This song's audio is missing")
    path, mime = music_convert.pick_file(lib, song, lossless)
    return FileResponse(
        path,
        media_type=mime,
        headers={"Cache-Control": IMMUTABLE, "X-Content-Type-Options": "nosniff"},
        content_disposition_type="inline",
        filename=song["file"] if path == lib.audio_path(song) else path.name,
    )


@router.get("/download/{song_id}")
async def download(song_id: str, request: Request):
    """The original file, as uploaded."""
    lib = music.library(request.app.state)
    song = lib.songs.get(song_id)
    if song is None or not lib.audio_path(song).is_file():
        raise NoteError(404, "No such song")
    return FileResponse(
        lib.audio_path(song),
        media_type="application/octet-stream",
        headers={"X-Content-Type-Options": "nosniff"},
        filename=song["file"],
    )


@router.get("/inbox")
async def inbox_status(request: Request):
    state = request.app.state
    last = getattr(getattr(state, "music_inbox", None), "last", None)
    return {"path": str(music_inbox.inbox_dir(state)), "last": last}


@router.post("/inbox/scan")
async def inbox_scan(request: Request):
    """Scan now rather than within the minute. A file still needs to hold
    still across two scans, so call it twice for an immediate import."""
    return await music_inbox.scan(request.app.state)


@router.get("/conversions")
async def conversions(request: Request):
    return music_convert.status(request.app.state)


# -- uploads ------------------------------------------------------------------------


class UploadOpen(BaseModel):
    name: str
    size: int
    folder: str | None = None
    # Folder names from a dropped folder's relative path, under `folder`.
    parts: list[str] = []


@router.post("/uploads")
async def upload_open(body: UploadOpen, request: Request):
    return music_upload.open_upload(request.app.state, body.name, body.size, body.folder, body.parts)


@router.get("/uploads/{upload_id}")
async def upload_get(upload_id: str, request: Request):
    return music_upload.upload_status(request.app.state, upload_id)


@router.put("/uploads/{upload_id}")
async def upload_piece(upload_id: str, offset: int, request: Request):
    return await music_upload.write_piece(request.app.state, upload_id, offset, request.stream())


@router.post("/uploads/{upload_id}/finish")
async def upload_finish(upload_id: str, request: Request):
    return await music_upload.finish(request.app.state, upload_id)


@router.delete("/uploads/{upload_id}")
async def upload_cancel(upload_id: str, request: Request):
    music_upload.cancel(request.app.state, upload_id)
    return {"ok": True}


# -- pictures -----------------------------------------------------------------------

MAX_PICTURE = 8 * 1024 * 1024


@router.post("/pictures")
async def picture_upload(file: UploadFile, request: Request):
    """A cover or playlist background from the owner's computer. Returns the
    vault path to set on a song, folder or playlist."""
    data = await file.read(MAX_PICTURE + 1)
    if len(data) > MAX_PICTURE:
        raise NoteError(413, "Pictures can be up to 8 MB")
    path = await music_edit.store_picture(request.app.state, data, file.content_type)
    return {"path": path}


# -- songs --------------------------------------------------------------------------


class Ids(BaseModel):
    ids: list[str]


class Fields(BaseModel):
    fields: dict[str, Any]


class Liked(BaseModel):
    liked: bool


class Move(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    ids: list[str]
    target: str
    source: str | None = None
    # "copy" on the wire; renamed here because BaseModel has a copy() method.
    also: bool = Field(False, alias="copy")
    index: int | None = None


@router.patch("/songs/{song_id}")
async def song_edit(song_id: str, body: Fields, request: Request):
    return await music_edit.edit_song(request.app.state, song_id, body.fields)


@router.put("/songs/{song_id}/liked")
async def song_like(song_id: str, body: Liked, request: Request):
    return await music_edit.set_liked(request.app.state, song_id, body.liked)


@router.post("/songs/move")
async def songs_move(body: Move, request: Request):
    return await music_edit.move_songs(
        request.app.state, body.ids, body.target, source=body.source, copy=body.also, index=body.index
    )


@router.post("/songs/trash")
async def songs_trash(body: Ids, request: Request):
    return {"trashed": await music_edit.trash_songs(request.app.state, body.ids)}


@router.post("/songs/restore")
async def songs_restore(body: Ids, request: Request):
    return {"restored": await music_edit.restore_songs(request.app.state, body.ids)}


@router.post("/trash/empty")
async def trash_empty(body: Ids | None = None, request: Request = None):
    ids = body.ids if body is not None and body.ids else None
    return {"deleted": await music_edit.empty_trash(request.app.state, ids)}


@router.put("/liked/order")
async def liked_order(body: Ids, request: Request):
    return {"liked": await music_edit.order_liked(request.app.state, body.ids)}


# -- folders ------------------------------------------------------------------------


class FolderNew(BaseModel):
    name: str
    parent: str | None = None
    index: int | None = None


class FolderMove(BaseModel):
    parent: str | None = None
    index: int | None = None


class FolderOrder(BaseModel):
    songs: list[str] | None = None
    liked: list[str] | None = None


class ChildOrder(BaseModel):
    parent: str | None = None
    ids: list[str]


class Cover(BaseModel):
    cover: str | None = None


@router.post("/folders")
async def folder_create(body: FolderNew, request: Request):
    return await music_edit.create_folder(request.app.state, body.name, body.parent, body.index)


@router.patch("/folders/{folder_id}")
async def folder_edit(folder_id: str, body: Fields, request: Request):
    return await music_edit.update_folder(request.app.state, folder_id, body.fields)


@router.post("/folders/{folder_id}/move")
async def folder_move(folder_id: str, body: FolderMove, request: Request):
    return await music_edit.move_folder(request.app.state, folder_id, body.parent, body.index)


@router.put("/folders/{folder_id}/order")
async def folder_order(folder_id: str, body: FolderOrder, request: Request):
    return await music_edit.order_folder(request.app.state, folder_id, body.songs, body.liked)


@router.post("/folders/{folder_id}/remove")
async def folder_remove(folder_id: str, body: Ids, request: Request):
    return {"removed": await music_edit.remove_from_folder(request.app.state, folder_id, body.ids)}


@router.put("/folders/{folder_id}/covers")
async def folder_covers(folder_id: str, body: Cover, request: Request):
    return {"songs": await music_edit.set_folder_covers(request.app.state, folder_id, body.cover)}


@router.delete("/folders/{folder_id}")
async def folder_delete(folder_id: str, request: Request):
    return await music_edit.delete_folder(request.app.state, folder_id)


@router.put("/folder-order")
async def children_order(body: ChildOrder, request: Request):
    return {"ids": await music_edit.order_children(request.app.state, body.parent, body.ids)}


# -- playlists ----------------------------------------------------------------------


class PlaylistNew(BaseModel):
    name: str
    songs: list[str] = []


class PlaylistAdd(BaseModel):
    ids: list[str]
    index: int | None = None


@router.post("/playlists")
async def playlist_create(body: PlaylistNew, request: Request):
    return await music_edit.create_playlist(request.app.state, body.name, body.songs)


@router.patch("/playlists/{playlist_id}")
async def playlist_edit(playlist_id: str, body: Fields, request: Request):
    return await music_edit.update_playlist(request.app.state, playlist_id, body.fields)


@router.post("/playlists/{playlist_id}/add")
async def playlist_add(playlist_id: str, body: PlaylistAdd, request: Request):
    return await music_edit.playlist_add(request.app.state, playlist_id, body.ids, body.index)


@router.post("/playlists/{playlist_id}/remove")
async def playlist_remove(playlist_id: str, body: Ids, request: Request):
    return await music_edit.playlist_remove(request.app.state, playlist_id, body.ids)


@router.delete("/playlists/{playlist_id}")
async def playlist_delete(playlist_id: str, request: Request):
    await music_edit.delete_playlist(request.app.state, playlist_id)
    return {"ok": True}


@router.put("/playlist-order")
async def playlists_order(body: Ids, request: Request):
    return {"ids": await music_edit.order_playlists(request.app.state, body.ids)}


# -- the shared session (Deploy 3) ---------------------------------------------------


class SessionPut(BaseModel):
    base: int | None
    state: dict[str, Any]


class Heartbeat(BaseModel):
    speaker: str
    position: float
    playing: bool
    final: bool = False


class Play(BaseModel):
    song: str
    device: str
    seconds: float
    duration: float | None = None
    ended: str = "stopped"
    repeat: bool = False
    context: str | None = None


@router.get("/session")
async def session_get(request: Request):
    return music_session.get(request.app.state)


@router.put("/session")
async def session_put(body: SessionPut, request: Request):
    """The whole new state, from the version it was made from. A stale base
    answers 409 with the current session for the client to adopt."""
    state = request.app.state
    try:
        return await music_session.update(state, body.base, body.state, music.library(state).songs)
    except music_session.StaleSession as stale:
        return JSONResponse(status_code=409, content={"detail": stale.detail, "session": stale.current})


@router.post("/session/heartbeat")
async def session_heartbeat(body: Heartbeat, request: Request):
    return await music_session.heartbeat(
        request.app.state, body.speaker, body.position, body.playing, body.final
    )


@router.post("/plays")
async def play_record(body: Play, request: Request):
    state = request.app.state
    if body.song not in music.library(state).songs:
        raise NoteError(404, "No such song")
    history(state).record(
        body.song, body.device, body.seconds, body.duration, body.ended, body.repeat, body.context
    )
    return {"ok": True}


@router.get("/recent")
async def recent(request: Request, limit: int = 100):
    return {"played": history(request.app.state).recent(max(1, min(limit, 500)))}


@router.get("/token")
async def stream_token(request: Request):
    """A day-long signed link token for players that cannot send the login
    (the desktop app's <audio>)."""
    return music_links.make_token(request.app.state.settings)
