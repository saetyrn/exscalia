"""Music, Deploy 1: the library records, the drop folder, streaming."""
import asyncio
import json
import os
import struct
import wave
from pathlib import Path

from mutagen.id3 import APIC, TALB, TIT2, TPE1, TRCK
from mutagen.wave import WAVE

from app import music, music_inbox
from app.api import music as music_api
from app.vault.notes import NoteError

from conftest import make_client

# A real 1x1 PNG, so the stored cover survives media_lookup's checks.
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
)


def run(coro):
    return asyncio.run(coro)


def make_wav(path: Path, *, frames: int = 800, seed: int = 0, title=None, artist=None,
             album=None, track=None, art: bytes | None = None) -> Path:
    """A short silent WAV, made unique by `seed`, with optional ID3 tags."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(8000)
        out.writeframes(struct.pack("<h", seed) + b"\x00\x00" * (frames - 1))
    if any(v is not None for v in (title, artist, album, track, art)):
        audio = WAVE(str(path))
        audio.add_tags()
        if title:
            audio.tags.add(TIT2(encoding=3, text=title))
        if artist:
            audio.tags.add(TPE1(encoding=3, text=artist))
        if album:
            audio.tags.add(TALB(encoding=3, text=album))
        if track:
            audio.tags.add(TRCK(encoding=3, text=str(track)))
        if art:
            audio.tags.add(APIC(encoding=3, mime="image/png", type=3, desc="", data=art))
        audio.save()
    return path


def inbox(state) -> Path:
    return music_inbox.inbox_dir(state)


def scan_twice(state) -> dict:
    """A file is taken once it has held still across two scans."""
    first = run(music_inbox.scan(state))
    assert first.get("added", []) == []
    return run(music_inbox.scan(state))


def folder_named(lib, name, parent=None):
    ids = lib.root if parent is None else lib.folders[parent]["children"]
    return next(lib.folders[i] for i in ids if lib.folders[i]["name"] == name)


# -- the drop folder ----------------------------------------------------------------


def test_drop_folder_imports_an_album_in_track_order(state):
    root = inbox(state)
    make_wav(root / "Lil Uzi" / "Eternal" / "b.wav", seed=2, title="Two", artist="Lil Uzi Vert", track=2)
    make_wav(root / "Lil Uzi" / "Eternal" / "a.wav", seed=3, title="Three", artist="Lil Uzi Vert", track=3)
    make_wav(root / "Lil Uzi" / "Eternal" / "c.wav", seed=1, title="One", artist="Lil Uzi Vert", track=1)
    queue = state.live.subscribe()

    report = scan_twice(state)

    assert len(report["added"]) == 3
    lib = music.library(state)
    artist = folder_named(lib, "Lil Uzi")
    album = folder_named(lib, "Eternal", artist["id"])
    assert artist["children"] == [album["id"]]
    assert [lib.songs[s]["title"] for s in album["songs"]] == ["One", "Two", "Three"]
    assert all(lib.songs[s]["artist"] == "Lil Uzi Vert" for s in album["songs"])
    # Sources are removed once copied, and the emptied folders with them.
    assert list(root.iterdir()) == []
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    assert any(e["type"] == "music" for e in events)


def test_a_file_still_being_copied_waits(state):
    root = inbox(state)
    path = make_wav(root / "song.wav", seed=1)
    run(music_inbox.scan(state))
    # It grew between scans: still copying.
    with path.open("ab") as handle:
        handle.write(b"\x00" * 64)
    second = run(music_inbox.scan(state))
    assert second["added"] == [] and second["waiting"] == 1
    third = run(music_inbox.scan(state))
    assert len(third["added"]) == 1


def test_loose_files_land_in_unsorted_and_hidden_files_are_ignored(state):
    root = inbox(state)
    make_wav(root / "loose.wav", seed=5)
    make_wav(root / ".partial.wav", seed=6)
    make_wav(root / "._loose.wav", seed=7)

    report = scan_twice(state)

    lib = music.library(state)
    assert len(report["added"]) == 1
    unsorted = lib.folders[music.UNSORTED_ID]
    assert lib.songs[unsorted["songs"][0]]["title"] == "loose"
    assert (root / ".partial.wav").exists()


def test_the_same_audio_twice_is_skipped_with_where_it_lives(state):
    root = inbox(state)
    make_wav(root / "Rap" / "x.wav", seed=9)
    scan_twice(state)
    make_wav(root / "Other" / "copy of x.wav", seed=9)

    report = scan_twice(state)

    assert report["added"] == []
    assert report["duplicates"] == [{"name": "copy of x.wav", "in": ["Rap"]}]
    assert not (root / "Other").exists()


def test_embedded_art_becomes_the_song_cover_and_the_album_cover(state):
    root = inbox(state)
    make_wav(root / "Album" / "1.wav", seed=1, track=1, art=PNG)
    make_wav(root / "Album" / "2.wav", seed=2, track=2, art=PNG)

    scan_twice(state)

    lib = music.library(state)
    album = folder_named(lib, "Album")
    covers = {lib.songs[s]["cover"] for s in album["songs"]}
    assert len(covers) == 1 and next(iter(covers)).startswith("attachments/")
    assert album["cover"] == next(iter(covers))


def test_a_song_without_art_adopts_the_folder_shared_cover(state):
    root = inbox(state)
    make_wav(root / "Album" / "1.wav", seed=1, track=1, art=PNG)
    scan_twice(state)
    make_wav(root / "Album" / "bonus.wav", seed=4)
    scan_twice(state)

    lib = music.library(state)
    album = folder_named(lib, "Album")
    first, bonus = (lib.songs[s] for s in album["songs"])
    assert bonus["cover"] == first["cover"] is not None


def test_one_song_is_not_an_album(state):
    make_wav(inbox(state) / "Singles" / "1.wav", seed=1, art=PNG)
    scan_twice(state)

    lib = music.library(state)
    singles = folder_named(lib, "Singles")
    assert lib.songs[singles["songs"][0]]["cover"] and singles["cover"] is None


def test_folder_art_file_is_the_picked_cover(state):
    root = inbox(state)
    make_wav(root / "Mixtape" / "1.wav", seed=1)
    (root / "Mixtape" / "cover.png").write_bytes(PNG)

    scan_twice(state)

    lib = music.library(state)
    assert folder_named(lib, "Mixtape")["cover"].startswith("attachments/")
    assert not (root / "Mixtape").exists()


def test_a_file_that_cannot_be_removed_is_not_offered_again(state, monkeypatch):
    path = make_wav(inbox(state) / "Rap" / "a.wav", seed=1)
    monkeypatch.setattr(music, "_remove_source", lambda item: None)

    assert len(scan_twice(state)["added"]) == 1
    assert path.exists()
    for _ in range(3):
        again = run(music_inbox.scan(state))
        assert again.get("duplicates", []) == [] and again.get("waiting", 0) == 0


def test_a_missing_drop_folder_is_reported_not_fatal(state, tmp_path):
    state.settings.music_inbox = tmp_path / "nowhere"
    assert run(music_inbox.scan(state)) == {"inbox": str(tmp_path / "nowhere"), "present": False}


# -- records ----------------------------------------------------------------------


def test_records_reload_and_bad_data_never_stops_the_load(state):
    root = inbox(state)
    make_wav(root / "Rap" / "a.wav", seed=1, title="A")
    make_wav(root / "Rap" / "b.wav", seed=2, title="B")
    scan_twice(state)
    lib = music.library(state)
    rap = folder_named(lib, "Rap")
    a, b = rap["songs"]
    # One corrupt song record, one song whose audio vanished.
    (lib.root_dir / "songs" / f"{a}.json").write_text("{not json")
    os.remove(lib.audio_path(lib.songs[b]))

    fresh = music.MusicLibrary(state.settings.vault_dir, state.settings.data_dir)
    fresh.load()

    assert a not in fresh.songs
    assert fresh.songs[b]["missing"] is True
    assert fresh.folders[rap["id"]]["songs"] == [b]


def test_a_folder_lost_from_the_root_order_comes_back(state):
    make_wav(inbox(state) / "Rap" / "a.wav", seed=1)
    scan_twice(state)
    lib = music.library(state)
    (lib.root_dir / "library.json").write_text(json.dumps({"root": []}))

    fresh = music.MusicLibrary(state.settings.vault_dir, state.settings.data_dir)
    fresh.load()

    assert [fresh.folders[f]["name"] for f in fresh.root] == ["Rap"]


def test_music_records_are_not_notes(state):
    make_wav(inbox(state) / "Rap" / "a.wav", seed=1)
    scan_twice(state)

    assert not any(n.path.startswith("music/") for n in state.store.list_notes())
    assert not any(f.startswith("music") for f in state.store.list_folders())
    try:
        state.store.create("music/sneaky.md", "# no")
    except NoteError as exc:
        assert exc.status == 400
    else:
        raise AssertionError("a note was written into music/")


def test_a_crash_mid_copy_leaves_the_source(state, monkeypatch):
    path = make_wav(inbox(state) / "a.wav", seed=1)
    real = music._hash_file
    monkeypatch.setattr(music, "_hash_file", lambda p: "0" * 64)

    report = scan_twice(state)

    assert report["failed"] and path.exists()
    assert not any(music.library(state).staging_dir.glob("*.part"))
    monkeypatch.setattr(music, "_hash_file", real)


# -- the API ------------------------------------------------------------------------


def test_library_and_stream_with_range(state):
    make_wav(inbox(state) / "Rap" / "a.wav", seed=1, title="A", frames=4000)
    scan_twice(state)
    client = make_client(state, music_api.router)

    library = client.get("/api/music/library").json()
    assert [f["name"] for f in library["folders"]] == ["Rap"]
    song = library["songs"][0]
    assert song["title"] == "A" and song["mime"] == "audio/wav"

    whole = client.get(f"/api/music/stream/{song['id']}")
    assert whole.status_code == 200
    assert whole.headers["accept-ranges"] == "bytes"
    assert whole.headers["cache-control"].startswith("private")
    assert whole.headers["content-disposition"].startswith("inline")
    part = client.get(f"/api/music/stream/{song['id']}", headers={"Range": "bytes=100-199"})
    assert part.status_code == 206 and len(part.content) == 100
    assert part.content == whole.content[100:200]


def test_stream_of_unknown_or_missing_audio_is_404(state):
    make_wav(inbox(state) / "a.wav", seed=1)
    scan_twice(state)
    lib = music.library(state)
    song = next(iter(lib.songs.values()))
    os.remove(lib.audio_path(song))
    client = make_client(state, music_api.router)

    assert client.get("/api/music/stream/nope").status_code == 404
    assert client.get(f"/api/music/stream/{song['id']}").status_code == 404
    assert client.get("/api/music/library").json()["songs"][0]["missing"] is True
