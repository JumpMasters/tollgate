"""Composition root.

The only module that imports concrete adapters and wires them to the application.
Everything else depends on ports. The import-linter contracts in
``pyproject.toml`` keep that boundary honest.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from tollgate.adapters.clock import SystemClock
from tollgate.adapters.postgres.credential_repo import PostgresCredentialRepository
from tollgate.adapters.postgres.engine import build_engine
from tollgate.adapters.postgres.identifiers import Uuid7IdGenerator
from tollgate.adapters.postgres.unit_of_work import PostgresUnitOfWork
from tollgate.api.app import create_api
from tollgate.application.auth import AuthContext, CredentialAuthenticator
from tollgate.application.handlers.cancel import CancelHandler
from tollgate.application.handlers.commit import CommitHandler
from tollgate.application.handlers.extend import ExtendHandler
from tollgate.application.handlers.grace import GraceBackfillHandler
from tollgate.application.handlers.reserve import ReserveHandler
from tollgate.config.settings import Settings, load_settings


def _authenticator(
    engine: AsyncEngine, *, token_secret: str
) -> Callable[[str], Awaitable[AuthContext]]:
    """Authenticate each bearer token on its own short-lived connection (section 5.0).

    Authentication is a precondition outside the command transaction, so it
    borrows a pooled connection just long enough for the credential lookup.
    """

    async def authenticate(presented_token: str) -> AuthContext:
        async with engine.connect() as conn:
            authenticator = CredentialAuthenticator(
                PostgresCredentialRepository(conn), token_secret=token_secret
            )
            return await authenticator.authenticate(presented_token)

    return authenticate


def build_app(settings: Settings | None = None) -> FastAPI:
    """Wire settings, the datastore engine, and the HTTP surface together."""
    settings = settings or load_settings()
    if not settings.token_hash_secret:
        msg = "TOLLGATE_TOKEN_HASH_SECRET must be set: it keys bearer-token hashing (ADR 0026)"
        raise ValueError(msg)
    engine = build_engine(
        settings.database_url,
        statement_timeout_ms=settings.reserve_statement_timeout_ms,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await engine.dispose()

    app = create_api(lifespan=lifespan)
    app.state.engine = engine
    app.state.settings = settings
    app.state.authenticate = _authenticator(engine, token_secret=settings.token_hash_secret)
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
