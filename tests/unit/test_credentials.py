"""Tests for the credential authN/authz value types and the authorization predicate."""

from __future__ import annotations

import pytest

from tollgate.domain.credentials import (
    Credential,
    CredentialStatus,
    Principal,
    authorizes,
)
from tollgate.domain.ids import CredentialId, OrgId, PrincipalId, TeamId, UserId
from tollgate.domain.scopes import ScopeKind

_USER_NODE = {ScopeKind.ORG: "org-1", ScopeKind.TEAM: "team-1", ScopeKind.USER: "user-1"}
_TEAM_NODE = {ScopeKind.ORG: "org-1", ScopeKind.TEAM: "team-1"}
_PROJECT_NODE = {ScopeKind.ORG: "org-1", ScopeKind.PROJECT: "proj-1"}


def _cred(scope_kind: ScopeKind, scope_id: str) -> Credential:
    return Credential(
        credential_id=CredentialId("cred-1"),
        principal_id=PrincipalId("user-1"),
        scope_kind=scope_kind,
        scope_id=scope_id,
        status=CredentialStatus.ACTIVE,
    )


def test_credential_and_principal_carry_their_fields() -> None:
    cred = _cred(ScopeKind.TEAM, "team-1")
    assert cred.principal_id == "user-1"
    assert cred.scope_kind is ScopeKind.TEAM
    assert cred.status is CredentialStatus.ACTIVE
    principal = Principal(user_id=UserId("user-1"), team_id=TeamId("team-1"), org_id=OrgId("org-1"))
    assert (principal.user_id, principal.team_id, principal.org_id) == ("user-1", "team-1", "org-1")


@pytest.mark.parametrize(
    ("scope_kind", "scope_id", "target", "expected"),
    [
        (ScopeKind.ORG, "org-1", _USER_NODE, True),  # org covers a user under it
        (ScopeKind.ORG, "org-1", _TEAM_NODE, True),  # ... and a team under it
        (ScopeKind.ORG, "org-1", _PROJECT_NODE, True),  # ... and a project under it
        (ScopeKind.ORG, "org-2", _USER_NODE, False),  # a different org does not
        (ScopeKind.TEAM, "team-1", _USER_NODE, True),  # team covers its user
        (ScopeKind.TEAM, "team-1", _TEAM_NODE, True),  # ... and itself
        (ScopeKind.TEAM, "team-1", _PROJECT_NODE, False),  # a project has no team ancestor
        (ScopeKind.TEAM, "team-2", _USER_NODE, False),  # a sibling team does not
        (ScopeKind.USER, "user-1", _USER_NODE, True),  # user covers only itself
        (ScopeKind.USER, "user-2", _USER_NODE, False),
        (ScopeKind.PROJECT, "proj-1", _PROJECT_NODE, True),  # project covers only its project
        (ScopeKind.PROJECT, "proj-2", _PROJECT_NODE, False),
    ],
)
def test_authorizes_covers_at_or_below_scope(
    scope_kind: ScopeKind, scope_id: str, target: dict[ScopeKind, str], expected: bool
) -> None:
    assert authorizes(_cred(scope_kind, scope_id), target) is expected
