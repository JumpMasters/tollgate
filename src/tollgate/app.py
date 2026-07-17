"""Composition root.

The only module that imports concrete adapters and wires them to the application.
Everything else depends on ports. The import-linter contracts in
``pyproject.toml`` keep that boundary honest.
"""

from __future__ import annotations

from fastapi import FastAPI

from tollgate.adapters.clock import SystemClock
from tollgate.adapters.postgres.engine import build_engine
from tollgate.adapters.postgres.identifiers import Uuid7IdGenerator
from tollgate.adapters.postgres.unit_of_work import PostgresUnitOfWork
from tollgate.api.app import create_api
from tollgate.application.handlers.cancel import CancelHandler
from tollgate.application.handlers.commit import CommitHandler
from tollgate.application.handlers.extend import ExtendHandler
from tollgate.application.handlers.grace import GraceBackfillHandler
from tollgate.application.handlers.reserve import ReserveHandler
from tollgate.config.settings import Settings, load_settings


def build_app(settings: Settings | None = None) -> FastAPI:
    """Wire settings, the datastore engine, and the HTTP surface together."""
    settings = settings or load_settings()
    engine = build_engine(
        settings.database_url,
        statement_timeout_ms=settings.reserve_statement_timeout_ms,
    )
    app = create_api()
    app.state.engine = engine
    app.state.settings = settings
    uow = PostgresUnitOfWork(engine)
    clock = SystemClock()
    ids = Uuid7IdGenerator()
    app.state.reserve_handler = ReserveHandler(
        uow=uow,
        clock=clock,
        ids=ids,
        reservation_ttl_seconds=settings.reservation_ttl_seconds,
    )
    app.state.commit_handler = CommitHandler(uow=uow, ids=ids)
    app.state.cancel_handler = CancelHandler(uow=uow, ids=ids)
    app.state.extend_handler = ExtendHandler(
        uow=uow,
        clock=clock,
        reservation_ttl_seconds=settings.reservation_ttl_seconds,
    )
    app.state.grace_backfill_handler = GraceBackfillHandler(uow=uow, clock=clock, ids=ids)
    return app
