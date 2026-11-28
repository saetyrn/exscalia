"""Music: every change to the library after import.

One service layer, used by the API now and by OMNIA's tools in Deploy 5, so
both take the same path to the records, the commit and the live event. Each
operation takes the library lock, changes what it must, saves exactly the
records it touched and announces the change once.

Rules from MUSIC.md section 3:
- A song can sit in several folders, each with its own order. A song in no
  folder lands in Unsorted.
- Liking a song puts it first in Liked and first in the liked part of every
  folder it is in; it also stays in its regular place.
- A song's own cover is the same everywhere. Changing a folder's picked cover
  moves the songs that showed the old one to the new one.
- Deleting a song puts it in the trash with the folders it was in. Its audio
  stays until the owner empties the trash; nothing empties it on a timer.
- Details the owner types are recorded in ``manual`` so nothing automatic
  ever overwrites them (the fill-in pass, Deploy 5).
"""
from __future__ import annotations

import shutil
from pathlib import Path

from . import music
from .music import UNSORTED_ID, MusicError, MusicLibrary, _new_folder, _new_playlist
from .vault.internal import iso_utc, utc_now

SONG_FIELDS = ("title", "artist", "album", "year", "genre", "track", "cover")
TEXT_FIELDS = {"title": 300, "artist": 300, "album": 300, "genre": 100}
FOLDER_COLOURS = (
    "f59cb2", "b48d71", "a05c46", "e49e91", "d5708b",
    "e9b4b3", "f2dcbe", "f0b6c6", "eeceb6",
)
FOLDER_GLYPHS = (
    "note", "headphones", "mic", "vinyl", "heart",
    "star", "moon", "flame", "cloud", "bolt",
)


# -- helpers -----------------------------------------------------------------------


def _song(lib: MusicLibrary, song_id: str) -> dict:
    song = lib.songs.get(song_id)
    if song is None or song.get("trashed"):
        raise MusicError(404, "No such song")
    return song


def _folder(lib: MusicLibrary, folder_id: str) -> dict:
    folder = lib.folders.get(folder_id)
    if folder is None:
        raise MusicError(404, "No such folder")
    return folder


def _playlist(lib: MusicLibrary, playlist_id: str) -> dict:
    playlist = lib.playlists.get(playlist_id)
    if playlist is None:
        raise MusicError(404, "No such playlist")
    return playlist


def _songs(lib: MusicLibrary, ids: list[str]) -> list[str]:
    out = list(dict.fromkeys(ids))
    for song_id in out:
        _song(lib, song_id)
    return out


def _name(value: object, what: str) -> str:
    text = str(value or "").replace("\x00", " ").strip()
    if not text:
        raise MusicError(400, f"A {what} needs a name")
    if len(text) > 120:
        raise MusicError(400, f"That {what} name is too long")
    return text


def _cover_path(state, value: object) -> str | None:
    """A cover is a vault attachment path, or None to clear it."""
    if value in (None, ""):
        return None
    path = str(value)
    try:
        abs_path = state.store.resolve_attachment(path)
    except Exception as exc:
        raise MusicError(400, "That picture is not in the vault's attachments") from exc
    if not abs_path.is_file():
        raise MusicError(400, "That picture does not exist")
    return path


def _siblings(lib: MusicLibrary, parent: str | None) -> list[str]:
    return lib.root if parent is None else _folder(lib, parent)["children"]


def _place(ids: list[str], item: str, index: int | None) -> None:
    if item in ids:
        ids.remove(item)
    if index is None or index >= len(ids):
        ids.append(item)
    else:
        ids.insert(max(0, index), item)


def _same_set(current: list[str], proposed: list[str], what: str) -> list[str]:
    proposed = list(dict.fromkeys(proposed))
    if sorted(proposed) != sorted(current):
        raise MusicError(409, f"The {what} changed meanwhile; reload and try again")
    return proposed


def _rehome(lib: MusicLibrary, song_id: str, saved: set[str]) -> None:
    """A song that is in no folder any more goes to Unsorted."""
    if any(song_id in f["songs"] for f in lib.folders.values()):
        return
    unsorted = lib._unsorted()
    unsorted["songs"].append(song_id)
    saved.add(unsorted["id"])


