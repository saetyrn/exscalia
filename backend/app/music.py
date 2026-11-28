"""Music: the ninth section. The library, its records, and getting songs in.

The design and its reasons are in MUSIC.md at the repo root. In short:

    vault/music/library.json          {"root": [folder ids]}: top-level folder order
    vault/music/folders/<id>.json     one folder: name, children, songs, cover, ...
    vault/music/songs/<id>.json       one song: details read from its tags
    vault/music/playlists/<id>.json   one playlist: hand-made, any songs
    data/music/audio/<id>/<file>      the audio itself, outside git
    data/music/incoming/              where a file is staged and checked first

``music/`` is an app-managed root like ``paper/``: the note scans skip it
(NoteStore.list_notes / list_folders), and its records are JSON, never
markdown, so the indexer, the semantic index and the desktop manifest never
see a song. The records are still in the vault, so git versions them.

A song's id is the first 16 hex digits of its audio's SHA-256. The same audio
arriving twice is the same song, and is skipped. A song can sit in several
folders; each folder keeps its own order. A song in no folder lands in
Unsorted.

Audio is never deleted automatically: the server holds the only copy and the
owner keeps no backup. A record whose audio has gone is loaded as
``missing`` rather than dropped.

Nothing here touches the network. Writes announce a ``music`` event on the
LiveBus and ask for a commit; the API layer and the inbox ticker call in.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import re
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .vault.internal import atomic_json, iso_utc, read_json, utc_now
from .vault.notes import NoteError

log = logging.getLogger(__name__)

MUSIC_DIR = "music"
UNSORTED_ID = "unsorted"
UNSORTED_NAME = "Unsorted"

# What a browser is told each format is. Every one of these is accepted and
# kept; the ones Chrome cannot play (ALAC, AIFF) get a playable copy in
# Deploy 2.
AUDIO_TYPES = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".aif": "audio/aiff",
    ".aiff": "audio/aiff",
}
# Album art dropped beside the songs: the folder's picked cover.
FOLDER_ART_NAMES = {"cover", "folder", "front", "album", "albumart"}
IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}

CHUNK = 1024 * 1024
MAX_NAME = 200


class MusicError(NoteError):
    pass


def _safe_file_name(name: str) -> str:
    """The original filename, kept for recognisability, made safe to store."""
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    stem = re.sub(r"[\x00-\x1f/\\:*?\"<>|]+", " ", stem).strip(" .") or "audio"
    stem = stem.encode("utf-8")[:MAX_NAME].decode("utf-8", "ignore").strip() or "audio"
    return f"{stem}.{ext.lower()}" if ext else stem


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        value = value[0] if value else None
        if value is None:
            return None
    text = str(value).replace("\x00", " ").strip()
    return text[:300] or None


def _number(value: Any) -> int | None:
    """'3', '3/12' and ['3'] all mean 3."""
    text = _clean(value)
    if not text:
        return None
    match = re.match(r"\s*(\d+)", text)
    return int(match.group(1)) if match else None


@dataclass
class Tags:
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    year: int | None = None
    genre: str | None = None
    track: int | None = None
    disc: int | None = None
    duration: float | None = None
    art: bytes | None = None
    art_type: str | None = None


def read_tags(path: Path) -> Tags:
    """Everything mutagen can tell about one file. Never raises: a file with
    unreadable tags is still a song, named after its file."""
    import mutagen

    tags = Tags()
    try:
        audio = mutagen.File(path)
    except Exception:
        log.info("unreadable tags in %s", path.name)
        return tags
    if audio is None:
        return tags
    length = getattr(getattr(audio, "info", None), "length", None)
    tags.duration = round(float(length), 3) if length else None
    raw = getattr(audio, "tags", None)
    if raw is not None and hasattr(raw, "getall"):
        # ID3 (MP3, and the ID3 chunk inside WAV and AIFF): read the frames,
        # since mutagen's "easy" mapping does not cover WAV and AIFF.
        def get(*frames):
            for frame in frames:
                found = raw.getall(frame)
                if found and getattr(found[0], "text", None):
                    return found[0].text
            return None
    else:
        values = raw or {}

        def get(*keys):
            for key in keys:
                try:
                    value = values.get(key)
                except Exception:
                    value = None
                if value:
                    return value
            return None

        try:
            easy = mutagen.File(path, easy=True)
            if easy is not None and easy.tags is not None:
                values = easy.tags
        except Exception:
            pass
    try:
        tags.title = _clean(get("TIT2", "title"))
        tags.artist = _clean(get("TPE1", "TPE2", "artist", "albumartist"))
        tags.album = _clean(get("TALB", "album"))
        tags.year = _number(get("TDRC", "TYER", "date", "year"))
        tags.genre = _clean(get("TCON", "genre"))
        tags.track = _number(get("TRCK", "tracknumber"))
        tags.disc = _number(get("TPOS", "discnumber"))
    except Exception:
        log.info("unreadable tags in %s", path.name)
    try:
        tags.art, tags.art_type = _embedded_art(audio)
    except Exception:
        log.info("unreadable artwork in %s", path.name)
    return tags


def _embedded_art(audio) -> tuple[bytes | None, str | None]:
    if audio is None:
        return None, None
    pictures = getattr(audio, "pictures", None)  # FLAC
    if pictures:
        picture = _front(pictures, lambda p: p.type)
        return picture.data, picture.mime
    tags = getattr(audio, "tags", None)
    if tags is None:
        return None, None
    if hasattr(tags, "getall"):  # ID3: MP3, AIFF, WAV
        frames = tags.getall("APIC")
        if frames:
            frame = _front(frames, lambda f: f.type)
            return frame.data, frame.mime
        return None, None
    covers = tags.get("covr") if hasattr(tags, "get") else None  # MP4
    if covers:
        cover = covers[0]
        kind = "image/png" if getattr(cover, "imageformat", None) == 14 else "image/jpeg"
        return bytes(cover), kind
    blocks = tags.get("metadata_block_picture") if hasattr(tags, "get") else None  # Ogg
    if blocks:
        from mutagen.flac import Picture

        pictures = []
        for block in blocks:
            try:
                pictures.append(Picture(base64.b64decode(block)))
            except Exception:
                continue
        if pictures:
            picture = _front(pictures, lambda p: p.type)
            return picture.data, picture.mime
    return None, None


def _front(items, type_of):
    """The front cover (picture type 3) when there is one, else the first."""
    for item in items:
        if type_of(item) == 3:
            return item
    return items[0]


def _sniff_image(data: bytes, hint: str | None) -> str | None:
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return hint if hint in IMAGE_TYPES.values() else None


# -- the library ----------------------------------------------------------------


def _new_playlist(name: str, playlist_id: str | None = None) -> dict:
    return {
        "id": playlist_id or uuid.uuid4().hex[:12],
        "name": name,
        "songs": [],
        "cover": None,
        "background": None,
        "blur": 24,
        "dim": 45,
        "created": iso_utc(utc_now()),
    }


def _new_folder(name: str, folder_id: str | None = None) -> dict:
    return {
        "id": folder_id or uuid.uuid4().hex[:12],
        "name": name,
        "children": [],
        "songs": [],
        "liked": [],
        "cover": None,
        "colour": None,
        "glyph": None,
        "pinned": False,
        "created": iso_utc(utc_now()),
    }


class MusicLibrary:
    """The whole library in memory. A thousand songs is a few hundred KB, and
    twenty thousand is still small, so every read is a dict lookup and every
    write is one or two small JSON files."""

    def __init__(self, vault_dir: Path, data_dir: Path) -> None:
        self.root_dir = vault_dir / MUSIC_DIR
        self.audio_dir = data_dir / "music" / "audio"
        self.staging_dir = data_dir / "music" / "incoming"
        self.songs: dict[str, dict] = {}
        self.folders: dict[str, dict] = {}
        self.playlists: dict[str, dict] = {}
        self.root: list[str] = []
        # Liked, newest first unless reordered by hand; playlist order.
        self.liked: list[str] = []
        self.playlist_order: list[str] = []
        self.revision = 0
        self.lock = asyncio.Lock()

    # -- paths --

    def _song_path(self, song_id: str) -> Path:
        return self.root_dir / "songs" / f"{song_id}.json"

    def _folder_path(self, folder_id: str) -> Path:
        return self.root_dir / "folders" / f"{folder_id}.json"

    def _playlist_path(self, playlist_id: str) -> Path:
        return self.root_dir / "playlists" / f"{playlist_id}.json"

    def audio_path(self, song: dict) -> Path:
        return self.audio_dir / song["id"] / song["file"]

    # -- load --

    def load(self) -> None:
        """Read every record. A corrupt or half-written record is set aside by
        read_json and skipped; boot never stops on bad data."""
        self.songs.clear()
        self.folders.clear()
        for path in sorted((self.root_dir / "songs").glob("*.json")):
            song = read_json(path, None, skip_corrupt=True)
            if isinstance(song, dict) and song.get("id") == path.stem and song.get("file"):
                song["missing"] = not self.audio_path(song).is_file()
                self.songs[song["id"]] = song
        for path in sorted((self.root_dir / "folders").glob("*.json")):
            folder = read_json(path, None, skip_corrupt=True)
            if isinstance(folder, dict) and folder.get("id") == path.stem:
                for key, default in _new_folder("").items():
                    folder.setdefault(key, default)
                self.folders[folder["id"]] = folder
        self.playlists.clear()
        for path in sorted((self.root_dir / "playlists").glob("*.json")):
            playlist = read_json(path, None, skip_corrupt=True)
            if isinstance(playlist, dict) and playlist.get("id") == path.stem:
                for key, default in _new_playlist("").items():
                    playlist.setdefault(key, default)
                self.playlists[playlist["id"]] = playlist
        index = read_json(self.root_dir / "library.json", {}, skip_corrupt=True)
        if not isinstance(index, dict):
            index = {}
        root = index.get("root", [])
        self.liked = [s for s in dict.fromkeys(index.get("liked", [])) if isinstance(s, str)]
        self.playlist_order = [p for p in dict.fromkeys(index.get("playlists", [])) if isinstance(p, str)]
        self._repair(root if isinstance(root, list) else [])
        missing = sum(1 for s in self.songs.values() if s["missing"])
        if self.songs or self.folders:
            log.info(
                "music: %d songs, %d folders%s",
                len(self.songs),
                len(self.folders),
                f", {missing} with audio missing" if missing else "",
            )

    def _repair(self, root: list[str]) -> None:
        """Drop ids that point nowhere, give every folder one parent, and put
        orphaned folders back at the top level. In memory only: the next write
        to a record saves its repaired form."""
        for folder in self.folders.values():
            folder["songs"] = list(dict.fromkeys(s for s in folder["songs"] if s in self.songs))
            folder["liked"] = [s for s in dict.fromkeys(folder["liked"]) if s in folder["songs"]]
        seen: set[str] = set()

        def claim(ids: list[str]) -> list[str]:
            kept = []
            for folder_id in ids:
                if folder_id in self.folders and folder_id not in seen:
                    seen.add(folder_id)
                    kept.append(folder_id)
            return kept

        self.root = claim(root)
        queue = list(self.root)
        while queue:
            folder = self.folders[queue.pop()]
            folder["children"] = claim(folder["children"])
            queue.extend(folder["children"])
        for folder_id in self.folders:
            if folder_id not in seen:
                seen.add(folder_id)
                self.root.append(folder_id)
        for playlist in self.playlists.values():
            playlist["songs"] = [s for s in playlist["songs"] if s in self.songs]
        self.playlist_order = [p for p in self.playlist_order if p in self.playlists]
        self.playlist_order += [p for p in self.playlists if p not in self.playlist_order]
        liked = {s for s, song in self.songs.items() if song.get("liked") and not song.get("trashed")}
        self.liked = [s for s in self.liked if s in liked]
        self.liked += [s for s in liked if s not in self.liked]
        homed = {s for f in self.folders.values() for s in f["songs"]}
        stray = [s for s, song in self.songs.items() if s not in homed and not song.get("trashed")]
        if stray:
            unsorted = self._unsorted()
            unsorted["songs"].extend(stray)

    # -- saving --

    def _save_song(self, song: dict) -> None:
        record = {k: v for k, v in song.items() if k != "missing"}
        atomic_json(self._song_path(song["id"]), record)

    def _save_folder(self, folder: dict) -> None:
        atomic_json(self._folder_path(folder["id"]), folder)

    def _save_playlist(self, playlist: dict) -> None:
        atomic_json(self._playlist_path(playlist["id"]), playlist)

    def _save_root(self) -> None:
        atomic_json(
            self.root_dir / "library.json",
            {"root": self.root, "liked": self.liked, "playlists": self.playlist_order},
        )

    # -- reads --

    def parents(self) -> dict[str, str | None]:
        out: dict[str, str | None] = {f: None for f in self.root}
        for folder in self.folders.values():
            for child in folder["children"]:
                out[child] = folder["id"]
        return out

    def snapshot(self) -> dict:
        return {
            "revision": self.revision,
            "root": list(self.root),
            "liked": list(self.liked),
            "playlist_order": list(self.playlist_order),
            "folders": list(self.folders.values()),
            "playlists": list(self.playlists.values()),
            "songs": list(self.songs.values()),
        }

    def locate(self, song_id: str) -> list[str]:
        """Names of the folders a song is in, for "already in …" notes."""
        return [f["name"] for f in self.folders.values() if song_id in f["songs"]]

    # -- folders --

    def _unsorted(self) -> dict:
        folder = self.folders.get(UNSORTED_ID)
        if folder is None:
            folder = _new_folder(UNSORTED_NAME, UNSORTED_ID)
            self.folders[UNSORTED_ID] = folder
            self.root.append(UNSORTED_ID)
        return folder

    def child_named(self, parent_id: str | None, name: str) -> dict:
        """The folder called ``name`` under ``parent_id`` (None = top level),
        created when there is none. Matched without regard to case, since the
        owner's Mac does not tell "Rap" from "rap" either."""
        name = name.strip() or UNSORTED_NAME
        siblings = self.root if parent_id is None else self.folders[parent_id]["children"]
        for folder_id in siblings:
            if self.folders[folder_id]["name"].casefold() == name.casefold():
                return self.folders[folder_id]
        folder = _new_folder(name)
        self.folders[folder["id"]] = folder
        siblings.append(folder["id"])
        return folder

    def folder_for_parts(
        self, parts: tuple[str, ...], parent: str | None = None
    ) -> tuple[dict, list[dict]]:
        """The folder at a path of names under ``parent`` (None = the top),
        creating what is missing. Returns it and every folder that changed (so
        the caller saves them)."""
        if parent is not None and parent not in self.folders:
            parent = None
        if not parts:
            if parent is not None:
                return self.folders[parent], []
            return self._unsorted(), []
        before = {fid: len(f["children"]) for fid, f in self.folders.items()}
        folder = None
        for name in parts:
            folder = self.child_named(parent, name)
            parent = folder["id"]
        changed = [
            f for fid, f in self.folders.items()
            if fid not in before or len(f["children"]) != before[fid]
        ]
        return folder, changed

    def shared_cover(self, folder: dict) -> str | None:
        """The cover every song in the folder shares, if they all share one."""
        covers = {self.songs[s].get("cover") for s in folder["songs"] if s in self.songs}
        if len(covers) == 1:
            return next(iter(covers))
        return None


