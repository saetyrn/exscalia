"""Secret stores outside the vault (ai.json, media.json): written atomically at
0600, and an unreadable file is set aside and never overwritten."""
import json
import os
import stat
from pathlib import Path

import pytest

from app import media_lookup
from app.api import media as media_api
from app.api import meta as meta_api
from app.overseer import runtime_config as rc
from app.vault.internal import atomic_secret_json, quarantine_pending
from app.vault.notes import NoteError
from tests.test_autocorrect import ai_settings
from tests.test_media_api import make_client

KEYS = {
    "provider": "anthropic",
    "keys": {"anthropic": "sk-ant-real", "openai": "sk-real", "google": "AIza-real"},
    "models": {"anthropic": "claude-opus-4-8"},
    "reasoning": "high",
}


def _mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _leftovers(tmp_path) -> list[str]:
    return sorted(p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp"))


def _torn(path, value) -> bytes:
    """What a full disk leaves behind: the head of the file, then nothing."""
    raw = json.dumps(value, indent=2).encode()[: len(json.dumps(value)) // 2]
    path.write_bytes(raw)
    return raw


def _cannot_read(monkeypatch, path) -> None:
    """EACCES on `path` only: a root-owned 0600 file seen by uid 10001. Not
    chmod, which root (some CI containers) reads through anyway."""
    real = Path.read_bytes

    def read_bytes(self):
        if self == path:
            raise PermissionError(13, "Permission denied", str(self))
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)


# -- the writer ---------------------------------------------------------------


def test_atomic_secret_json_is_0600_from_creation(tmp_path):
    old = os.umask(0o022)
    try:
        path = tmp_path / "secret.json"
        atomic_secret_json(path, {"k": "v"})
        assert _mode(path) == 0o600
        assert json.loads(path.read_text()) == {"k": "v"}
        # Replacing a file that somehow became 0644 lands 0600 again.
        os.chmod(path, 0o644)
        atomic_secret_json(path, {"k": "w"})
        assert _mode(path) == 0o600
    finally:
        os.umask(old)
    assert _leftovers(tmp_path) == []


def test_failed_write_leaves_the_original_and_no_temp(tmp_path, monkeypatch):
    path = tmp_path / "secret.json"
    atomic_secret_json(path, {"k": "original"})

    def disk_full(_fd):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "fsync", disk_full)
    with pytest.raises(OSError):
        atomic_secret_json(path, {"k": "replacement"})
    assert json.loads(path.read_text()) == {"k": "original"}
    assert _leftovers(tmp_path) == []


def test_failed_ai_save_keeps_the_keys(tmp_path, monkeypatch):
    rc.save_ai_config(tmp_path, KEYS)

    def disk_full(_fd):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "fsync", disk_full)
    with pytest.raises(OSError):
        rc.save_ai_config(tmp_path, {**KEYS, "monthly_token_budget": 5})
    monkeypatch.undo()
    assert rc.load_ai_config(tmp_path) == KEYS
    assert quarantine_pending(tmp_path / "ai.json") is None


# -- ai.json ------------------------------------------------------------------


def test_torn_ai_json_is_set_aside_and_save_refuses(tmp_path, caplog):
    path = tmp_path / "ai.json"
    rc.save_ai_config(tmp_path, KEYS)
    assert _mode(path) == 0o600
    torn = _torn(path, KEYS)

    # Readers (every chat and background turn) degrade to "no config".
    with caplog.at_level("ERROR"):
        assert rc.load_ai_config(tmp_path) == {}
    assert "unreadable" in caplog.text
    quarantined = quarantine_pending(path)
    assert quarantined is not None and quarantined.name.startswith("ai.json.corrupt-")
    assert quarantined.read_bytes() == torn
    assert not path.exists()
    assert rc.load_ai_config(tmp_path) == {}  # still readable, no raise

    # Writing {} + one field would bury the keys: refuse, keep the evidence.
    with pytest.raises(NoteError) as err:
        rc.save_ai_config(tmp_path, {"monthly_token_budget": 1})
    assert err.value.status == 500
    assert quarantined.name in err.value.detail
    assert not path.exists()
    assert quarantined.read_bytes() == torn

    # The owner deals with it (here: removes it), then saving works again.
    quarantined.unlink()
    rc.save_ai_config(tmp_path, {"monthly_token_budget": 1})
    assert rc.load_ai_config(tmp_path) == {"monthly_token_budget": 1}
    assert _mode(path) == 0o600


