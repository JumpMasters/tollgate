"""Tests for the typed-error to HTTP mapping (ADR 0031)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

from tollgate.api.app import create_api
from tollgate.domain.errors import (
    AmountOutOfRange,
    AuthenticationFailed,
    BalanceGuardViolation,
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


def _client_raising(exc: Exception) -> TestClient:
    app = create_api()

    @app.get("/boom")
    async def boom() -> None:
        raise exc

    return TestClient(app, raise_server_exceptions=False)


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
        (BalanceGuardViolation(), 500, "balance_guard_violation"),
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


@pytest.mark.parametrize(
    "exc",
    [
        ConnectionRefusedError(
            "connection refused"
        ),  # raw asyncpg connect failure (the common case)
        TimeoutError("connect timed out"),  # ETIMEDOUT, an OSError subclass
        OperationalError("SELECT 1", {}, Exception("statement timeout")),
        InterfaceError("SELECT 1", {}, Exception("connection is closed")),
        SQLAlchemyTimeoutError("QueuePool limit of size 5 overflow 10 reached"),
    ],
)
def test_datastore_connectivity_errors_fail_closed_to_503(exc: Exception) -> None:
    # A datastore outage means no enforcement decision was made: it must surface as the
    # documented 503 EnforcementUnavailable envelope, not an off-contract 500 (#62).
    response = _client_raising(exc).get("/boom")
    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "enforcement_unavailable"
    assert body["error"]["message"]


def test_unmapped_subtypes_fail_closed_to_500() -> None:
    class _Novel(TollgateError):
        """A TollgateError subtype the mapping has never heard of."""

    response = _client_raising(_Novel()).get("/boom")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
