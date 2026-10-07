import json
import os

import pytest

from app.overseer import runtime_config
from app.overseer.runtime_config import api_key_matches, load_ai_config, save_ai_config


def test_google_accepts_current_and_legacy_key_formats():
    assert api_key_matches("google", "AQ.example-auth-key")
    assert api_key_matches("google", "AIza-example-standard-key")


def test_google_rejects_other_provider_key_formats():
    assert not api_key_matches("google", "sk-example")


def test_other_provider_prefixes_are_unchanged():
    assert api_key_matches("anthropic", "sk-ant-example")
    assert api_key_matches("openai", "sk-example")


# --- the key file -------------------------------------------------------------


def mode(path) -> int:
    return os.stat(path).st_mode & 0o777


def test_config_round_trips_and_is_private(tmp_path):
    config = {
        "provider": "openai",
        "keys": {"anthropic": "sk-ant-one", "openai": "sk-two"},
        "models": {"openai": "gpt-5.4"},
        "reasoning": "high",
    }

    save_ai_config(tmp_path, config)

    assert load_ai_config(tmp_path) == config
    assert mode(tmp_path / "ai.json") == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["ai.json"]


def test_the_single_provider_file_migrates(tmp_path):
    (tmp_path / "ai.json").write_text(
        json.dumps({"api_key": "sk-ant-legacy", "model": "claude-opus-4-8"})
    )

    assert load_ai_config(tmp_path) == {
        "provider": "anthropic",
        "keys": {"anthropic": "sk-ant-legacy"},
        "models": {"anthropic": "claude-opus-4-8"},
    }


def test_a_corrupt_file_reads_as_empty(tmp_path):
    (tmp_path / "ai.json").write_text('{"provider": "anthr')

    assert load_ai_config(tmp_path) == {}


def test_a_failed_save_leaves_the_old_keys_in_place(tmp_path, monkeypatch):
    """The file was rewritten in place, so a process killed mid-write left it
    truncated; load_ai_config reads that as {} and the next settings save wrote
    it back without the keys. Now the old file stays until the new one is whole,
    and the new one is private before it is visible."""
    original = {"provider": "anthropic", "keys": {"anthropic": "sk-ant-keep-me"}}
    save_ai_config(tmp_path, original)
    before = (tmp_path / "ai.json").read_bytes()
    staged_modes: list[int] = []

    def replace_fails(src, dst):
        staged_modes.append(mode(src))
        raise OSError("disk went away")

    monkeypatch.setattr(os, "replace", replace_fails)
    with pytest.raises(OSError):
        save_ai_config(tmp_path, {"provider": "anthropic", "keys": {}})

    assert staged_modes == [0o600]
    assert (tmp_path / "ai.json").read_bytes() == before
    assert mode(tmp_path / "ai.json") == 0o600
    assert load_ai_config(tmp_path) == original
    assert [p.name for p in tmp_path.iterdir()] == ["ai.json"]
