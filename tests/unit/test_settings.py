"""Tests for application settings."""

from __future__ import annotations

import pytest

from tollgate.config.settings import Settings, load_settings


def test_defaults() -> None:
    settings = Settings()
    assert settings.database_url.startswith("postgresql+asyncpg://")
    assert settings.reserve_statement_timeout_ms == 2_000
    assert settings.reservation_ttl_seconds == 600


def test_environment_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOLLGATE_DATABASE_URL", "postgresql+asyncpg://x:y@db:5432/z")
    monkeypatch.setenv("TOLLGATE_RESERVATION_TTL_SECONDS", "30")
    settings = load_settings()
    assert settings.database_url == "postgresql+asyncpg://x:y@db:5432/z"
    assert settings.reservation_ttl_seconds == 30
