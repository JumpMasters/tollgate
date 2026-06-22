"""Tests for the domain command value types (reserve / commit / cancel / extend)."""

from __future__ import annotations

import pytest

from tollgate.domain.commands import (
    CancelCommand,
    CommitCommand,
    ExtendCommand,
    ProviderUsage,
    ReserveCommand,
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
