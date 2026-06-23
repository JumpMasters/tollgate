"""PostgresReserveTransaction: the multi-budget, all-or-nothing guarded reserve (§5.2/§5.3).

A reserve gates against several budget nodes at once — the principal's ancestry path plus an
optional project budget. This walks that already-resolved applicable set in the deterministic
lock order (org < team < user < project, then scope_id), so overlapping reserves by sibling
users — which share their parent rows — acquire the shared rows in one canonical sequence and
cannot form a lock cycle (§5.3). It reuses PostgresCounterStore's single-node primitives (plan
05) and adds no SQL of its own: for each node it lazily rolls the period then issues the guarded
reserve; the first node without headroom stops the walk and is named as the binding node
(most-restrictive resolution).

The walk does not open, commit, or roll back the transaction — it reports the outcome, and the
command envelope (plans 09-10) commits on success or rolls back the partial reserves on denial.
That rollback is what makes the reserve all-or-nothing; an insufficient-budget denial rolls the
whole transaction back, so the idempotency key is not persisted and a later retry can succeed
(§5.1). The applicable set is resolved and proven non-empty upstream (resolve_applicable_set,
ADR 0020); this walk is never called with zero nodes.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncConnection

from tollgate.adapters.postgres.counter_store import PostgresCounterStore
from tollgate.domain.scopes import BudgetNode, ReserveOutcome, lock_order_key


class PostgresReserveTransaction:
    """Deterministically-ordered, all-or-nothing reserve across an applicable budget set."""

    def __init__(self, conn: AsyncConnection) -> None:
        self._store = PostgresCounterStore(conn)

    async def reserve(
        self,
        nodes: Sequence[BudgetNode],
        period_start: datetime,
        amount_micro: int,
    ) -> ReserveOutcome:
        """Reserve ``amount_micro`` on every node in lock order; name the first short one.

        ``nodes`` is sorted here by :func:`lock_order_key`, so the caller may pass the applicable
        set in any order and the canonical lock sequence still holds. Returns
        ``ReserveOutcome(ok=True)`` iff all nodes were reserved; on the first node without
        headroom returns ``ReserveOutcome(ok=False, binding_node=node)`` and stops.
        """
        for node in sorted(nodes, key=lock_order_key):
            await self._store.ensure_period(node.budget_id, period_start)
            if not await self._store.reserve(node.budget_id, period_start, amount_micro):
                return ReserveOutcome(ok=False, binding_node=node)
        return ReserveOutcome(ok=True)
