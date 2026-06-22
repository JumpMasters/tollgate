"""Tests that a conforming fake satisfies the application ports.

The ports are structural (Protocols); this both documents the expected shape and
lets mypy verify a concrete implementation conforms.
"""

from __future__ import annotations

from tollgate.application.ports import CounterStore
from tollgate.domain.ids import BudgetId


class _FakeStore:
    async def reserve(self, budget_id: BudgetId, period_start: str, amount_micro: int) -> bool:
        return amount_micro >= 0

    async def commit(
        self,
        budget_id: BudgetId,
        period_start: str,
        reserved_micro: int,
        actual_micro: int,
    ) -> None:
        return None

    async def release(self, budget_id: BudgetId, period_start: str, amount_micro: int) -> None:
        return None


async def test_fake_conforms_to_counter_store() -> None:
    store: CounterStore = _FakeStore()
    assert await store.reserve(BudgetId("b1"), "2026-06-01", 10)
    await store.commit(BudgetId("b1"), "2026-06-01", 10, 8)
    await store.release(BudgetId("b1"), "2026-06-01", 2)
