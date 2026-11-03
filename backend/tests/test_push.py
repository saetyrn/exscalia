"""Web Push: key lifecycle, payload shaping, subscription CRUD, and delivery
(pruning dead endpoints). Sends are monkeypatched — no network, no pywebpush."""

import asyncio
import json
import stat
import sys
import time
import types
from pathlib import Path
from types import SimpleNamespace

from app import push_service
from app.api import auth as auth_module


def make_auth(tmp_path):
    return auth_module.SessionAuth("owner", "pw", "secret", tmp_path)


def _settings(tmp_path, pub=None, priv=None):
    return SimpleNamespace(
        data_dir=tmp_path,
        vapid_public_key=pub,
        vapid_private_key=priv,
        vapid_subject="mailto:owner@test",
    )


def test_vapid_is_generated_persisted_and_stable(tmp_path):
    v1 = push_service.load_or_create_vapid(_settings(tmp_path))
    assert v1.public_key and v1.private_key
    path = tmp_path / "push_keys.json"
    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # A second boot loads the SAME keypair — rotating it would kill every sub.
    v2 = push_service.load_or_create_vapid(_settings(tmp_path))
    assert (v2.public_key, v2.private_key) == (v1.public_key, v1.private_key)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["push_keys.json"]


def test_vapid_env_pins_keys_and_writes_nothing(tmp_path):
    v = push_service.load_or_create_vapid(_settings(tmp_path, pub="PUB", priv="PRIV"))
    assert (v.public_key, v.private_key) == ("PUB", "PRIV")
    assert not (tmp_path / "push_keys.json").exists()


def _unreadable(tmp_path):
    return sorted(tmp_path.glob("push_keys.json.unreadable-*"))


def test_unreadable_vapid_file_disables_push_instead_of_rotating(tmp_path, caplog):
    v1 = push_service.load_or_create_vapid(_settings(tmp_path))
    path = tmp_path / "push_keys.json"
    torn = path.read_bytes()[:40]
    path.write_bytes(torn)

    with caplog.at_level("ERROR"):
        assert push_service.load_or_create_vapid(_settings(tmp_path)) is None
    assert "push disabled" in caplog.text
    # Set aside with its bytes intact; no new key minted in its place.
    (quarantined,) = _unreadable(tmp_path)
    assert quarantined.read_bytes() == torn
    assert not path.exists()

    # The next boot must not treat "absent" as "first boot" either.
    assert push_service.load_or_create_vapid(_settings(tmp_path)) is None
    assert not path.exists()

    # Restoring the key file brings the SAME key back.
    path.write_text(
        json.dumps({"public_key": v1.public_key, "private_key": v1.private_key}),
        encoding="utf-8",
    )
    quarantined.unlink()
    v2 = push_service.load_or_create_vapid(_settings(tmp_path))
    assert (v2.public_key, v2.private_key) == (v1.public_key, v1.private_key)


def test_vapid_file_that_cannot_be_read_disables_push_for_this_boot_only(tmp_path, monkeypatch, caplog):
    """EACCES or EIO is not corruption: the file stays put, no key is minted,
    and the next boot that can read it has push again."""
    v1 = push_service.load_or_create_vapid(_settings(tmp_path))
    path = tmp_path / "push_keys.json"
    real = Path.read_bytes

    def read_bytes(self):
        if self == path:
            raise PermissionError(13, "Permission denied", str(self))
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    with caplog.at_level("ERROR"):
        assert push_service.load_or_create_vapid(_settings(tmp_path)) is None
    assert "cannot be read" in caplog.text
    assert _unreadable(tmp_path) == []
    assert path.is_file()

    monkeypatch.undo()
    v2 = push_service.load_or_create_vapid(_settings(tmp_path))
    assert (v2.public_key, v2.private_key) == (v1.public_key, v1.private_key)


def test_vapid_file_of_the_wrong_shape_is_set_aside(tmp_path):
    path = tmp_path / "push_keys.json"
    path.write_text(json.dumps({"publicKey": "BPUB", "privateKey": "PRIV"}), encoding="utf-8")
    assert push_service.load_or_create_vapid(_settings(tmp_path)) is None
    (quarantined,) = _unreadable(tmp_path)
    assert json.loads(quarantined.read_text()) == {"publicKey": "BPUB", "privateKey": "PRIV"}
    assert not path.exists()

    # A second bad file in the same second sits beside the first, not over it.
    path.write_text("[]", encoding="utf-8")
    assert push_service.load_or_create_vapid(_settings(tmp_path)) is None
    assert not path.exists()
    assert len(_unreadable(tmp_path)) == 2
    assert quarantined.is_file()


def test_disabled_push_is_reported_and_delivers_nothing(tmp_path):
    (tmp_path / "push_keys.json").write_text("{", encoding="utf-8")
    vapid = push_service.load_or_create_vapid(_settings(tmp_path))
    assert vapid is None
    auth = make_auth(tmp_path)
    auth.add_push_subscription("https://push/e1", "p", "a", "UA")
    state = SimpleNamespace(vapid=vapid, auth=auth)
    result = asyncio.run(push_service.deliver(state, [{"title": "T"}]))
    assert result == {"sent": 0, "pruned": 0}
    assert len(auth.list_push_subscriptions()) == 1


