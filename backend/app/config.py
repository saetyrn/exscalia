from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OMNIA_", env_file=".env", extra="ignore")

    # Storage
    data_dir: Path = REPO_ROOT / "data"
    frontend_dir: Path = REPO_ROOT / "frontend" / "build"
    # Desktop-app update feed: latest.json + signed installers, published by
    # desktop/publish-update.sh. Served unauthenticated at /updates/*.
    updates_dir: Path = REPO_ROOT / "updates"
    timezone: str = "America/New_York"

    # Auth: if unset, auth is disabled (dev mode / trusted network)
    auth_username: str = "owner"
    auth_password: str | None = None
    session_secret: str = "change-me-in-production"

    # Web Push (VAPID). Left unset the app auto-generates a keypair on first
    # boot and persists it to data_dir/push_keys.json — set these only to pin
    # an existing keypair. Both are base64url (public = the browser's
    # applicationServerKey; private = the raw VAPID private key).
    vapid_public_key: str | None = None
    vapid_private_key: str | None = None
    vapid_subject: str = "mailto:owner@exscalia.art"

    # Git identity for the user's own autosave commits
    git_author_name: str = "user"
    git_author_email: str = "user@exscalia.local"
    # Seconds of quiet after a save before an auto-commit fires
    autocommit_debounce: float = 10.0

    # AI provider (overseer activates only when a key is present).
    # Keys/models/provider can also be set at runtime from the Settings UI.
    ai_provider: str = "anthropic"
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-opus-4-8"
    openai_api_key: str | None = None
    openai_model: str = "gpt-5.6-sol"
    google_api_key: str | None = None
    google_model: str = "gemini-3.6-flash"

    # Off-site vault backup: a git remote pushed to on an interval.
    # e.g. https://x-access-token:<TOKEN>@github.com/you/vault-backup.git
    backup_remote: str | None = None
    backup_interval: float = 3600.0  # seconds between pushes

    # Embeddings
    embedding_backend: str = "fastembed"
    embedding_model: str = "BAAI/bge-small-en-v1.5"

    @property
    def vault_dir(self) -> Path:
        return self.data_dir / "vault"

    @property
    def index_dir(self) -> Path:
        return self.data_dir / "index"


@lru_cache
def get_settings() -> Settings:
    return Settings()