def _finish(state, lib: MusicLibrary, *, songs=(), folders=(), playlists=(), root=False) -> None:
    for song_id in songs:
        if song_id in lib.songs:
            lib._save_song(lib.songs[song_id])
    for folder_id in folders:
        if folder_id in lib.folders:
            lib._save_folder(lib.folders[folder_id])
    for playlist_id in playlists:
        if playlist_id in lib.playlists:
            lib._save_playlist(lib.playlists[playlist_id])
    if root or (UNSORTED_ID in folders and UNSORTED_ID in lib.root):
        lib._save_root()
    music.changed(state, lib)


# -- songs ---------------------------------------------------------------------------


async def edit_song(state, song_id: str, fields: dict) -> dict:
    lib = music.library(state)
    async with lib.lock:
        song = _song(lib, song_id)
        manual = set(song.get("manual") or [])
        for key, value in fields.items():
            if key not in SONG_FIELDS:
                raise MusicError(400, f"Unknown song field: {key}")
            if key == "cover":
                value = _cover_path(state, value)
            elif key in ("year", "track"):
                if value in (None, ""):
                    value = None
                else:
                    try:
                        value = int(value)
                    except (TypeError, ValueError) as exc:
                        raise MusicError(400, f"{key} must be a number") from exc
                    if not 0 < value < 10000:
                        raise MusicError(400, f"{key} is out of range")
            else:
                value = str(value or "").strip()[: TEXT_FIELDS[key]] or None
                if key == "title" and not value:
                    raise MusicError(400, "A song needs a title")
            song[key] = value
            manual.add(key)
        song["manual"] = sorted(manual)
        _finish(state, lib, songs=[song_id])
        return song


async def set_liked(state, song_id: str, liked: bool) -> dict:
    lib = music.library(state)
    async with lib.lock:
        song = _song(lib, song_id)
        touched: set[str] = set()
        song["liked"] = bool(liked)
        if liked:
            song["liked_at"] = iso_utc(utc_now())
            _place(lib.liked, song_id, 0)
            for folder in lib.folders.values():
                if song_id in folder["songs"]:
                    _place(folder["liked"], song_id, 0)
                    touched.add(folder["id"])
        else:
            song.pop("liked_at", None)
            if song_id in lib.liked:
                lib.liked.remove(song_id)
            for folder in lib.folders.values():
                if song_id in folder["liked"]:
                    folder["liked"].remove(song_id)
                    touched.add(folder["id"])
        _finish(state, lib, songs=[song_id], folders=touched, root=True)
        return song


async def order_liked(state, ids: list[str]) -> list[str]:
    lib = music.library(state)
    async with lib.lock:
        lib.liked = _same_set(lib.liked, ids, "Liked list")
        _finish(state, lib, root=True)
        return lib.liked


async def trash_songs(state, ids: list[str]) -> list[str]:
    lib = music.library(state)
    async with lib.lock:
        ids = _songs(lib, ids)
        folders: set[str] = set()
        playlists: set[str] = set()
        for song_id in ids:
            song = lib.songs[song_id]
            was_in = [f["id"] for f in lib.folders.values() if song_id in f["songs"]]
            song["trashed"] = {"at": iso_utc(utc_now()), "folders": was_in}
            for folder_id in was_in:
                folder = lib.folders[folder_id]
                folder["songs"].remove(song_id)
                if song_id in folder["liked"]:
                    folder["liked"].remove(song_id)
                folders.add(folder_id)
            for playlist in lib.playlists.values():
                if song_id in playlist["songs"]:
                    playlist["songs"] = [s for s in playlist["songs"] if s != song_id]
                    playlists.add(playlist["id"])
            if song_id in lib.liked:
                lib.liked.remove(song_id)
        _finish(state, lib, songs=ids, folders=folders, playlists=playlists, root=True)
        return ids


