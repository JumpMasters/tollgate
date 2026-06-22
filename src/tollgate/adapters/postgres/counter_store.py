"""PostgresCounterStore: invariant-guarded budget-balance primitives (§5.2).

Single-node ``reserve`` / ``commit`` / ``release`` over ``budget_balance``, plus a
lazy period-roll. Written as explicit SQLAlchemy Core statements — the ``WHERE``
clause is the guard, so an over-budget reserve matches zero rows and is denied with
no read-modify-write gap and no version column. The store binds the active command
transaction's connection; the multi-budget orchestration (deterministic order,
all-or-nothing) lives a layer up (plan 07).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import literal, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from tollgate.adapters.postgres.schema import budget, budget_balance
from tollgate.domain.ids import BudgetId


class PostgresCounterStore:
    """Guarded conditional writes over ``budget_balance`` on one bound connection."""

    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def ensure_period(self, budget_id: BudgetId, period_start: datetime) -> None:
        """Create the period's balance row, seeded from the budget's limit (§5.3/§5.5).

        ``INSERT … SELECT hard_limit_micro FROM budget … ON CONFLICT DO NOTHING`` so a
        concurrent first-reserver in the same new period converges on one row instead
        of failing.
        """
        seed = select(
            budget.c.budget_id,
            literal(period_start).label("period_start"),
            budget.c.hard_limit_micro.label("limit_micro"),
            literal(0).label("reserved_micro"),
            literal(0).label("committed_micro"),
            literal(0).label("overage_micro"),
        ).where(budget.c.budget_id == budget_id)
        stmt = (
            pg_insert(budget_balance)
            .from_select(
                [
                    "budget_id",
                    "period_start",
                    "limit_micro",
                    "reserved_micro",
                    "committed_micro",
                    "overage_micro",
                ],
                seed,
            )
            .on_conflict_do_nothing(index_elements=["budget_id", "period_start"])
        )
        await self._conn.execute(stmt)

    async def reserve(self, budget_id: BudgetId, period_start: datetime, amount_micro: int) -> bool:
        """Guarded reserve: succeed iff the node has headroom (§5.2).

        ``remaining = limit - reserved - committed - overage``; the conditional
        ``WHERE`` is the guard, so zero rows updated means no headroom → denied.
        """
        stmt = (
            update(budget_balance)
            .where(
                budget_balance.c.budget_id == budget_id,
                budget_balance.c.period_start == period_start,
                budget_balance.c.limit_micro
                - budget_balance.c.reserved_micro
                - budget_balance.c.committed_micro
                - budget_balance.c.overage_micro
                >= amount_micro,
            )
            .values(reserved_micro=budget_balance.c.reserved_micro + amount_micro)
        )
        result = await self._conn.execute(stmt)
        return result.rowcount == 1

    async def commit(
        self,
        budget_id: BudgetId,
        period_start: datetime,
        reserved_micro: int,
        actual_micro: int,
    ) -> None:
        """Reconcile: move at most the reserved estimate; record any excess as overage.

        Mirrors the §5.2 commit guard. ``reserved_micro >= :est`` keeps ``reserved``
        non-negative; the committed / overage split is ``LEAST`` / ``GREATEST``.
        ``actual_micro`` and ``reserved_micro`` are both known scalars here, so the
        split is computed directly (matching ``domain.pricing.reconcile``).
        """
        committed_delta = min(actual_micro, reserved_micro)
        overage_delta = max(actual_micro - reserved_micro, 0)
        stmt = (
            update(budget_balance)
            .where(
                budget_balance.c.budget_id == budget_id,
                budget_balance.c.period_start == period_start,
                budget_balance.c.reserved_micro >= reserved_micro,
            )
            .values(
                reserved_micro=budget_balance.c.reserved_micro - reserved_micro,
                committed_micro=budget_balance.c.committed_micro + committed_delta,
                overage_micro=budget_balance.c.overage_micro + overage_delta,
            )
        )
        await self._conn.execute(stmt)

    async def release(self, budget_id: BudgetId, period_start: datetime, amount_micro: int) -> None:
        """Release a held estimate back to the node; the guard keeps reserved >= 0."""
        stmt = (
            update(budget_balance)
            .where(
                budget_balance.c.budget_id == budget_id,
                budget_balance.c.period_start == period_start,
                budget_balance.c.reserved_micro >= amount_micro,
            )
            .values(reserved_micro=budget_balance.c.reserved_micro - amount_micro)
        )
        await self._conn.execute(stmt)
