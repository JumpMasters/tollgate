"""Tests for the bearer authentication dependency (ADR 0031)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tollgate.api.app import create_api
from tollgate.api.dependencies import RequestAuth
from tollgate.application.auth import AuthContext
from tollgate.domain.credentials import Credential, CredentialStatus, Principal
from tollgate.domain.errors import AuthenticationFailed
from tollgate.domain.ids import CredentialId, OrgId, PrincipalId, TeamId, UserId
from tollgate.domain.scopes import ScopeKind


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


class _RecordingAuthenticator:
    def __init__(self) -> None:
        self.tokens: list[str] = []

    async def __call__(self, presented_token: str) -> AuthContext:
        self.tokens.append(presented_token)
        return _auth_context()


def _app(authenticate: object) -> FastAPI:
    app = create_api()
    app.state.authenticate = authenticate

    @app.get("/whoami")
    async def whoami(auth: RequestAuth) -> dict[str, str]:
        return {"user_id": auth.principal.user_id}

    return app


def test_a_valid_bearer_token_authenticates() -> None:
    authenticator = _RecordingAuthenticator()
    client = TestClient(_app(authenticator))
    response = client.get("/whoami", headers={"Authorization": "Bearer tok-1"})
    assert response.status_code == 200
    assert response.json() == {"user_id": "u1"}
    assert authenticator.tokens == ["tok-1"]


def test_a_missing_authorization_header_is_401() -> None:
    client = TestClient(_app(_RecordingAuthenticator()))
    response = client.get("/whoami")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_failed"
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_a_non_bearer_scheme_is_401() -> None:
    client = TestClient(_app(_RecordingAuthenticator()))
    response = client.get("/whoami", headers={"Authorization": "Basic dXNlcjpwYXNz"})
    assert response.status_code == 401


def test_an_empty_bearer_token_is_401() -> None:
    client = TestClient(_app(_RecordingAuthenticator()))
    response = client.get("/whoami", headers={"Authorization": "Bearer "})
    assert response.status_code == 401


def test_an_unrecognized_token_is_401() -> None:
    class _Rejecting:
        async def __call__(self, presented_token: str) -> AuthContext:
            raise AuthenticationFailed

    client = TestClient(_app(_Rejecting()))
    response = client.get("/whoami", headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_failed"
