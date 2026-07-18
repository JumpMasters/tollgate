"""Tests for application settings."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from tollgate.config.settings import Settings, load_settings


def test_defaults() -> None:
    settings = Settings()
    assert settings.database_url.get_secret_value().startswith("postgresql+asyncpg://")
    assert settings.reserve_statement_timeout_ms == 2_000
    assert settings.reservation_ttl_seconds == 600
    assert settings.token_hash_secret.get_secret_value() == ""


def test_environment_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOLLGATE_DATABASE_URL", "postgresql+asyncpg://x:y@db:5432/z")
    monkeypatch.setenv("TOLLGATE_RESERVATION_TTL_SECONDS", "30")
    monkeypatch.setenv("TOLLGATE_TOKEN_HASH_SECRET", "pepper-from-env")
    settings = load_settings()
    assert settings.database_url.get_secret_value() == "postgresql+asyncpg://x:y@db:5432/z"
    assert settings.reservation_ttl_seconds == 30
    assert settings.token_hash_secret.get_secret_value() == "pepper-from-env"


def test_reaper_settings_have_sane_defaults() -> None:
    settings = Settings(token_hash_secret=SecretStr("s"))  # match this file's construction style
    assert settings.reaper_poll_interval_seconds == 30.0
    assert settings.reaper_batch_size == 100
    assert settings.idempotency_ttl_hours == 24
    assert settings.idempotency_reaper_poll_interval_seconds == 3600.0
    assert settings.idempotency_reaper_batch_size == 500
