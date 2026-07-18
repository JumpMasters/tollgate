"""Tests for the composition root."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from tollgate.adapters.postgres.engine import build_engine
from tollgate.app import _idempotency_reaper, _reservation_reaper, _serve, _worker_engine, build_app
from tollgate.application.handlers.cancel import CancelHandler
from tollgate.application.handlers.commit import CommitHandler
from tollgate.application.handlers.extend import ExtendHandler
from tollgate.application.handlers.grace import GraceBackfillHandler
from tollgate.application.handlers.reap import IdempotencyReaperHandler, ReservationReaperHandler
from tollgate.application.handlers.reserve import ReserveHandler
from tollgate.config.settings import Settings

_SETTINGS = Settings(
    database_url="postgresql+asyncpg://u:p@localhost/db",
    token_hash_secret="test-pepper",
)


def _settings() -> Settings:
    return Settings(
        token_hash_secret=SecretStr("s"), reaper_batch_size=7, idempotency_reaper_batch_size=9
    )


def test_build_app_uses_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOLLGATE_TOKEN_HASH_SECRET", "test-pepper")
    app = build_app()
    assert isinstance(app, FastAPI)
    assert app.state.engine is not None
    assert app.state.settings is not None


def test_build_app_refuses_an_empty_token_hash_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TOLLGATE_TOKEN_HASH_SECRET", raising=False)
    with pytest.raises(ValueError, match="TOLLGATE_TOKEN_HASH_SECRET"):
        build_app()


def test_build_app_accepts_injected_settings() -> None:
    app = build_app(_SETTINGS)
    assert app.state.settings is _SETTINGS


def test_build_app_wires_the_reserve_handler() -> None:
    app = build_app(_SETTINGS)
    assert isinstance(app.state.reserve_handler, ReserveHandler)


def test_build_app_wires_the_lifecycle_handlers() -> None:
    app = build_app(_SETTINGS)
    assert isinstance(app.state.commit_handler, CommitHandler)
    assert isinstance(app.state.cancel_handler, CancelHandler)
    assert isinstance(app.state.extend_handler, ExtendHandler)
    assert isinstance(app.state.grace_backfill_handler, GraceBackfillHandler)


def test_build_app_wires_the_authenticator() -> None:
    app = build_app(_SETTINGS)
    assert callable(app.state.authenticate)


def test_the_lifespan_disposes_the_engine() -> None:
    app = build_app(_SETTINGS)
    pool_before = app.state.engine.sync_engine.pool
    with TestClient(app):
        pass
    # dispose() replaces the engine's connection pool
    assert app.state.engine.sync_engine.pool is not pool_before


def test_worker_engine_builds_an_async_engine() -> None:
    engine = _worker_engine(_settings())
    assert isinstance(engine, AsyncEngine)


def test_reservation_reaper_builder_threads_settings() -> None:
    engine = build_engine("postgresql+asyncpg://x/y")
    handler = _reservation_reaper(engine, _settings())
    assert isinstance(handler, ReservationReaperHandler)
    assert handler._batch_size == 7


def test_idempotency_reaper_builder_threads_settings() -> None:
    engine = build_engine("postgresql+asyncpg://x/y")
    handler = _idempotency_reaper(engine, _settings())
    assert isinstance(handler, IdempotencyReaperHandler)
    assert handler._batch_size == 9


async def test_serve_runs_the_loop_and_disposes_the_engine() -> None:
    disposed = False

    class _FakeEngine:
        async def dispose(self) -> None:
            nonlocal disposed
            disposed = True

    class _OneShotTick:
        def __init__(self, stop: asyncio.Event) -> None:
            self._stop = stop

        async def run_once(self) -> object:
            self._stop.set()
            return None

    stop = asyncio.Event()
    await _serve(
        _OneShotTick(stop),
        interval_seconds=0,
        engine=_FakeEngine(),
        name="test",
        stop=stop,
        install_signals=False,
    )
    assert disposed is True
