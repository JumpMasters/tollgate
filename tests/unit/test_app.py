"""Tests for the composition root."""

from __future__ import annotations

from fastapi import FastAPI

from tollgate.app import build_app
from tollgate.application.handlers.reserve import ReserveHandler
from tollgate.config.settings import Settings


def test_build_app_uses_defaults() -> None:
    app = build_app()
    assert isinstance(app, FastAPI)
    assert app.state.engine is not None
    assert app.state.settings is not None


def test_build_app_accepts_injected_settings() -> None:
    settings = Settings(database_url="postgresql+asyncpg://u:p@localhost/db")
    app = build_app(settings)
    assert app.state.settings is settings


def test_build_app_wires_the_reserve_handler() -> None:
    app = build_app()
    assert isinstance(app.state.reserve_handler, ReserveHandler)
