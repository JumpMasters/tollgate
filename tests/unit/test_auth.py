"""Tests for credential authentication: the keyed token hash (and, later, the authenticator)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from tollgate.application.auth import (
    AuthContext,
    CredentialAuthenticator,
    hash_token,
    require_scope,
)
from tollgate.domain.credentials import Credential, CredentialStatus, Principal
from tollgate.domain.errors import AuthenticationFailed, ScopeNotAuthorized
from tollgate.domain.ids import CredentialId, OrgId, PrincipalId, TeamId, UserId
from tollgate.domain.scopes import ScopeKind

_SECRET = "pepper"


def test_hash_token_is_deterministic() -> None:
    assert hash_token("tok", secret=_SECRET) == hash_token("tok", secret=_SECRET)


def test_hash_token_depends_on_the_token() -> None:
    assert hash_token("tok-a", secret=_SECRET) != hash_token("tok-b", secret=_SECRET)


def test_hash_token_depends_on_the_secret() -> None:
    assert hash_token("tok", secret="pepper-a") != hash_token("tok", secret="pepper-b")


def test_hash_token_is_a_sha256_hex_digest() -> None:
    digest = hash_token("tok", secret=_SECRET)
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


def test_hash_token_never_contains_the_raw_token() -> None:
    assert "tok" not in hash_token("tok", secret=_SECRET)


class _FakeCredentials:
    def __init__(
        self, *, credential: Credential | None = None, principal: Principal | None = None
    ) -> None:
        self._credential = credential
        self._principal = principal
        self.looked_up: list[str] = []

    async def find_by_token_hash(self, token_hash: str) -> Credential | None:
        self.looked_up.append(token_hash)
        return self._credential

    async def load_principal(self, principal_id: PrincipalId) -> Principal | None:
        return self._principal


def _active_credential() -> Credential:
    return Credential(
        credential_id=CredentialId("cred-1"),
        principal_id=PrincipalId("user-1"),
        scope_kind=ScopeKind.TEAM,
        scope_id="team-1",
        status=CredentialStatus.ACTIVE,
    )


def _principal() -> Principal:
    return Principal(user_id=UserId("user-1"), team_id=TeamId("team-1"), org_id=OrgId("org-1"))


async def test_authenticate_returns_context_for_active_credential() -> None:
    repo = _FakeCredentials(credential=_active_credential(), principal=_principal())
    auth = CredentialAuthenticator(repo, token_secret=_SECRET)
    ctx = await auth.authenticate("tok")
    assert ctx == AuthContext(credential=_active_credential(), principal=_principal())
    # the lookup used the keyed hash of the presented token, never the raw token
    assert repo.looked_up == [hash_token("tok", secret=_SECRET)]


async def test_authenticate_rejects_unknown_token() -> None:
    auth = CredentialAuthenticator(_FakeCredentials(credential=None), token_secret=_SECRET)
    with pytest.raises(AuthenticationFailed):
        await auth.authenticate("tok")


async def test_authenticate_rejects_revoked_credential() -> None:
    revoked = replace(_active_credential(), status=CredentialStatus.REVOKED)
    repo = _FakeCredentials(credential=revoked, principal=_principal())
    auth = CredentialAuthenticator(repo, token_secret=_SECRET)
    with pytest.raises(AuthenticationFailed):
        await auth.authenticate("tok")


async def test_authenticate_rejects_when_principal_missing() -> None:
    repo = _FakeCredentials(credential=_active_credential(), principal=None)
    auth = CredentialAuthenticator(repo, token_secret=_SECRET)
    with pytest.raises(AuthenticationFailed):
        await auth.authenticate("tok")


def test_require_scope_passes_when_authorized() -> None:
    # a team-1 credential covers a user under team-1
    require_scope(
        _active_credential(),
        {ScopeKind.ORG: "org-1", ScopeKind.TEAM: "team-1", ScopeKind.USER: "user-1"},
        target="user:user-1",
    )


def test_require_scope_raises_when_not_authorized() -> None:
    # a team credential cannot name a project (a project has no team ancestor)
    with pytest.raises(ScopeNotAuthorized) as excinfo:
        require_scope(
            _active_credential(),
            {ScopeKind.ORG: "org-1", ScopeKind.PROJECT: "proj-1"},
            target="project:proj-1",
        )
    assert excinfo.value.scope == "project:proj-1"
