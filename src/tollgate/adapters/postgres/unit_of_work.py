"""PostgresUnitOfWork: the §5 command transaction envelope as a port (plan 09).

``begin()`` opens one ``engine.begin()`` transaction and yields a command context whose
repositories all bind that connection, so a command's resolution reads and guarded writes commit —
or roll back on any raised denial — as a single unit. It constructs the concrete repositories
(price book, budgets, idempotency, reservations, ledger, and the multi-budget reserve transaction).
Like the other adapters it satisfies the application's ``UnitOfWork`` / ``CommandContext`` ports
structurally and never imports ``application``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tollgate.adapters.postgres.budget_repo import PostgresBudgetRepository
from tollgate.adapters.postgres.idempotency_repo import PostgresIdempotencyRepository
from tollgate.adapters.postgres.ledger_repo import PostgresLedgerRepository
from tollgate.adapters.postgres.price_book_repo import PostgresPriceBookRepository
from tollgate.adapters.postgres.reservations_repo import PostgresReservationRepository
from tollgate.adapters.postgres.reserve_tx import PostgresReserveTransaction


class _PostgresCommandContext:
    """The command-scoped repositories bound to one connection (the CommandContext port)."""

    def __init__(self, conn: AsyncConnection) -> None:
        self.prices = PostgresPriceBookRepository(conn)
        self.budgets = PostgresBudgetRepository(conn)
        self.idempotency = PostgresIdempotencyRepository(conn)
        self.reservations = PostgresReservationRepository(conn)
        self.ledger = PostgresLedgerRepository(conn)
        self.reserve_tx = PostgresReserveTransaction(conn)


class PostgresUnitOfWork:
    """Opens the command transaction and yields its bound repositories (the UnitOfWork port)."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[_PostgresCommandContext]:
        """Open one transaction; commit on clean exit, roll back on exception (§5)."""
        async with self._engine.begin() as conn:
            yield _PostgresCommandContext(conn)