async def restore_songs(state, ids: list[str]) -> list[str]:
    lib = music.library(state)
    async with lib.lock:
        restored = []
        folders: set[str] = set()
        for song_id in dict.fromkeys(ids):
            song = lib.songs.get(song_id)
            if song is None or not song.get("trashed"):
                continue
            homes = [f for f in song["trashed"].get("folders", []) if f in lib.folders]
            song.pop("trashed", None)
            for folder_id in homes:
                lib.folders[folder_id]["songs"].append(song_id)
                if song.get("liked"):
                    lib.folders[folder_id]["liked"].insert(0, song_id)
                folders.add(folder_id)
            if song.get("liked"):
                _place(lib.liked, song_id, 0)
            _rehome(lib, song_id, folders)
            restored.append(song_id)
        _finish(state, lib, songs=restored, folders=folders, root=True)
        return restored


async def empty_trash(state, ids: list[str] | None = None) -> list[str]:
    """The only path that deletes audio: the owner emptying the trash."""
    lib = music.library(state)
    async with lib.lock:
        trashed = [s for s, song in lib.songs.items() if song.get("trashed")]
        doomed = trashed if ids is None else [s for s in ids if s in trashed]
        for song_id in doomed:
            song = lib.songs.pop(song_id)
            lib._song_path(song_id).unlink(missing_ok=True)
            shutil.rmtree(lib.audio_path(song).parent, ignore_errors=True)
        if doomed:
            from .music_history import history

            history(state).forget(doomed)
        if doomed:
            music.changed(state, lib)
        return doomed


# -- folders -------------------------------------------------------------------------


async def create_folder(state, name: str, parent: str | None = None, index: int | None = None) -> dict:
    lib = music.library(state)
    async with lib.lock:
        name = _name(name, "folder")
        siblings = _siblings(lib, parent)
        if any(lib.folders[f]["name"].casefold() == name.casefold() for f in siblings):
            raise MusicError(409, f"There is already a folder called {name} here")
        folder = _new_folder(name)
        folder["colour"] = FOLDER_COLOURS[len(lib.folders) % len(FOLDER_COLOURS)]
        lib.folders[folder["id"]] = folder
        _place(siblings, folder["id"], index)
        _finish(state, lib, folders=[folder["id"]] + ([parent] if parent else []), root=parent is None)
        return folder


async def update_folder(state, folder_id: str, fields: dict) -> dict:
    lib = music.library(state)
    async with lib.lock:
        folder = _folder(lib, folder_id)
        songs: set[str] = set()
        for key, value in fields.items():
            if key == "name":
                name = _name(value, "folder")
                parent = lib.parents().get(folder_id)
                for other in _siblings(lib, parent):
                    if other != folder_id and lib.folders[other]["name"].casefold() == name.casefold():
                        raise MusicError(409, f"There is already a folder called {name} here")
                folder["name"] = name
            elif key == "colour":
                colour = str(value or "").lstrip("#").lower() or None
                if colour is not None and (len(colour) != 6 or any(c not in "0123456789abcdef" for c in colour)):
                    raise MusicError(400, "A colour is six hex digits")
                folder["colour"] = colour
            elif key == "glyph":
                if value not in (None, "") and value not in FOLDER_GLYPHS:
                    raise MusicError(400, f"Unknown glyph: {value}")
                folder["glyph"] = value or None
            elif key == "pinned":
                folder["pinned"] = bool(value)
            elif key == "cover":
                old = folder.get("cover")
                new = _cover_path(state, value)
                folder["cover"] = new
                if old and new != old:
                    for song_id in folder["songs"]:
                        song = lib.songs[song_id]
                        if song.get("cover") == old:
                            song["cover"] = new
                            songs.add(song_id)
            else:
                raise MusicError(400, f"Unknown folder field: {key}")
        _finish(state, lib, songs=songs, folders=[folder_id])
        return folder


async def set_folder_covers(state, folder_id: str, cover: str | None) -> list[str]:
    """Every song in the folder to one picture (an OMNIA ask, MUSIC.md 3)."""
    lib = music.library(state)
    async with lib.lock:
        folder = _folder(lib, folder_id)
        path = _cover_path(state, cover)
        for song_id in folder["songs"]:
            lib.songs[song_id]["cover"] = path
        _finish(state, lib, songs=list(folder["songs"]))
        return list(folder["songs"])