def test_build_payload_routes_and_bodies():
    reminder = push_service.build_payload(
        {"title": "Lecture", "minutes_before": 15, "key": "e1|s|15"}
    )
    assert reminder == {"title": "Lecture", "body": "in 15 min", "url": "/calendar", "tag": "e1|s|15"}

    omnia = push_service.build_payload(
        {"title": "Heads up", "body": "do the thing", "source": "omnia", "tag": "omnia:1"}
    )
    assert omnia["url"] == "/omnia" and omnia["body"] == "do the thing"

    assert push_service.build_payload({"title": "T", "overdue": True})["body"] == "Overdue"
    assert push_service.build_payload({"title": "T"})["body"] == "now"
    assert push_service.build_payload({})["title"] == "EXSCALIA"


def test_push_subscription_crud(tmp_path):
    auth = make_auth(tmp_path)
    auth.add_push_subscription("https://push/e1", "p1", "a1", "UA")
    subs = auth.list_push_subscriptions()
    assert len(subs) == 1 and subs[0]["endpoint"] == "https://push/e1"

    # Re-subscribing the same endpoint refreshes keys, never duplicates.
    auth.add_push_subscription("https://push/e1", "p2", "a2", "UA2")
    subs = auth.list_push_subscriptions()
    assert len(subs) == 1 and subs[0]["p256dh"] == "p2" and subs[0]["user_agent"] == "UA2"

    assert subs[0]["last_ok_at"] is None
    auth.mark_push_ok("https://push/e1")
    assert auth.list_push_subscriptions()[0]["last_ok_at"]

    auth.remove_push_subscription("https://push/e1")
    assert auth.list_push_subscriptions() == []


def test_deliver_marks_live_and_prunes_dead(tmp_path, monkeypatch):
    auth = make_auth(tmp_path)
    auth.add_push_subscription("https://push/alive", "p", "a", "UA")
    auth.add_push_subscription("https://push/dead", "p", "a", "UA")
    state = SimpleNamespace(vapid=push_service.VapidKeys("pub", "priv", "mailto:x@y"), auth=auth)

    def fake_send(info, payload, vapid):
        return 410 if info["endpoint"].endswith("dead") else 201

    monkeypatch.setattr(push_service, "send_push", fake_send)
    result = asyncio.run(push_service.deliver(state, [{"title": "Hi", "source": "omnia"}]))

    assert result == {"sent": 1, "pruned": 1}
    remaining = auth.list_push_subscriptions()
    assert [s["endpoint"] for s in remaining] == ["https://push/alive"]
    assert remaining[0]["last_ok_at"]  # the live endpoint was marked ok


def test_deliver_is_noop_without_vapid_or_subscriptions(tmp_path):
    auth = make_auth(tmp_path)
    no_vapid = SimpleNamespace(vapid=None, auth=auth)
    assert asyncio.run(push_service.deliver(no_vapid, [{"title": "x"}])) == {"sent": 0, "pruned": 0}

    with_vapid = SimpleNamespace(vapid=push_service.VapidKeys("p", "k", "s"), auth=auth)
    assert asyncio.run(push_service.deliver(with_vapid, [{"title": "x"}])) == {"sent": 0, "pruned": 0}


def test_send_push_bounds_the_request(monkeypatch):
    """pywebpush hands requests `timeout=None` unless given one, and requests
    then waits forever on a push service that accepts and never answers."""
    calls = []

    def webpush(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(status_code=201)

    fake = types.ModuleType("pywebpush")
    fake.webpush = webpush
    fake.WebPushException = type("WebPushException", (Exception,), {})
    monkeypatch.setitem(sys.modules, "pywebpush", fake)

    status = push_service.send_push(
        {"endpoint": "https://push/e1", "keys": {"p256dh": "p", "auth": "a"}},
        {"title": "Hi"},
        push_service.VapidKeys("pub", "priv", "mailto:x@y"),
    )
    assert status == 201
    assert calls[0]["timeout"] == push_service.SEND_TIMEOUT


def test_deliver_gives_up_on_a_stalled_endpoint_and_moves_on(tmp_path, monkeypatch):
    auth = make_auth(tmp_path)
    auth.add_push_subscription("https://push/stalled", "p", "a", "UA")
    auth.add_push_subscription("https://push/fast", "p", "a", "UA")
    state = SimpleNamespace(vapid=push_service.VapidKeys("pub", "priv", "mailto:x@y"), auth=auth)
    attempted = []

    def fake_send(info, payload, vapid):
        attempted.append(info["endpoint"])
        if info["endpoint"].endswith("stalled"):
            time.sleep(1.0)  # a push service that never answers
        return 201

    monkeypatch.setattr(push_service, "send_push", fake_send)
    monkeypatch.setattr(push_service, "DELIVER_TIMEOUT", 0.05)

    async def run():
        started = time.monotonic()
        result = await push_service.deliver(state, [{"title": "Hi", "source": "omnia"}])
        return result, time.monotonic() - started

    result, elapsed = asyncio.run(run())

    assert elapsed < 0.5
    assert sorted(attempted) == ["https://push/fast", "https://push/stalled"]
    assert result == {"sent": 1, "pruned": 0}
    subs = {s["endpoint"]: s for s in auth.list_push_subscriptions()}
    assert subs["https://push/fast"]["last_ok_at"]
    assert subs["https://push/stalled"]["last_ok_at"] is None  # kept, not marked ok