def library(state) -> MusicLibrary:
    """The app's library, loaded on first use (tests build state without the
    lifespan, and boot loads it explicitly)."""
    lib = getattr(state, "music", None)
    if lib is None:
        lib = MusicLibrary(state.settings.vault_dir, state.settings.data_dir)
        lib.load()
        state.music = lib
    return lib


def changed(state, lib: MusicLibrary) -> None:
    lib.revision += 1
    state.autocommit.touch()
    state.live.publish({"type": "music", "path": MUSIC_DIR, "revision": lib.revision})


# -- importing ------------------------------------------------------------------


@dataclass
class ImportItem:
    source: Path
    parts: tuple[str, ...]  # folder names, outermost first
    name: str  # the file's original name
    remove_source: bool = False
    # The folder ``parts`` are under (None = the top level).
    parent: str | None = None


@dataclass
class ImportReport:
    added: list[str] = field(default_factory=list)
    duplicates: list[dict] = field(default_factory=list)
    failed: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"added": self.added, "duplicates": self.duplicates, "failed": self.failed}


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _stage(source: Path, staging_dir: Path) -> tuple[Path, str]:
    """Copy ``source`` into staging while hashing it, then hash the copy. A
    mismatch means the copy is bad (or the source changed under us); the
    source is left alone and the copy thrown away."""
    staging_dir.mkdir(parents=True, exist_ok=True)
    staged = staging_dir / f"{uuid.uuid4().hex}.part"
    digest = hashlib.sha256()
    try:
        with source.open("rb") as src, staged.open("wb") as dst:
            while chunk := src.read(CHUNK):
                digest.update(chunk)
                dst.write(chunk)
            dst.flush()
            os.fsync(dst.fileno())
        expected = digest.hexdigest()
        if _hash_file(staged) != expected:
            raise MusicError(500, "the copy did not match the original")
        return staged, expected
    except BaseException:
        staged.unlink(missing_ok=True)
        raise


