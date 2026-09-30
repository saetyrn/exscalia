"""A server that must be authenticated refuses to boot open, and /api/health
names the build that is answering (deploy.sh checks it through Caddy)."""

import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main as main_module
from app.config import DEFAULT_SESSION_SECRET, Settings
from app.main import check_auth_settings

REAL_SECRET = "0123456789abcdef" * 4


def app_settings(tmp_path: Path, **overrides) -> Settings:
    values = dict(
        data_dir=tmp_path / "app-data",
        frontend_dir=tmp_path / "no-frontend",
        embedding_backend="none",
        autocommit_debounce=0.01,
        auth_password=None,
        session_secret=DEFAULT_SESSION_SECRET,
        require_auth=False,
        anthropic_api_key=None,
        openai_api_key=None,
        google_api_key=None,
        backup_remote=None,
    )
    values.update(overrides)
    return Settings(**values)


def test_required_auth_refuses_an_empty_password(tmp_path: Path):
    for password in (None, ""):
        with pytest.raises(RuntimeError, match="OMNIA_AUTH_PASSWORD is empty"):
            check_auth_settings(
                app_settings(
                    tmp_path, require_auth=True, auth_password=password, session_secret=REAL_SECRET
                )
            )


def test_required_auth_refuses_the_default_session_secret(tmp_path: Path):
    for secret in (DEFAULT_SESSION_SECRET, ""):
        with pytest.raises(RuntimeError, match="OMNIA_SESSION_SECRET"):
            check_auth_settings(
                app_settings(
                    tmp_path, require_auth=True, auth_password="hunter2", session_secret=secret
                )
            )


def test_dev_mode_and_real_credentials_boot(tmp_path: Path):
    # A local dev server: no password, placeholder secret, nothing required.
    check_auth_settings(app_settings(tmp_path))
    check_auth_settings(
        app_settings(
            tmp_path, require_auth=True, auth_password="hunter2", session_secret=REAL_SECRET
        )
    )


def test_lifespan_refuses_before_touching_the_data_dir(tmp_path: Path, monkeypatch):
    settings = app_settings(tmp_path, require_auth=True, session_secret=REAL_SECRET)
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)

    with pytest.raises(RuntimeError, match="OMNIA_AUTH_PASSWORD is empty"):
        with TestClient(main_module.app):
            pass
    assert not settings.data_dir.exists()


def test_health_reports_the_build_behind_required_auth(tmp_path: Path, monkeypatch, caplog):
    settings = app_settings(
        tmp_path,
        require_auth=True,
        auth_password="hunter2",
        session_secret=REAL_SECRET,
        git_sha="abc1234",
    )
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)

    with caplog.at_level(logging.WARNING, logger=main_module.__name__):
        with TestClient(main_module.app) as client:
            assert client.get("/api/health").json() == {"ok": True, "build": "abc1234"}
            assert client.get("/api/auth/status").json()["auth_required"] is True
            assert client.get("/api/tasks").status_code == 401
    assert "authentication is DISABLED" not in caplog.text


def test_disabled_auth_is_logged_at_boot(tmp_path: Path, monkeypatch, caplog):
    settings = app_settings(tmp_path)
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)

    with caplog.at_level(logging.WARNING, logger=main_module.__name__):
        with TestClient(main_module.app) as client:
            assert client.get("/api/auth/status").json()["auth_required"] is False
    assert "authentication is DISABLED: every /api route is open" in caplog.text
    log_file = settings.data_dir / "logs" / "app.log"
    assert "authentication is DISABLED" in log_file.read_text(encoding="utf-8")
