"""Tests that a conforming fake satisfies the application ports.

The ports are structural (Protocols); this both documents the expected shape and
lets mypy verify a concrete implementation conforms.
"""

from __future__ import annotations

from datetime import UTC, datetime

from tollgate.application.ports import CounterStore
from tollgate.domain.ids import BudgetId

_PERIOD = datetime(2026, 6, 1, tzinfo=UTC)


class _FakeStore:
    async def ensure_period(self, budget_id: BudgetId, period_start: datetime) -> None:
        return None

    async def reserve(self, budget_id: BudgetId, period_start: datetime, amount_micro: int) -> bool:
        return amount_micro >= 0

    async def commit(
        self,
        budget_id: BudgetId,
        period_start: datetime,
        reserved_micro: int,
        actual_micro: int,
    ) -> None:
        return None

    async def release(self, budget_id: BudgetId, period_start: datetime, amount_micro: int) -> None:
        return None


async def test_fake_conforms_to_counter_store() -> None:
    store: CounterStore = _FakeStore()
    await store.ensure_period(BudgetId("b1"), _PERIOD)
    assert await store.reserve(BudgetId("b1"), _PERIOD, 10)
    await store.commit(BudgetId("b1"), _PERIOD, 10, 8)
    await store.release(BudgetId("b1"), _PERIOD, 2)