async def move_folder(state, folder_id: str, parent: str | None, index: int | None = None) -> dict:
    lib = music.library(state)
    async with lib.lock:
        folder = _folder(lib, folder_id)
        at = parent
        while at is not None:
            if at == folder_id:
                raise MusicError(400, "A folder cannot go inside itself")
            at = lib.parents().get(at)
        target = _siblings(lib, parent)
        for other in target:
            if other != folder_id and lib.folders[other]["name"].casefold() == folder["name"].casefold():
                raise MusicError(409, f"There is already a folder called {folder['name']} there")
        old_parent = lib.parents().get(folder_id)
        _siblings(lib, old_parent).remove(folder_id)
        _place(target, folder_id, index)
        touched = [f for f in (old_parent, parent) if f]
        _finish(state, lib, folders=touched, root=old_parent is None or parent is None)
        return folder


async def order_children(state, parent: str | None, ids: list[str]) -> list[str]:
    lib = music.library(state)
    async with lib.lock:
        siblings = _siblings(lib, parent)
        siblings[:] = _same_set(siblings, ids, "folder order")
        _finish(state, lib, folders=[parent] if parent else [], root=parent is None)
        return list(siblings)


async def delete_folder(state, folder_id: str) -> dict:
    """The folder and everything under it goes. Songs that live in no other
    folder go to the trash, so nothing is lost by a delete."""
    lib = music.library(state)
    async with lib.lock:
        _folder(lib, folder_id)
        doomed: list[str] = []
        queue = [folder_id]
        while queue:
            current = queue.pop()
            doomed.append(current)
            queue.extend(lib.folders[current]["children"])
        doomed_set = set(doomed)
        songs = {s for f in doomed for s in lib.folders[f]["songs"]}
        elsewhere = {
            s for f in lib.folders.values() if f["id"] not in doomed_set for s in f["songs"]
        }
        orphans = sorted(songs - elsewhere)
        parent = lib.parents().get(folder_id)
        _siblings(lib, parent).remove(folder_id)
        for fid in doomed:
            lib.folders.pop(fid)
            lib._folder_path(fid).unlink(missing_ok=True)
        now = iso_utc(utc_now())
        for song_id in orphans:
            song = lib.songs[song_id]
            song["trashed"] = {"at": now, "folders": []}
            if song_id in lib.liked:
                lib.liked.remove(song_id)
        _finish(state, lib, songs=orphans, folders=[parent] if parent else [], root=True)
        return {"folders": doomed, "trashed": orphans}


async def order_folder(state, folder_id: str, songs: list[str] | None = None, liked: list[str] | None = None) -> dict:
    lib = music.library(state)
    async with lib.lock:
        folder = _folder(lib, folder_id)
        if songs is not None:
            folder["songs"] = _same_set(folder["songs"], songs, "folder")
        if liked is not None:
            folder["liked"] = _same_set(folder["liked"], liked, "liked part")
        _finish(state, lib, folders=[folder_id])
        return folder


async def move_songs(
    state,
    ids: list[str],
    target: str,
    *,
    source: str | None = None,
    copy: bool = False,
    index: int | None = None,
) -> dict:
    """Move songs from ``source`` into ``target`` (or add them there too when
    ``copy``). Liked songs keep their place in the target's liked part."""
    lib = music.library(state)
    async with lib.lock:
        ids = _songs(lib, ids)
        dest = _folder(lib, target)
        touched = {target}
        if not copy and source is not None and source != target:
            origin = _folder(lib, source)
            for song_id in ids:
                if song_id in origin["songs"]:
                    origin["songs"].remove(song_id)
                if song_id in origin["liked"]:
                    origin["liked"].remove(song_id)
            touched.add(source)
        position = index
        for song_id in ids:
            if source == target and not copy:
                _place(dest["songs"], song_id, position)
            elif song_id not in dest["songs"]:
                _place(dest["songs"], song_id, position)
            else:
                continue
            if position is not None:
                position = dest["songs"].index(song_id) + 1
            if lib.songs[song_id].get("liked") and song_id not in dest["liked"]:
                dest["liked"].insert(0, song_id)
        if not copy and source == UNSORTED_ID and not lib.folders[UNSORTED_ID]["songs"]:
            pass  # an empty Unsorted stays until the owner deletes it
        _finish(state, lib, folders=touched)
        return dest