async def _store_art(state, data: bytes, mime: str | None, hint: str) -> str | None:
    from . import media_lookup

    kind = _sniff_image(data, mime)
    if kind is None:
        return None
    try:
        stored = await media_lookup.store_image_bytes(state, data, kind, hint, kind="cover")
    except Exception:
        log.info("could not keep artwork for %s", hint, exc_info=True)
        return None
    return stored["path"]


def _remove_source(item: ImportItem) -> None:
    if item.remove_source:
        try:
            item.source.unlink()
        except OSError:
            log.warning("could not remove %s from the drop folder", item.name)


async def import_files(
    state,
    items: list[ImportItem],
    folder_art: dict[tuple[str, ...], Path] | None = None,
) -> ImportReport:
    """Import audio files into the library, grouped by folder. Within a folder
    an album keeps its order: disc, then track number, then filename."""
    lib = library(state)
    report = ImportReport()
    folder_art = folder_art or {}
    groups: dict[tuple[str | None, tuple[str, ...]], list[ImportItem]] = {}
    for item in items:
        groups.setdefault((item.parent, item.parts), []).append(item)

    for (parent, parts), group in groups.items():
        staged: list[tuple[ImportItem, Path, str, Tags]] = []
        for item in group:
            try:
                path, digest = await asyncio.to_thread(_stage, item.source, lib.staging_dir)
            except Exception as exc:
                log.warning("music import of %s failed: %s", item.name, exc)
                report.failed.append({"name": item.name, "error": str(exc) or type(exc).__name__})
                continue
            song_id = digest[:16]
            if song_id in lib.songs:
                path.unlink(missing_ok=True)
                _remove_source(item)
                report.duplicates.append({"name": item.name, "in": lib.locate(song_id)})
                continue
            tags = await asyncio.to_thread(read_tags, path)
            staged.append((item, path, digest, tags))
        staged.sort(key=lambda s: (s[3].disc or 0, s[3].track or 10_000, s[0].name.casefold()))
        if not staged:
            continue

        async with lib.lock:
            root_before = list(lib.root)
            folder, touched = lib.folder_for_parts(parts, parent)
            touched_ids = {f["id"] for f in touched} | {folder["id"]}
            art_file = folder_art.get(parts)
            if art_file is not None and not folder.get("cover"):
                try:
                    data = await asyncio.to_thread(art_file.read_bytes)
                    folder["cover"] = await _store_art(
                        state, data, IMAGE_TYPES.get(art_file.suffix.lower()), "music-cover"
                    )
                except OSError:
                    log.info("could not read folder art %s", art_file.name)
            for item, path, digest, tags in staged:
                song_id = digest[:16]
                if song_id in lib.songs:  # two copies in one drop
                    path.unlink(missing_ok=True)
                    _remove_source(item)
                    report.duplicates.append({"name": item.name, "in": lib.locate(song_id)})
                    continue
                file_name = _safe_file_name(item.name)
                target = lib.audio_dir / song_id / file_name
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(path, target)
                except OSError as exc:
                    path.unlink(missing_ok=True)
                    report.failed.append({"name": item.name, "error": str(exc)})
                    continue
                cover = None
                if tags.art:
                    # One fixed name hint: the same art on twelve songs is
                    # then one file, which is what "shared cover" compares.
                    cover = await _store_art(state, tags.art, tags.art_type, "music-cover")
                if cover is None and folder["songs"]:
                    # Every song here shares one cover: the newcomer adopts it.
                    cover = lib.shared_cover(folder)
                song = {
                    "id": song_id,
                    "digest": digest,
                    "file": file_name,
                    "bytes": target.stat().st_size,
                    "mime": AUDIO_TYPES.get(Path(file_name).suffix.lower(), "application/octet-stream"),
                    "title": tags.title or Path(item.name).stem,
                    "artist": tags.artist,
                    "album": tags.album,
                    "year": tags.year,
                    "genre": tags.genre,
                    "track": tags.track,
                    "disc": tags.disc,
                    "duration": tags.duration,
                    "cover": cover,
                    "liked": False,
                    "added": iso_utc(utc_now()),
                    "manual": [],
                    "missing": False,
                }
                lib._save_song(song)
                lib.songs[song_id] = song
                folder["songs"].append(song_id)
                report.added.append(song_id)
                _remove_source(item)
            # An album whose songs all carry the same art: that art is the
            # folder's picked cover (the owner's rule, MUSIC.md section 3).
            # One song is not an album: a lone single keeps a plain folder.
            if not folder.get("cover") and len(folder["songs"]) >= 2:
                common = lib.shared_cover(folder)
                if common:
                    folder["cover"] = common
            for folder_id in touched_ids:
                lib._save_folder(lib.folders[folder_id])
            if lib.root != root_before:
                lib._save_root()
            changed(state, lib)
    if report.added:
        from . import music_convert

        music_convert.convert_soon(state, report.added)
    return report


def clear_staging(lib: MusicLibrary) -> None:
    """Half-staged copies left by a crash. Their sources are still in place."""
    if lib.staging_dir.is_dir():
        shutil.rmtree(lib.staging_dir, ignore_errors=True)