def test_ai_json_that_cannot_be_read_stays_put_and_save_refuses(tmp_path, monkeypatch, caplog):
    """A permission or I/O error is not corruption: renaming the file away
    left every later boot without keys until someone found the quarantine."""
    path = tmp_path / "ai.json"
    rc.save_ai_config(tmp_path, KEYS)
    on_disk = path.read_bytes()
    _cannot_read(monkeypatch, path)

    with caplog.at_level("ERROR"):
        assert rc.load_ai_config(tmp_path) == {}
    assert "cannot be read" in caplog.text
    assert quarantine_pending(path) is None

    # {} + one field over a file nobody could read would bury the keys.
    with pytest.raises(NoteError) as err:
        rc.save_ai_config(tmp_path, {"monthly_token_budget": 1})
    assert err.value.status == 500
    assert "cannot be read" in err.value.detail

    # Once the file is readable again, nothing needs cleaning up.
    monkeypatch.undo()
    assert path.read_bytes() == on_disk
    assert rc.load_ai_config(tmp_path) == KEYS


@pytest.mark.parametrize("content", ["null", "[]", '"sk-ant-x"'])
def test_ai_json_of_the_wrong_type_is_set_aside(tmp_path, content):
    path = tmp_path / "ai.json"
    path.write_text(content, encoding="utf-8")
    assert rc.load_ai_config(tmp_path) == {}
    assert quarantine_pending(path) is not None
    with pytest.raises(NoteError):
        rc.save_ai_config(tmp_path, {})


def test_legacy_ai_json_still_migrates(tmp_path):
    (tmp_path / "ai.json").write_text(
        json.dumps({"api_key": "sk-ant-old", "model": "claude-sonnet-5"}), encoding="utf-8"
    )
    assert rc.load_ai_config(tmp_path) == {
        "provider": "anthropic",
        "keys": {"anthropic": "sk-ant-old"},
        "models": {"anthropic": "claude-sonnet-5"},
    }
    assert quarantine_pending(tmp_path / "ai.json") is None


def test_settings_route_does_not_persist_a_budget_over_torn_keys(state):
    """The verifier's repro: torn ai.json, then an unrelated Settings save."""
    for name, value in vars(ai_settings()).items():
        setattr(state.settings, name, value)
    data_dir = state.settings.data_dir
    path = data_dir / "ai.json"
    rc.save_ai_config(data_dir, KEYS)
    torn = _torn(path, KEYS)
    client = make_client(state, meta_api.router)

    saved = client.put("/api/settings/omnia", json={"monthly_token_budget": 2_000_000})
    assert saved.status_code == 500
    assert "set aside" in saved.json()["detail"]
    assert not path.exists()
    assert quarantine_pending(path).read_bytes() == torn


# -- media.json ---------------------------------------------------------------


def test_torn_media_json_is_set_aside_and_save_refuses(tmp_path):
    path = tmp_path / "media.json"
    media_lookup.save_media_keys(tmp_path, {"tmdb": "t-key", "rawg": "r-key"})
    assert _mode(path) == 0o600
    torn = _torn(path, {"tmdb": "t-key", "rawg": "r-key"})

    assert media_lookup.load_media_keys(tmp_path) == {}
    quarantined = quarantine_pending(path)
    assert quarantined is not None and quarantined.name.startswith("media.json.corrupt-")
    assert quarantined.read_bytes() == torn

    with pytest.raises(NoteError):
        media_lookup.save_media_keys(tmp_path, {"rawg": "new"})
    assert not path.exists()
    assert quarantined.read_bytes() == torn

    quarantined.unlink()
    media_lookup.save_media_keys(tmp_path, {"rawg": "new"})
    assert media_lookup.load_media_keys(tmp_path) == {"rawg": "new"}


def test_media_json_that_cannot_be_read_stays_put_and_save_refuses(tmp_path, monkeypatch):
    path = tmp_path / "media.json"
    media_lookup.save_media_keys(tmp_path, {"tmdb": "t-key", "rawg": "r-key"})
    _cannot_read(monkeypatch, path)

    assert media_lookup.load_media_keys(tmp_path) == {}
    assert quarantine_pending(path) is None
    with pytest.raises(NoteError):
        media_lookup.save_media_keys(tmp_path, {"rawg": "new"})

    monkeypatch.undo()
    assert media_lookup.load_media_keys(tmp_path) == {"tmdb": "t-key", "rawg": "r-key"}


def test_media_json_of_the_wrong_type_is_set_aside(tmp_path):
    path = tmp_path / "media.json"
    path.write_text('["tmdb"]', encoding="utf-8")
    assert media_lookup.load_media_keys(tmp_path) == {}
    assert quarantine_pending(path) is not None
    with pytest.raises(NoteError):
        media_lookup.save_media_keys(tmp_path, {})


def test_media_settings_route_refuses_over_torn_keys(state):
    data_dir = state.settings.data_dir
    path = data_dir / "media.json"
    media_lookup.save_media_keys(data_dir, {"tmdb": "t-key", "rawg": "r-key"})
    torn = _torn(path, {"tmdb": "t-key", "rawg": "r-key"})
    client = make_client(state, media_api.router)

    saved = client.put("/api/media/settings", json={"rawg": "only-this"})
    assert saved.status_code == 500
    assert not path.exists()
    assert quarantine_pending(path).read_bytes() == torn
