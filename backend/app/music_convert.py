"""Music: playable copies made with ffmpeg, in the background.

Lossless and Opus files get a 256 kbps constant-bitrate MP3, which is what
streams by default: a FLAC is about four times its size, and every byte of a
stream crosses the Cloudflare Tunnel (MUSIC.md section 2). Constant bitrate
keeps a browser's seek exact. WAV, AIFF and ALAC also get a FLAC copy for the
per-device Lossless switch, since Chrome plays neither AIFF nor ALAC and WAV
is twice the size of FLAC. MP3 and AAC originals are already small and
playable everywhere; they are left alone.

One file at a time, at low priority, writing under /data (the container's
/tmp is memory). A failure is logged and remembered in memory; the song
still streams its original. Boot queues any song missing its copies.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import uuid
from pathlib import Path

from . import music

log = logging.getLogger(__name__)

STREAM_FILE = "stream.mp3"
LOSSLESS_FILE = "lossless.flac"
TIMEOUT_SECONDS = 15 * 60
MP3_ARGS = ["-c:a", "libmp3lame", "-b:a", "256k", "-id3v2_version", "3"]
FLAC_ARGS = ["-c:a", "flac", "-compression_level", "5"]

LOSSLESS = {".flac", ".wav", ".aif", ".aiff"}
NEEDS_FLAC = {".wav", ".aif", ".aiff"}
NEEDS_MP3 = LOSSLESS | {".ogg", ".opus"}


def _is_alac(path: Path) -> bool:
    if path.suffix.lower() != ".m4a":
        return False
    try:
        import mutagen

        audio = mutagen.File(path)
        return getattr(getattr(audio, "info", None), "codec", "") == "alac"
    except Exception:
        return False


def wanted(lib: music.MusicLibrary, song: dict) -> list[tuple[str, list[str]]]:
    """The copies this song should have and does not."""
    path = lib.audio_path(song)
    suffix = path.suffix.lower()
    alac = _is_alac(path)
    out = []
    if (suffix in NEEDS_MP3 or alac) and not song.get("stream"):
        out.append((STREAM_FILE, MP3_ARGS))
    if (suffix in NEEDS_FLAC or alac) and not song.get("lossless"):
        out.append((LOSSLESS_FILE, FLAC_ARGS))
    return out


class Converter:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.pending: set[str] = set()
        self.errors: dict[str, str] = {}
        self.task: asyncio.Task | None = None
        self.warned = False


def _converter(state) -> Converter:
    conv = getattr(state, "music_converter", None)
    if conv is None:
        conv = Converter()
        state.music_converter = conv
    return conv


def ffmpeg_path(state) -> str | None:
    configured = getattr(state.settings, "music_ffmpeg", None) or "ffmpeg"
    return shutil.which(configured) or (configured if Path(configured).is_file() else None)


def convert_soon(state, song_ids: list[str]) -> None:
    conv = _converter(state)
    for song_id in song_ids:
        if song_id not in conv.pending:
            conv.pending.add(song_id)
            conv.queue.put_nowait(song_id)
    if conv.task is None or conv.task.done():
        conv.task = asyncio.create_task(_worker(state, conv))


def backfill(state) -> int:
    lib = music.library(state)
    todo = [
        s["id"] for s in lib.songs.values()
        if not s.get("missing") and not s.get("trashed") and wanted(lib, s)
    ]
    if todo:
        convert_soon(state, todo)
    return len(todo)


def status(state) -> dict:
    conv = _converter(state)
    return {"waiting": len(conv.pending), "errors": dict(conv.errors)}


async def _worker(state, conv: Converter) -> None:
    while not conv.queue.empty():
        song_id = await conv.queue.get()
        try:
            await convert_one(state, song_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            conv.errors[song_id] = str(exc) or type(exc).__name__
            log.exception("music conversion of %s failed", song_id)
        finally:
            conv.pending.discard(song_id)


async def _run(command: list[str]) -> None:
    process = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    try:
        _out, err = await asyncio.wait_for(process.communicate(), TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise RuntimeError("ffmpeg took too long") from None
    if process.returncode != 0:
        tail = (err or b"").decode("utf-8", "replace").strip().splitlines()[-1:] or ["no output"]
        raise RuntimeError(f"ffmpeg failed: {tail[0][:200]}")


async def convert_one(state, song_id: str) -> list[str]:
    lib = music.library(state)
    song = lib.songs.get(song_id)
    if song is None or song.get("trashed"):
        return []
    source = lib.audio_path(song)
    if not source.is_file():
        return []
    jobs = await asyncio.to_thread(wanted, lib, song)
    if not jobs:
        return []
    ffmpeg = ffmpeg_path(state)
    conv = _converter(state)
    if ffmpeg is None:
        if not conv.warned:
            log.warning("music: ffmpeg not found; songs stream their originals")
            conv.warned = True
        return []
    nice = ["nice", "-n", "10"] if shutil.which("nice") else []
    made = []
    for name, args in jobs:
        lib.staging_dir.mkdir(parents=True, exist_ok=True)
        temp = lib.staging_dir / f"{uuid.uuid4().hex}{Path(name).suffix}"
        try:
            await _run([
                *nice, ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-y",
                "-i", str(source), "-map", "0:a:0", "-vn", *args, str(temp),
            ])
            target = source.parent / name
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)
        made.append((name, target.stat().st_size))
    async with lib.lock:
        song = lib.songs.get(song_id)
        if song is None:
            return []
        for name, size in made:
            key = "stream" if name == STREAM_FILE else "lossless"
            song[key] = {"file": name, "bytes": size}
        conv.errors.pop(song_id, None)
        lib._save_song(song)
        music.changed(state, lib)
    return [name for name, _ in made]


def pick_file(lib: music.MusicLibrary, song: dict, lossless: bool) -> tuple[Path, str]:
    """What a stream request gets: the MP3 copy, or with Lossless on the FLAC
    copy, falling back to the original whenever a copy is missing."""
    folder = lib.audio_path(song).parent
    if lossless:
        copy = song.get("lossless")
        if copy and (folder / copy["file"]).is_file():
            return folder / copy["file"], "audio/flac"
        return lib.audio_path(song), song.get("mime") or "application/octet-stream"
    copy = song.get("stream")
    if copy and (folder / copy["file"]).is_file():
        return folder / copy["file"], "audio/mpeg"
    return lib.audio_path(song), song.get("mime") or "application/octet-stream"


async def shutdown(state) -> None:
    conv = getattr(state, "music_converter", None)
    if conv and conv.task and not conv.task.done():
        conv.task.cancel()
