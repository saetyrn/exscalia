"""Music, Deploy 3: the shared session and play history."""
import asyncio
import time

import pytest

from app import music_session
from app.api import music as music_api
from app.music_history import history

from conftest import make_client
from test_music import inbox, make_wav, scan_twice
from app import music


def run(coro):
    return asyncio.run(coro)


def seeded(state):
    for i in range(3):
        make_wav(inbox(state) / "Rap" / f"{i}.wav", seed=i + 1, title=f"S{i}")
    scan_twice(state)
    lib = music.library(state)
    return [s for f in lib.folders.values() for s in f["songs"]]


def test_play_from_one_device_and_follow_from_another(state):
    ids = seeded(state)
    client = make_client(state, music_api.router)
    queue = state.live.subscribe()
    first = client.get("/api/music/session").json()
    assert first["version"] == 0 and first["playing"] is False

    r = client.put("/api/music/session", json={"base": 0, "state": {
        "queue": ids, "original": ids, "index": 1, "playing": True, "position": 0,
        "speaker": "mac:tab1", "speaker_name": "Mac", "context": "rap",
    }})
    assert r.status_code == 200
    now = r.json()
    assert now["version"] == 1 and now["speaker"] == "mac:tab1" and now["index"] == 1
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    assert {"type": "music", "path": "music/session", "revision": 1} in events

    # The phone, still on version 0, is refused with the current state.
    stale = client.put("/api/music/session", json={"base": 0, "state": {"playing": False}})
    assert stale.status_code == 409 and stale.json()["session"]["version"] == 1
    # On the right version it pauses the Mac.
    paused = client.put("/api/music/session", json={"base": 1, "state": {"playing": False, "position": 12.5}})
    assert paused.json()["playing"] is False and paused.json()["position"] == 12.5


def test_heartbeats_are_quiet_and_only_from_the_speaker(state):
    ids = seeded(state)
    run(music_session.update(state, None, {"queue": ids, "index": 0, "playing": True, "speaker": "a:1"}, music.library(state).songs))
    version = music_session.get(state)["version"]
    queue = state.live.subscribe()
    run(music_session.heartbeat(state, "a:1", 30.0, True))
    run(music_session.heartbeat(state, "b:2", 99.0, True))  # not the speaker: ignored
    current = music_session.get(state)
    assert current["version"] == version and current["position"] == 30.0
    assert queue.empty()
    run(music_session.heartbeat(state, "a:1", 31.0, False, final=True))
    assert music_session.get(state)["playing"] is False
    assert music_session.get(state)["version"] == version + 1


def test_a_silent_speaker_is_paused(state, monkeypatch):
    ids = seeded(state)
    run(music_session.update(state, None, {"queue": ids, "index": 0, "playing": True, "position": 5, "speaker": "a:1"}, music.library(state).songs))
    sess = music_session.session(state)
    sess.state["heartbeat_at"] = time.time() - 120
    sess.state["position_at"] = time.time() - 120
    current = music_session.get(state)
    assert current["playing"] is False
    assert current["position"] == pytest.approx(5 + music_session.STALE_SECONDS, abs=1)


def test_unknown_songs_drop_out_and_the_index_follows(state):
    ids = seeded(state)
    proposed = {"queue": ["gone", ids[0], ids[1]], "index": 2, "playing": False}
    current = run(music_session.update(state, None, proposed, music.library(state).songs))
    assert current["queue"] == [ids[0], ids[1]] and current["index"] == 1


def test_bad_values_are_refused(state):
    seeded(state)
    client = make_client(state, music_api.router)
    r = client.put("/api/music/session", json={"base": 0, "state": {"repeat": "sometimes"}})
    assert r.status_code == 400


def test_session_survives_a_restart_paused(state):
    ids = seeded(state)
    run(music_session.update(state, None, {"queue": ids, "index": 2, "playing": True, "position": 40, "speaker": "a:1"}, music.library(state).songs))
    fresh = music_session.Session(state.settings.data_dir)
    assert fresh.state["queue"] == ids and fresh.state["index"] == 2
    assert fresh.state["playing"] is False


def test_plays_are_recorded_and_recent_is_newest_first(state):
    ids = seeded(state)
    client = make_client(state, music_api.router)
    for song, seconds, ended in ((ids[0], 40, "finished"), (ids[1], 5, "skipped"), (ids[0], 31, "finished")):
        assert client.post("/api/music/plays", json={"song": song, "device": "mac", "seconds": seconds, "ended": ended}).status_code == 200
    assert client.post("/api/music/plays", json={"song": "nope", "device": "mac", "seconds": 1}).status_code == 404
    recent = client.get("/api/music/recent").json()["played"]
    assert [r["song"] for r in recent][:2] in ([ids[0], ids[1]], [ids[1], ids[0]])  # same second
    assert {r["song"]: r["plays"] for r in recent}[ids[0]] == 2
    assert len(history(state).plays(song_id=ids[0])) == 2


def test_emptying_the_trash_forgets_the_history(state):
    from app import music_edit

    ids = seeded(state)
    history(state).record(ids[0], "mac", 40, 60, "finished", False, None)
    run(music_edit.trash_songs(state, [ids[0]]))
    run(music_edit.empty_trash(state))
    assert history(state).plays(song_id=ids[0]) == []


def test_token_route(state):
    state.settings.session_secret = "s"
    client = make_client(state, music_api.router)
    body = client.get("/api/music/token").json()
    assert body["token"].startswith(str(body["expires"]))
