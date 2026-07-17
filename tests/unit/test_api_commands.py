"""Tests for the command routes (sections 4-5, ADR 0031)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tollgate.api.app import create_api
from tollgate.application.auth import AuthContext
from tollgate.domain.commands import (
    CancelCommand,
    CancelResult,
    CommitCommand,
    CommitResult,
    ExtendCommand,
    ExtendResult,
    GraceBackfillCommand,
    GraceBackfillResult,
    ProviderUsage,
    ReserveCommand,
    ReserveResult,
)
from tollgate.domain.credentials import Credential, CredentialStatus, Principal
from tollgate.domain.errors import InsufficientBudget
from tollgate.domain.ids import CredentialId, OrgId, PrincipalId, ReservationId, TeamId, UserId
from tollgate.domain.scopes import ScopeKind

_DEADLINE = datetime(2026, 6, 23, 12, 10, tzinfo=UTC)
_AUTH_HEADERS = {"Authorization": "Bearer tok-1", "Idempotency-Key": "idem-1"}
_RESERVE_BODY = {
    "provider": "anthropic",
    "model": "claude",
    "input_bound_tokens": 100,
    "max_output_tokens": 100,
    "labels": {"env": "prod"},
}


def _auth_context() -> AuthContext:
    credential = Credential(
        credential_id=CredentialId("cred-1"),
        principal_id=PrincipalId("u1"),
        scope_kind=ScopeKind.USER,
        scope_id="u1",
        status=CredentialStatus.ACTIVE,
    )
    principal = Principal(user_id=UserId("u1"), team_id=TeamId("t1"), org_id=OrgId("o1"))
    return AuthContext(credential=credential, principal=principal)


async def _authenticate(presented_token: str) -> AuthContext:
    return _auth_context()


def _app(**state: Any) -> FastAPI:
    app = create_api()
    app.state.authenticate = _authenticate
    for name, value in state.items():
        setattr(app.state, name, value)
    return app


class _StubReserve:
    def __init__(self) -> None:
        self.calls: list[tuple[AuthContext, ReserveCommand]] = []

    async def reserve(self, auth: AuthContext, command: ReserveCommand) -> ReserveResult:
        self.calls.append((auth, command))
        return ReserveResult(
            reservation_id=ReservationId("r1"),
            estimated_micro=300,
            price_book_version="pb-1",
            ttl_deadline=_DEADLINE,
        )


class _StubCommit:
    def __init__(self) -> None:
        self.calls: list[tuple[AuthContext, CommitCommand]] = []

    async def commit(self, auth: AuthContext, command: CommitCommand) -> CommitResult:
        self.calls.append((auth, command))
        return CommitResult(
            reservation_id=ReservationId("r1"), committed_micro=200, overage_micro=0
        )


class _StubCancel:
    def __init__(self) -> None:
        self.calls: list[tuple[AuthContext, CancelCommand]] = []

    async def cancel(self, auth: AuthContext, command: CancelCommand) -> CancelResult:
        self.calls.append((auth, command))
        return CancelResult(reservation_id=ReservationId("r1"), released_micro=300)


class _StubExtend:
    def __init__(self) -> None:
        self.calls: list[tuple[AuthContext, ExtendCommand]] = []

    async def extend(self, auth: AuthContext, command: ExtendCommand) -> ExtendResult:
        self.calls.append((auth, command))
        return ExtendResult(reservation_id=ReservationId("r1"), ttl_deadline=_DEADLINE)


class _StubGrace:
    def __init__(self) -> None:
        self.calls: list[tuple[AuthContext, GraceBackfillCommand]] = []

    async def backfill(
        self, auth: AuthContext, command: GraceBackfillCommand
    ) -> GraceBackfillResult:
        self.calls.append((auth, command))
        return GraceBackfillResult(actual_micro=200, price_book_version="pb-1")


def test_reserve_translates_the_wire_into_the_command() -> None:
    stub = _StubReserve()
    client = TestClient(_app(reserve_handler=stub))
    response = client.post("/v1/reserve", json=_RESERVE_BODY, headers=_AUTH_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["reservation_id"] == "r1"
    assert body["estimated_micro"] == 300
    assert body["price_book_version"] == "pb-1"
    assert datetime.fromisoformat(body["ttl_deadline"]) == _DEADLINE
    (auth, command) = stub.calls[0]
    assert auth.principal.user_id == "u1"
    assert command == ReserveCommand(
        idempotency_key="idem-1",
        provider="anthropic",
        model="claude",
        input_bound_tokens=100,
        max_output_tokens=100,
        labels={"env": "prod"},
        project_id=None,
    )


def test_reserve_requires_the_idempotency_key_header() -> None:
    stub = _StubReserve()
    client = TestClient(_app(reserve_handler=stub))
    response = client.post(
        "/v1/reserve", json=_RESERVE_BODY, headers={"Authorization": "Bearer tok-1"}
    )
    assert response.status_code == 422
    assert stub.calls == []


def test_reserve_rejects_unknown_body_fields() -> None:
    stub = _StubReserve()
    client = TestClient(_app(reserve_handler=stub))
    body = {**_RESERVE_BODY, "max_output_token": 5}
    response = client.post("/v1/reserve", json=body, headers=_AUTH_HEADERS)
    assert response.status_code == 422
    assert stub.calls == []


def test_reserve_requires_authentication() -> None:
    stub = _StubReserve()
    client = TestClient(_app(reserve_handler=stub))
    response = client.post("/v1/reserve", json=_RESERVE_BODY, headers={"Idempotency-Key": "idem-1"})
    assert response.status_code == 401
    assert stub.calls == []


def test_a_domain_error_from_the_handler_maps_through_the_envelope() -> None:
    class _Denying:
        async def reserve(self, auth: AuthContext, command: ReserveCommand) -> ReserveResult:
            raise InsufficientBudget("user:u1")

    client = TestClient(_app(reserve_handler=_Denying()))
    response = client.post("/v1/reserve", json=_RESERVE_BODY, headers=_AUTH_HEADERS)
    assert response.status_code == 402
    assert response.json()["error"]["message"] == "insufficient budget at user:u1"


def test_commit_translates_the_wire_into_the_command() -> None:
    stub = _StubCommit()
    client = TestClient(_app(commit_handler=stub))
    response = client.post(
        "/v1/commit",
        json={"reservation_id": "r1", "usage": {"input_tokens": 100, "output_tokens": 50}},
        headers=_AUTH_HEADERS,
    )
    assert response.status_code == 200
    assert response.json() == {"reservation_id": "r1", "committed_micro": 200, "overage_micro": 0}
    (_, command) = stub.calls[0]
    assert command == CommitCommand(
        idempotency_key="idem-1",
        reservation_id=ReservationId("r1"),
        usage=ProviderUsage(input_tokens=100, output_tokens=50, cached_input_tokens=0),
    )


def test_cancel_translates_the_wire_into_the_command() -> None:
    stub = _StubCancel()
    client = TestClient(_app(cancel_handler=stub))
    response = client.post("/v1/cancel", json={"reservation_id": "r1"}, headers=_AUTH_HEADERS)
    assert response.status_code == 200
    assert response.json() == {"reservation_id": "r1", "released_micro": 300}
    (_, command) = stub.calls[0]
    assert command == CancelCommand(idempotency_key="idem-1", reservation_id=ReservationId("r1"))


def test_extend_needs_no_idempotency_key() -> None:
    stub = _StubExtend()
    client = TestClient(_app(extend_handler=stub))
    response = client.post(
        "/v1/extend",
        json={"reservation_id": "r1"},
        headers={"Authorization": "Bearer tok-1"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["reservation_id"] == "r1"
    assert datetime.fromisoformat(body["ttl_deadline"]) == _DEADLINE
    (_, command) = stub.calls[0]
    assert command == ExtendCommand(reservation_id=ReservationId("r1"))


def test_grace_backfill_translates_the_wire_into_the_command() -> None:
    stub = _StubGrace()
    client = TestClient(_app(grace_backfill_handler=stub))
    response = client.post(
        "/v1/grace-backfill",
        json={
            "provider": "anthropic",
            "model": "claude",
            "usage": {"input_tokens": 100, "output_tokens": 50},
        },
        headers=_AUTH_HEADERS,
    )
    assert response.status_code == 200
    assert response.json() == {"actual_micro": 200, "price_book_version": "pb-1"}
    (_, command) = stub.calls[0]
    assert command == GraceBackfillCommand(
        idempotency_key="idem-1",
        provider="anthropic",
        model="claude",
        usage=ProviderUsage(input_tokens=100, output_tokens=50, cached_input_tokens=0),
        project_id=None,
    )
