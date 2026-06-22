"""Tests for the shared spend-invariant predicates (oracle + stateful tests)."""

from __future__ import annotations

from tollgate.domain.invariants import (
    Balance,
    LedgerDelta,
    amounts_non_negative,
    can_reserve,
    committed_rolls_up,
    committed_within_limit,
    conserves,
    node_invariants_hold,
    remaining,
    reservation_within_limit,
)


def _balance(
    *,
    limit: int = 1_000_000,
    reserved: int = 0,
    committed: int = 0,
    overage: int = 0,
) -> Balance:
    return Balance(
        limit_micro=limit,
        reserved_micro=reserved,
        committed_micro=committed,
        overage_micro=overage,
    )


def test_remaining_subtracts_every_aggregate() -> None:
    b = _balance(limit=100, reserved=10, committed=20, overage=5)
    assert remaining(b) == 65


def test_remaining_is_negative_when_overage_exceeds_headroom() -> None:
    # A commit that overran its reservation can drive remaining below zero; this is
    # the signal the node stops admitting reserves, not an invariant breach (§3).
    b = _balance(limit=100, reserved=0, committed=100, overage=50)
    assert remaining(b) == -50


def test_can_reserve_true_when_estimate_fits_headroom() -> None:
    b = _balance(limit=100, reserved=10, committed=20, overage=5)  # remaining 65
    assert can_reserve(b, 65)
    assert can_reserve(b, 64)


def test_can_reserve_false_when_estimate_exceeds_headroom() -> None:
    b = _balance(limit=100, reserved=10, committed=20, overage=5)  # remaining 65
    assert not can_reserve(b, 66)


def test_can_reserve_false_for_negative_estimate() -> None:
    assert not can_reserve(_balance(), -1)


def test_amounts_non_negative_true_for_clean_balance() -> None:
    assert amounts_non_negative(_balance(reserved=10, committed=20, overage=5))


def test_amounts_non_negative_false_when_an_aggregate_is_negative() -> None:
    assert not amounts_non_negative(_balance(reserved=-1))
    assert not amounts_non_negative(_balance(committed=-1))
    assert not amounts_non_negative(_balance(overage=-1))
    assert not amounts_non_negative(_balance(limit=-1))


def test_committed_within_limit_is_the_no_breach_guarantee() -> None:
    assert committed_within_limit(_balance(limit=100, committed=100))
    assert not committed_within_limit(_balance(limit=100, committed=101))


def test_reservation_within_limit_is_the_storage_guard() -> None:
    assert reservation_within_limit(_balance(limit=100, reserved=60, committed=40))
    assert not reservation_within_limit(_balance(limit=100, reserved=61, committed=40))


def test_node_invariants_hold_for_a_healthy_balance() -> None:
    assert node_invariants_hold(_balance(limit=100, reserved=30, committed=40, overage=10))


def test_node_invariants_hold_despite_overage_driven_negative_remaining() -> None:
    # committed <= limit and reserved + committed <= limit both hold; only remaining
    # is negative (audited overage). The node invariants must still pass (§4).
    b = _balance(limit=100, reserved=0, committed=100, overage=50)
    assert remaining(b) == -50
    assert node_invariants_hold(b)


def test_node_invariants_hold_false_on_breach() -> None:
    assert not node_invariants_hold(_balance(limit=100, committed=101))


def test_node_invariants_hold_false_on_negative_aggregate() -> None:
    assert not node_invariants_hold(_balance(reserved=-1))


def test_node_invariants_hold_false_when_reservation_exceeds_limit() -> None:
    assert not node_invariants_hold(_balance(limit=100, reserved=70, committed=40))


def test_conserves_when_deltas_sum_to_the_balance() -> None:
    b = _balance(limit=100, reserved=30, committed=40, overage=10)
    deltas = [
        LedgerDelta(delta_reserved_micro=70, delta_committed_micro=0, delta_overage_micro=0),
        LedgerDelta(delta_reserved_micro=-40, delta_committed_micro=40, delta_overage_micro=10),
    ]
    assert conserves(b, deltas)


def test_conserves_false_when_a_component_disagrees() -> None:
    b = _balance(limit=100, reserved=30, committed=40, overage=10)
    deltas = [
        LedgerDelta(delta_reserved_micro=30, delta_committed_micro=40, delta_overage_micro=9),
    ]
    assert not conserves(b, deltas)


def test_conserves_with_no_deltas_requires_a_zero_balance() -> None:
    assert conserves(_balance(limit=100), [])
    assert not conserves(_balance(limit=100, reserved=1), [])


def test_committed_rolls_up_when_parent_equals_sum_of_children() -> None:
    assert committed_rolls_up(100, [60, 40])


def test_committed_rolls_up_false_on_mismatch() -> None:
    assert not committed_rolls_up(100, [60, 30])


def test_committed_rolls_up_with_no_children_requires_zero_parent() -> None:
    assert committed_rolls_up(0, [])
    assert not committed_rolls_up(5, [])
