"""Music, Deploy 2: edits, likes, trash, playlists, uploads, conversion."""
import asyncio
import os
import shutil

import pytest

from app import music, music_convert, music_edit, music_upload
from app.api import music as music_api
from app.music import MusicError

from conftest import make_client
from test_music import PNG, folder_named, inbox, make_wav, scan_twice


def run(coro):
    return asyncio.run(coro)


def seed(state):
    """Rap/a, Rap/b, Rap/c and Pop/d."""
    root = inbox(state)
    for i, name in enumerate("abc", 1):
        make_wav(root / "Rap" / f"{name}.wav", seed=i, title=name.upper(), track=i)
    make_wav(root / "Pop" / "d.wav", seed=9, title="D")
    scan_twice(state)
    lib = music.library(state)
    rap, pop = folder_named(lib, "Rap"), folder_named(lib, "Pop")
    return lib, rap, pop


def titles(lib, ids):
    return [lib.songs[s]["title"] for s in ids]


# -- songs and likes ---------------------------------------------------------------


def test_edit_marks_fields_as_hand_set(state):
    lib, rap, _ = seed(state)
    a = rap["songs"][0]
    song = run(music_edit.edit_song(state, a, {"artist": "Lil Uzi Vert", "year": "2020"}))
    assert song["artist"] == "Lil Uzi Vert" and song["year"] == 2020
    assert song["manual"] == ["artist", "year"]
    with pytest.raises(MusicError):
        run(music_edit.edit_song(state, a, {"title": ""}))
    with pytest.raises(MusicError):
        run(music_edit.edit_song(state, a, {"cover": "attachments/nope.png"}))


def test_like_goes_first_everywhere_and_unlike_leaves_the_order(state):
    lib, rap, pop = seed(state)
    a, b, c = rap["songs"]
    run(music_edit.move_songs(state, [c], pop["id"], copy=True))
    run(music_edit.set_liked(state, b, True))
    run(music_edit.set_liked(state, c, True))
    assert lib.liked == [c, b]
    assert rap["liked"] == [c, b] and pop["liked"] == [c]
    assert rap["songs"] == [a, b, c]  # still in its regular place
    run(music_edit.set_liked(state, c, False))
    assert lib.liked == [b] and rap["liked"] == [b] and pop["liked"] == []


def test_order_must_be_the_same_songs(state):
    lib, rap, _ = seed(state)
    a, b, c = rap["songs"]
    run(music_edit.order_folder(state, rap["id"], songs=[c, a, b]))
    assert rap["songs"] == [c, a, b]
    with pytest.raises(MusicError) as err:
        run(music_edit.order_folder(state, rap["id"], songs=[a, b]))
    assert err.value.status == 409


# -- moving ------------------------------------------------------------------------


def test_move_and_add_to_folder(state):
    lib, rap, pop = seed(state)
    a, b, c = rap["songs"]
    run(music_edit.move_songs(state, [b, c], pop["id"], source=rap["id"]))
    assert rap["songs"] == [a] and titles(lib, pop["songs"]) == ["D", "B", "C"]
    run(music_edit.move_songs(state, [a], pop["id"], copy=True, index=0))
    assert rap["songs"] == [a] and pop["songs"][0] == a


def test_removing_from_the_last_folder_lands_in_unsorted(state):
    lib, rap, pop = seed(state)
    d = pop["songs"][0]
    run(music_edit.remove_from_folder(state, pop["id"], [d]))
    assert lib.folders[music.UNSORTED_ID]["songs"] == [d]


def test_folders_nest_move_and_refuse_cycles(state):
    lib, rap, pop = seed(state)
    sub = run(music_edit.create_folder(state, "2019", rap["id"]))
    assert rap["children"] == [sub["id"]]
    run(music_edit.move_folder(state, pop["id"], sub["id"]))
    assert sub["children"] == [pop["id"]] and pop["id"] not in lib.root
    with pytest.raises(MusicError):
        run(music_edit.move_folder(state, rap["id"], pop["id"]))
    with pytest.raises(MusicError):
        run(music_edit.create_folder(state, "rap"))  # same name, any case


