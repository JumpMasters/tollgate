"""Tests for the SDK client error taxonomy (mapped from the ADR-0031 envelope)."""

from __future__ import annotations

import pytest

from tollgate.adapters.integrations.sdk.errors import (
    AuthenticationFailed,
    BudgetDenied,
    EnforcementUnavailable,
    IdempotencyKeyReuse,
    InternalError,
    InvalidRequest,
    NotAuthorized,
    ReservationNotHeld,
    TollgateApiError,
    UnknownModel,
    error_for,
)


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [
        (401, "authentication_failed", AuthenticationFailed),
        (402, "insufficient_budget", BudgetDenied),
        (403, "scope_not_authorized", NotAuthorized),
        (403, "budget_not_found", NotAuthorized),
        (409, "idempotency_key_reuse", IdempotencyKeyReuse),
        (409, "reservation_not_held", ReservationNotHeld),
        (422, "unknown_model", UnknownModel),
        (422, "amount_out_of_range", InvalidRequest),
        (503, "enforcement_unavailable", EnforcementUnavailable),
        (500, "internal_error", InternalError),
        (500, "conflicting_budget_scope", InternalError),
        (500, "balance_guard_violation", InternalError),
    ],
)
def test_error_for_maps_status_and_code(status: int, code: str, expected: type) -> None:
    err = error_for(status, code, "boom")
    assert isinstance(err, expected)
    assert isinstance(err, TollgateApiError)
    assert err.status == status
    assert err.code == code
    assert "boom" in str(err)


def test_unknown_status_falls_back_to_internal_error() -> None:
    err = error_for(504, None, "gateway timeout")
    assert isinstance(err, EnforcementUnavailable)  # 5xx without a known code fails closed
