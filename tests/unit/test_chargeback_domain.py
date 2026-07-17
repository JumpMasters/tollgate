"""Unit tests for the chargeback read view and its reserved-inclusive alert logic (section 2, 3)."""

from __future__ import annotations

from datetime import UTC, datetime

from tollgate.domain.chargeback import (
    BudgetState,
    BudgetStatesView,
    crossed_thresholds,
    remaining_micro,
    spent_micro,
    utilization_pct,
)
from tollgate.domain.ids import BudgetId
from tollgate.domain.invariants import Balance
from tollgate.domain.scopes import ScopeKind, ScopeRef


def _state(
    *,
    limit: int,
    reserved: int = 0,
    committed: int = 0,
    overage: int = 0,
    thresholds: tuple[int, ...] = (),
) -> BudgetState:
    return BudgetState(
        budget_id=BudgetId("b1"),
        scope_kind=ScopeKind.USER,
        scope_id="u1",
        balance=Balance(
            limit_micro=limit,
            reserved_micro=reserved,
            committed_micro=committed,
            overage_micro=overage,
        ),
        alert_thresholds_pct=thresholds,
    )


def test_spent_is_reserved_inclusive_and_remaining_is_headroom() -> None:
    state = _state(limit=1000, reserved=200, committed=100, overage=0)
    assert spent_micro(state) == 300
    assert remaining_micro(state) == 700
    assert utilization_pct(state) == 30


def test_utilization_exceeds_100_under_overage_and_remaining_goes_negative() -> None:
    state = _state(limit=1000, reserved=0, committed=900, overage=200)
    assert spent_micro(state) == 1100
    assert remaining_micro(state) == -100
    assert utilization_pct(state) == 110


def test_non_positive_limit_is_zero_utilization_and_no_alerts() -> None:
    state = _state(limit=0, thresholds=(50, 100))
    assert utilization_pct(state) == 0
    assert crossed_thresholds(state) == ()


def test_threshold_crosses_exactly_at_the_boundary() -> None:
    # spent 800 / limit 1000 == 80%: 50 and 80 are crossed (>= is inclusive), 95 is not.
    state = _state(limit=1000, reserved=500, committed=300, thresholds=(95, 50, 80))
    assert crossed_thresholds(state) == (50, 80)


def test_crossed_thresholds_are_returned_ascending_and_include_all_reached_under_overage() -> None:
    state = _state(limit=1000, committed=1000, overage=100, thresholds=(100, 50))
    assert crossed_thresholds(state) == (50, 100)


def test_view_carries_the_period_and_states() -> None:
    state = _state(limit=1000)
    view = BudgetStatesView(period_start=datetime(2026, 7, 1, tzinfo=UTC), states=(state,))
    assert view.period_start == datetime(2026, 7, 1, tzinfo=UTC)
    assert view.states == (state,)


def test_scope_ref_is_a_frozen_kind_and_id() -> None:
    ref = ScopeRef(scope_kind=ScopeKind.TEAM, scope_id="t1")
    assert ref.scope_kind is ScopeKind.TEAM
    assert ref.scope_id == "t1"