def test_folder_cover_change_carries_the_songs_showing_it(state):
    root = inbox(state)
    make_wav(root / "Album" / "1.wav", seed=1, track=1, art=PNG)
    make_wav(root / "Album" / "2.wav", seed=2, track=2, art=PNG)
    scan_twice(state)
    lib = music.library(state)
    album = folder_named(lib, "Album")
    old = album["cover"]
    # A second picture in the vault.
    (state.settings.vault_dir / "attachments" / "x-0000000000000000.png").write_bytes(PNG)
    new = "attachments/x-0000000000000000.png"
    run(music_edit.update_folder(state, album["id"], {"cover": new}))
    assert album["cover"] == new
    assert all(lib.songs[s]["cover"] == new for s in album["songs"])
    assert old != new


def test_delete_folder_trashes_songs_that_live_nowhere_else(state):
    lib, rap, pop = seed(state)
    a, b, c = rap["songs"]
    run(music_edit.move_songs(state, [a], pop["id"], copy=True))
    result = run(music_edit.delete_folder(state, rap["id"]))
    assert rap["id"] not in lib.folders
    assert sorted(result["trashed"]) == sorted([b, c])
    assert lib.songs[b]["trashed"] and not lib.songs[a].get("trashed")


# -- trash -------------------------------------------------------------------------


def test_trash_restore_and_empty(state):
    lib, rap, _ = seed(state)
    a, b, _c = rap["songs"]
    run(music_edit.set_liked(state, a, True))
    run(music_edit.trash_songs(state, [a]))
    assert a not in rap["songs"] and a not in lib.liked
    assert lib.audio_path(lib.songs[a]).exists()  # audio kept while in the trash
    run(music_edit.restore_songs(state, [a]))
    assert a in rap["songs"] and lib.liked == [a] and rap["liked"] == [a]
    run(music_edit.trash_songs(state, [b]))
    audio = lib.audio_path(lib.songs[b])
    assert run(music_edit.empty_trash(state)) == [b]
    assert b not in lib.songs and not audio.exists()


def test_trashed_songs_stay_trashed_after_reload(state):
    lib, rap, _ = seed(state)
    a = rap["songs"][0]
    run(music_edit.trash_songs(state, [a]))
    fresh = music.MusicLibrary(state.settings.vault_dir, state.settings.data_dir)
    fresh.load()
    assert fresh.songs[a]["trashed"]
    assert not any(a in f["songs"] for f in fresh.folders.values())


# -- playlists ---------------------------------------------------------------------


def test_playlists(state):
    lib, rap, pop = seed(state)
    a, b, c = rap["songs"]
    d = pop["songs"][0]
    playlist = run(music_edit.create_playlist(state, "Night", [d, a]))
    run(music_edit.playlist_add(state, playlist["id"], [c, a], index=1))
    assert playlist["songs"] == [d, c, a]
    run(music_edit.update_playlist(state, playlist["id"], {"blur": 400, "dim": -3}))
    assert playlist["blur"] == 100 and playlist["dim"] == 0
    run(music_edit.trash_songs(state, [c]))
    assert playlist["songs"] == [d, a]
    fresh = music.MusicLibrary(state.settings.vault_dir, state.settings.data_dir)
    fresh.load()
    assert fresh.playlists[playlist["id"]]["songs"] == [d, a]
    run(music_edit.delete_playlist(state, playlist["id"]))
    assert lib.playlist_order == []


# -- uploads -----------------------------------------------------------------------


