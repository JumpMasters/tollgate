"""Tests for the domain command value types (reserve / commit / cancel / extend)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tollgate.domain.commands import (
    CancelCommand,
    CancelResult,
    CommitCommand,
    CommitResult,
    ExtendCommand,
    ExtendResult,
    GraceBackfillCommand,
    GraceBackfillResult,
    MeterCommand,
    MeterResult,
    ProviderUsage,
    ReserveCommand,
    ReserveResult,
)
from tollgate.domain.ids import ProjectId, ReservationId


def test_reserve_command_carries_the_request_fields() -> None:
    cmd = ReserveCommand(
        idempotency_key="idem-1",
        provider="anthropic",
        model="claude-opus-4-8",
        input_bound_tokens=1200,
        max_output_tokens=400,
        labels={"team": "payments"},
    )
    assert cmd.provider == "anthropic"
    assert cmd.input_bound_tokens == 1200
    assert cmd.labels == {"team": "payments"}
    assert cmd.project_id is None  # project is optional


def test_reserve_command_accepts_an_authorized_project() -> None:
    cmd = ReserveCommand(
        idempotency_key="idem-1",
        provider="anthropic",
        model="claude-opus-4-8",
        input_bound_tokens=10,
        max_output_tokens=10,
        labels={},
        project_id=ProjectId("proj-7"),
    )
    assert cmd.project_id == ProjectId("proj-7")


def test_reserve_command_is_immutable() -> None:
    cmd = ReserveCommand(
        idempotency_key="idem-1",
        provider="anthropic",
        model="claude-opus-4-8",
        input_bound_tokens=10,
        max_output_tokens=10,
        labels={},
    )
    with pytest.raises(AttributeError):
        cmd.provider = "openai"  # type: ignore[misc]


def test_reserve_command_equality_is_by_value() -> None:
    a = ReserveCommand(
        idempotency_key="idem-1",
        provider="anthropic",
        model="claude-opus-4-8",
        input_bound_tokens=10,
        max_output_tokens=10,
        labels={},
    )
    b = ReserveCommand(
        idempotency_key="idem-1",
        provider="anthropic",
        model="claude-opus-4-8",
        input_bound_tokens=10,
        max_output_tokens=10,
        labels={},
    )
    assert a == b


def test_provider_usage_defaults_cached_tokens_to_zero() -> None:
    usage = ProviderUsage(input_tokens=1000, output_tokens=200)
    assert usage.cached_input_tokens == 0


def test_commit_command_carries_usage_and_reservation() -> None:
    usage = ProviderUsage(input_tokens=1000, output_tokens=200, cached_input_tokens=300)
    cmd = CommitCommand(
        idempotency_key="idem-2",
        reservation_id=ReservationId("rsv-1"),
        usage=usage,
    )
    assert cmd.reservation_id == ReservationId("rsv-1")
    assert cmd.usage.cached_input_tokens == 300


def test_cancel_command_targets_a_reservation() -> None:
    cmd = CancelCommand(idempotency_key="idem-3", reservation_id=ReservationId("rsv-1"))
    assert cmd.reservation_id == ReservationId("rsv-1")


def test_extend_command_needs_no_idempotency_key() -> None:
    # extend is a monotonic heartbeat -- naturally idempotent (§4), so it carries no
    # idempotency key, only the reservation to advance.
    cmd = ExtendCommand(reservation_id=ReservationId("rsv-1"))
    assert cmd.reservation_id == ReservationId("rsv-1")
    assert not hasattr(cmd, "idempotency_key")


def test_reserve_result_reports_estimate_and_price_version() -> None:
    result = ReserveResult(
        reservation_id=ReservationId("rsv-1"),
        estimated_micro=4_200,
        price_book_version="2026-06-22",
        ttl_deadline=datetime(2026, 6, 22, 12, 0, tzinfo=UTC),
    )
    assert result.estimated_micro == 4_200
    assert result.price_book_version == "2026-06-22"


def test_commit_result_splits_committed_and_overage() -> None:
    result = CommitResult(
        reservation_id=ReservationId("rsv-1"),
        committed_micro=4_000,
        overage_micro=200,
    )
    # actual == committed + overage by construction (§4 reconciliation).
    assert result.committed_micro + result.overage_micro == 4_200


def test_cancel_result_reports_the_released_estimate() -> None:
    result = CancelResult(reservation_id=ReservationId("rsv-1"), released_micro=4_200)
    assert result.released_micro == 4_200


def test_extend_result_carries_the_new_deadline() -> None:
    deadline = datetime(2026, 6, 22, 12, 10, tzinfo=UTC)
    result = ExtendResult(reservation_id=ReservationId("rsv-1"), ttl_deadline=deadline)
    assert result.ttl_deadline == deadline


def test_result_types_are_immutable() -> None:
    result = CancelResult(reservation_id=ReservationId("rsv-1"), released_micro=1)
    with pytest.raises(AttributeError):
        result.released_micro = 2  # type: ignore[misc]


def test_grace_backfill_command_carries_usage_and_optional_project() -> None:
    usage = ProviderUsage(input_tokens=100, output_tokens=50)
    cmd = GraceBackfillCommand(
        idempotency_key="idem-4",
        provider="anthropic",
        model="claude-opus-4-8",
        usage=usage,
        project_id=ProjectId("proj-7"),
    )
    assert cmd.usage.output_tokens == 50
    assert cmd.project_id == ProjectId("proj-7")
    unprojected = GraceBackfillCommand(
        idempotency_key="idem-4", provider="anthropic", model="claude-opus-4-8", usage=usage
    )
    assert unprojected.project_id is None


def test_grace_backfill_result_reports_cost_and_price_basis() -> None:
    result = GraceBackfillResult(actual_micro=4_200, price_book_version="2026-06-22")
    assert result.actual_micro == 4_200
    assert result.price_book_version == "2026-06-22"


def test_meter_command_copies_labels_read_only() -> None:
    src = {"env": "prod"}
    cmd = MeterCommand(
        idempotency_key="k1",
        provider="anthropic",
        model="claude",
        usage=ProviderUsage(input_tokens=100, output_tokens=50),
        labels=src,
    )
    src["env"] = "dev"  # mutating the caller's dict must not change the command
    assert cmd.labels == {"env": "prod"}
    assert cmd.project_id is None and cmd.truncated is False
    with pytest.raises(TypeError):
        cmd.labels["x"] = "y"  # type: ignore[index]


def test_meter_result_fields() -> None:
    r = MeterResult(actual_micro=240, price_book_version="pb-1")
    assert (r.actual_micro, r.price_book_version) == (240, "pb-1")


def test_reserve_command_labels_are_an_immutable_copy() -> None:
    # frozen prevents rebinding, not mutation: the labels must be a read-only copy so mutating the
    # caller's dict after construction cannot alter the (already fingerprinted) command (#78).
    source = {"env": "prod"}
    command = ReserveCommand(
        idempotency_key="k",
        provider="p",
        model="m",
        input_bound_tokens=1,
        max_output_tokens=1,
        labels=source,
    )
    source["env"] = "changed"
    assert command.labels == {"env": "prod"}  # unaffected by the later mutation
    with pytest.raises(TypeError):
        command.labels["env"] = "x"  # type: ignore[index]  # a read-only view, not the live dict
