"""Web Push: key lifecycle, payload shaping, subscription CRUD, and delivery
(pruning dead endpoints). Sends are monkeypatched — no network, no pywebpush."""

import asyncio
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
    assert (tmp_path / "push_keys.json").is_file()
    # A second boot loads the SAME keypair — rotating it would kill every sub.
    v2 = push_service.load_or_create_vapid(_settings(tmp_path))
    assert (v2.public_key, v2.private_key) == (v1.public_key, v1.private_key)


def test_vapid_env_pins_keys_and_writes_nothing(tmp_path):
    v = push_service.load_or_create_vapid(_settings(tmp_path, pub="PUB", priv="PRIV"))
    assert (v.public_key, v.private_key) == ("PUB", "PRIV")
    assert not (tmp_path / "push_keys.json").exists()


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
