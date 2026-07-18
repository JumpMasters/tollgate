"""Tests for the typed-error to HTTP mapping (ADR 0031)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tollgate.api.app import create_api
from tollgate.domain.errors import (
    AmountOutOfRange,
    AuthenticationFailed,
    BudgetNotFound,
    ConflictingBudgetScope,
    EnforcementUnavailable,
    IdempotencyKeyReuse,
    InsufficientBudget,
    ReservationNotHeld,
    ScopeNotAuthorized,
    TollgateError,
    UnknownModel,
)


def _client_raising(exc: TollgateError) -> TestClient:
    app = create_api()

    @app.get("/boom")
    async def boom() -> None:
        raise exc

    return TestClient(app)


@pytest.mark.parametrize(
    ("exc", "status", "code"),
    [
        (AuthenticationFailed(), 401, "authentication_failed"),
        (InsufficientBudget("user:u1"), 402, "insufficient_budget"),
        (ScopeNotAuthorized("project:p1"), 403, "scope_not_authorized"),
        (BudgetNotFound("no budget governs the request"), 403, "budget_not_found"),
        (IdempotencyKeyReuse(), 409, "idempotency_key_reuse"),
        (ReservationNotHeld(), 409, "reservation_not_held"),
        (UnknownModel("anthropic", "unpriced"), 422, "unknown_model"),
        (AmountOutOfRange("amount out of range"), 422, "amount_out_of_range"),
        (ConflictingBudgetScope("user", "u1"), 500, "conflicting_budget_scope"),
        (EnforcementUnavailable(), 503, "enforcement_unavailable"),
    ],
)
def test_every_domain_error_maps_to_its_status_and_code(
    exc: TollgateError, status: int, code: str
) -> None:
    response = _client_raising(exc).get("/boom")
    assert response.status_code == status
    body = response.json()
    assert body["error"]["code"] == code
    assert body["error"]["message"]


def test_authentication_failure_advertises_the_bearer_scheme() -> None:
    response = _client_raising(AuthenticationFailed()).get("/boom")
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_the_message_prefers_the_exception_text() -> None:
    response = _client_raising(InsufficientBudget("user:u1")).get("/boom")
    assert response.json()["error"]["message"] == "insufficient budget at user:u1"


def test_bare_raises_get_a_default_message() -> None:
    response = _client_raising(IdempotencyKeyReuse()).get("/boom")
    message = response.json()["error"]["message"]
    assert message == "idempotency key reused with a different command"


def test_unmapped_subtypes_fail_closed_to_500() -> None:
    class _Novel(TollgateError):
        """A TollgateError subtype the mapping has never heard of."""

    response = _client_raising(_Novel()).get("/boom")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