def test_upload_in_pieces_resumes_and_imports_into_a_folder(state, tmp_path, monkeypatch):
    lib, rap, _ = seed(state)
    monkeypatch.setattr(music_upload, "PIECE", 1000)
    source = make_wav(tmp_path / "src" / "new song.wav", seed=42, frames=1500, title="New")
    data = source.read_bytes()
    client = make_client(state, music_api.router)

    opened = client.post("/api/music/uploads", json={
        "name": "new song.wav", "size": len(data), "folder": rap["id"], "parts": ["Live"],
    }).json()
    uid = opened["id"]
    first = client.put(f"/api/music/uploads/{uid}?offset=0", content=data[:1000])
    assert first.json()["received"] == 1000
    # A retried piece at the wrong offset is refused with the real position.
    again = client.put(f"/api/music/uploads/{uid}?offset=0", content=data[:1000])
    assert again.status_code == 409
    too_big = client.put(f"/api/music/uploads/{uid}?offset=1000", content=data[1000:] + bytes(2000))
    assert too_big.status_code == 413
    assert client.get(f"/api/music/uploads/{uid}").json()["received"] == 1000
    early = client.post(f"/api/music/uploads/{uid}/finish")
    assert early.status_code == 409
    for start in range(1000, len(data), 1000):
        r = client.put(f"/api/music/uploads/{uid}?offset={start}", content=data[start:start + 1000])
        assert r.status_code == 200, r.text
    report = client.post(f"/api/music/uploads/{uid}/finish").json()

    assert len(report["added"]) == 1
    live = folder_named(lib, "Live", rap["id"])
    assert titles(lib, live["songs"]) == ["New"]
    assert not any(music_upload._dir(state).iterdir())


def test_upload_refuses_what_it_cannot_play(state):
    client = make_client(state, music_api.router)
    assert client.post("/api/music/uploads", json={"name": "x.exe", "size": 10}).status_code == 400
    big = client.post("/api/music/uploads", json={"name": "x.flac", "size": 600 * 1024 * 1024})
    assert big.status_code == 413


# -- conversion --------------------------------------------------------------------

FFMPEG = os.environ.get("MUSIC_TEST_FFMPEG") or shutil.which("ffmpeg")


def test_what_needs_a_copy(state):
    lib, rap, _ = seed(state)
    song = lib.songs[rap["songs"][0]]
    assert [name for name, _ in music_convert.wanted(lib, song)] == ["stream.mp3", "lossless.flac"]
    song["file"] = "x.mp3"
    assert music_convert.wanted(lib, song) == []


def test_stream_falls_back_to_the_original_without_copies(state):
    lib, rap, _ = seed(state)
    song = lib.songs[rap["songs"][0]]
    path, mime = music_convert.pick_file(lib, song, lossless=False)
    assert path == lib.audio_path(song) and mime == "audio/wav"


@pytest.mark.skipif(not FFMPEG, reason="no ffmpeg (set MUSIC_TEST_FFMPEG)")
def test_conversion_makes_the_mp3_and_flac_and_streams_them(state):
    state.settings.music_ffmpeg = FFMPEG
    lib, rap, _ = seed(state)
    song_id = rap["songs"][0]
    made = run(music_convert.convert_one(state, song_id))
    assert made == ["stream.mp3", "lossless.flac"]
    song = lib.songs[song_id]
    client = make_client(state, music_api.router)
    mp3 = client.get(f"/api/music/stream/{song_id}")
    assert mp3.headers["content-type"] == "audio/mpeg" and mp3.content[:3] in (b"ID3", b"\xff\xfb")
    flac = client.get(f"/api/music/stream/{song_id}?lossless=1")
    assert flac.headers["content-type"] == "audio/flac" and flac.content[:4] == b"fLaC"
    original = client.get(f"/api/music/download/{song_id}")
    assert original.content[:4] == b"RIFF"
    assert song["stream"]["bytes"] > 0


def test_move_api_copy_flag_adds_instead_of_moving(state):
    lib, rap, pop = seed(state)
    a = rap["songs"][0]
    client = make_client(state, music_api.router)
    r = client.post("/api/music/songs/move", json={"ids": [a], "target": pop["id"], "source": rap["id"], "copy": True})
    assert r.status_code == 200
    assert a in rap["songs"] and a in pop["songs"]
    r = client.post("/api/music/songs/move", json={"ids": [a], "target": pop["id"], "source": rap["id"]})
    assert a not in rap["songs"] and a in pop["songs"]
