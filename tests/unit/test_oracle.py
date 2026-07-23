"""Unit tests for the pure oracle core: given rows, does it flag the right violations?"""

from __future__ import annotations

from datetime import UTC, datetime

from loadtest.oracle import Check, LedgerRow, OracleReport, Violation, evaluate

from tollgate.domain.invariants import Balance

_P = datetime(2026, 6, 1, tzinfo=UTC)


def test_clean_run_has_no_violations() -> None:
    balances = {
        ("b1", _P): Balance(limit_micro=100, reserved_micro=10, committed_micro=20, overage_micro=0)
    }
    rows = [
        LedgerRow("b1", _P, "r1", 10, 0, 0),  # r1 still held: +10 reserved
        LedgerRow("b1", _P, "r2", 20, 0, 0),  # r2 reserve
        LedgerRow("b1", _P, "r2", -20, 20, 0),  # r2 commit: -20 reserved, +20 committed
    ]
    report = evaluate(balances=balances, ledger_rows=rows, reservations=[], tree_edges=[])
    assert isinstance(report, OracleReport)
    assert report.ok
    assert report.violations == ()


def test_negative_amount_is_flagged() -> None:
    balances = {("b1", _P): Balance(100, -1, 0, 0)}
    report = evaluate(
        balances=balances,
        ledger_rows=[],
        reservations=[],
        tree_edges=[],
        checks=frozenset({Check.NON_NEGATIVE}),
    )
    assert [v.check for v in report.violations] == [Check.NON_NEGATIVE]


def test_breach_is_flagged() -> None:
    balances = {("b1", _P): Balance(100, 0, 150, 0)}
    report = evaluate(
        balances=balances,
        ledger_rows=[],
        reservations=[],
        tree_edges=[],
        checks=frozenset({Check.NO_BREACH}),
    )
    assert [v.check for v in report.violations] == [Check.NO_BREACH]


def test_storage_guard_is_flagged() -> None:
    balances = {("b1", _P): Balance(100, 80, 40, 0)}  # 120 > 100
    report = evaluate(
        balances=balances,
        ledger_rows=[],
        reservations=[],
        tree_edges=[],
        checks=frozenset({Check.STORAGE_GUARD}),
    )
    assert [v.check for v in report.violations] == [Check.STORAGE_GUARD]


def test_conservation_mismatch_is_flagged() -> None:
    balances = {("b1", _P): Balance(100, 10, 0, 0)}  # says reserved=10
    rows = [LedgerRow("b1", _P, "r1", 5, 0, 0)]  # ledger sums to 5
    report = evaluate(
        balances=balances,
        ledger_rows=rows,
        reservations=[],
        tree_edges=[],
        checks=frozenset({Check.CONSERVATION}),
    )
    assert [v.check for v in report.violations] == [Check.CONSERVATION]


def test_check_subset_skips_other_checks() -> None:
    balances = {("b1", _P): Balance(100, 0, 150, 0)}  # would breach
    report = evaluate(
        balances=balances,
        ledger_rows=[],
        reservations=[],
        tree_edges=[],
        checks=frozenset({Check.NON_NEGATIVE}),  # breach check disabled
    )
    assert report.ok


def test_violation_carries_scope_and_detail() -> None:
    balances = {("b1", _P): Balance(100, 0, 150, 0)}
    report = evaluate(
        balances=balances,
        ledger_rows=[],
        reservations=[],
        tree_edges=[],
        checks=frozenset({Check.NO_BREACH}),
    )
    (violation,) = report.violations
    assert isinstance(violation, Violation)
    assert "b1" in violation.scope
    assert "150" in violation.detail
