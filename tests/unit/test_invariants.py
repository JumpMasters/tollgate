"""Tests for the shared spend-invariant predicates (oracle + stateful tests)."""

from __future__ import annotations

from tollgate.domain.invariants import (
    Balance,
    amounts_non_negative,
    can_reserve,
    committed_within_limit,
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