async def remove_from_folder(state, folder_id: str, ids: list[str]) -> list[str]:
    lib = music.library(state)
    async with lib.lock:
        folder = _folder(lib, folder_id)
        ids = _songs(lib, ids)
        touched = {folder_id}
        for song_id in ids:
            if song_id in folder["songs"]:
                folder["songs"].remove(song_id)
            if song_id in folder["liked"]:
                folder["liked"].remove(song_id)
            _rehome(lib, song_id, touched)
        _finish(state, lib, folders=touched)
        return ids


# -- playlists ---------------------------------------------------------------------


async def create_playlist(state, name: str, songs: list[str] | None = None) -> dict:
    lib = music.library(state)
    async with lib.lock:
        playlist = _new_playlist(_name(name, "playlist"))
        playlist["songs"] = _songs(lib, songs or [])
        lib.playlists[playlist["id"]] = playlist
        lib.playlist_order.append(playlist["id"])
        _finish(state, lib, playlists=[playlist["id"]], root=True)
        return playlist


async def update_playlist(state, playlist_id: str, fields: dict) -> dict:
    lib = music.library(state)
    async with lib.lock:
        playlist = _playlist(lib, playlist_id)
        for key, value in fields.items():
            if key == "name":
                playlist["name"] = _name(value, "playlist")
            elif key in ("cover", "background"):
                playlist[key] = _cover_path(state, value)
            elif key in ("blur", "dim"):
                try:
                    number = int(value)
                except (TypeError, ValueError) as exc:
                    raise MusicError(400, f"{key} must be a number") from exc
                playlist[key] = max(0, min(100, number))
            elif key == "songs":
                playlist["songs"] = _same_set(playlist["songs"], list(value or []), "playlist")
            else:
                raise MusicError(400, f"Unknown playlist field: {key}")
        _finish(state, lib, playlists=[playlist_id])
        return playlist


async def playlist_add(state, playlist_id: str, ids: list[str], index: int | None = None) -> dict:
    lib = music.library(state)
    async with lib.lock:
        playlist = _playlist(lib, playlist_id)
        position = index
        for song_id in _songs(lib, ids):
            if song_id in playlist["songs"]:
                continue
            _place(playlist["songs"], song_id, position)
            if position is not None:
                position += 1
        _finish(state, lib, playlists=[playlist_id])
        return playlist


async def playlist_remove(state, playlist_id: str, ids: list[str]) -> dict:
    lib = music.library(state)
    async with lib.lock:
        playlist = _playlist(lib, playlist_id)
        drop = set(ids)
        playlist["songs"] = [s for s in playlist["songs"] if s not in drop]
        _finish(state, lib, playlists=[playlist_id])
        return playlist


async def delete_playlist(state, playlist_id: str) -> None:
    lib = music.library(state)
    async with lib.lock:
        _playlist(lib, playlist_id)
        lib.playlists.pop(playlist_id)
        lib.playlist_order = [p for p in lib.playlist_order if p != playlist_id]
        lib._playlist_path(playlist_id).unlink(missing_ok=True)
        _finish(state, lib, root=True)


async def order_playlists(state, ids: list[str]) -> list[str]:
    lib = music.library(state)
    async with lib.lock:
        lib.playlist_order = _same_set(lib.playlist_order, ids, "playlist order")
        _finish(state, lib, root=True)
        return lib.playlist_order


# -- pictures ----------------------------------------------------------------------


async def store_picture(state, data: bytes, mime: str | None) -> str:
    """A picture from the owner's computer: into the vault's attachments, the
    same way album art is kept. Returns its vault path."""
    path = await music._store_art(state, data, mime, "music-cover")
    if path is None:
        raise MusicError(400, "That is not a picture EXSCALIA can keep (JPEG, PNG or WebP)")
    return path


def audio_dir(lib: MusicLibrary, song: dict) -> Path:
    return lib.audio_path(song).parent
