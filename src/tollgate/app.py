"""Composition root.

The only module that imports concrete adapters and wires them to the application.
Everything else depends on ports. The import-linter contracts in
``pyproject.toml`` keep that boundary honest.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from tollgate.adapters.clock import SystemClock
from tollgate.adapters.postgres.chargeback_repo import PostgresChargebackReader
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
from tollgate.application.handlers.read import ChargebackHandler
from tollgate.application.handlers.reap import IdempotencyReaperHandler, ReservationReaperHandler
from tollgate.application.handlers.reserve import ReserveHandler
from tollgate.config.settings import Settings, load_settings
from tollgate.workers.runner import SupportsDispose, SupportsRunOnce, run_forever


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
    if not settings.token_hash_secret.get_secret_value():
        msg = "TOLLGATE_TOKEN_HASH_SECRET must be set: it keys bearer-token hashing (ADR 0026)"
        raise ValueError(msg)
    engine = build_engine(
        settings.database_url.get_secret_value(),
        statement_timeout_ms=settings.reserve_statement_timeout_ms,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await engine.dispose()

    app = create_api(lifespan=lifespan)
    app.state.engine = engine
    app.state.settings = settings
    app.state.authenticate = _authenticator(
        engine, token_secret=settings.token_hash_secret.get_secret_value()
    )
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
    app.state.chargeback_handler = ChargebackHandler(
        reader=PostgresChargebackReader(engine), clock=clock
    )
    return app


def _worker_engine(settings: Settings) -> AsyncEngine:
    """Build the datastore engine a reaper worker process runs on."""
    return build_engine(
        settings.database_url.get_secret_value(),
        statement_timeout_ms=settings.reserve_statement_timeout_ms,
    )


def _reservation_reaper(engine: AsyncEngine, settings: Settings) -> ReservationReaperHandler:
    """Wire the reservation reaper against the datastore (composition)."""
    return ReservationReaperHandler(
        uow=PostgresUnitOfWork(engine),
        clock=SystemClock(),
        ids=Uuid7IdGenerator(),
        batch_size=settings.reaper_batch_size,
    )


def _idempotency_reaper(engine: AsyncEngine, settings: Settings) -> IdempotencyReaperHandler:
    """Wire the idempotency-key reaper against the datastore (composition)."""
    return IdempotencyReaperHandler(
        uow=PostgresUnitOfWork(engine),
        clock=SystemClock(),
        ttl_hours=settings.idempotency_ttl_hours,
        batch_size=settings.idempotency_reaper_batch_size,
    )


async def _serve(
    tick: SupportsRunOnce,
    *,
    interval_seconds: float,
    engine: SupportsDispose,
    name: str,
    stop: asyncio.Event | None = None,
    install_signals: bool = True,
) -> None:
    """Run a worker tick loop until SIGINT/SIGTERM, then dispose the engine (graceful shutdown)."""
    stop = stop or asyncio.Event()
    if install_signals:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)
    try:
        await run_forever(tick, interval_seconds=interval_seconds, stop=stop, name=name)
    finally:
        await engine.dispose()


def run_reservation_reaper() -> None:  # pragma: no cover - process entrypoint
    """Console-script entrypoint: poll the reservation reaper until signalled."""
    settings = load_settings()
    engine = _worker_engine(settings)
    asyncio.run(
        _serve(
            _reservation_reaper(engine, settings),
            interval_seconds=settings.reaper_poll_interval_seconds,
            engine=engine,
            name="reservation-reaper",
        )
    )


def run_idempotency_reaper() -> None:  # pragma: no cover - process entrypoint
    """Console-script entrypoint: poll the idempotency-key reaper until signalled."""
    settings = load_settings()
    engine = _worker_engine(settings)
    asyncio.run(
        _serve(
            _idempotency_reaper(engine, settings),
            interval_seconds=settings.idempotency_reaper_poll_interval_seconds,
            engine=engine,
            name="idempotency-reaper",
        )
    )
