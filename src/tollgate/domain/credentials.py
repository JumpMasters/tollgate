"""Credential authority and the derived principal — the authN/authz value types.

Authentication resolves a presented bearer token to a :class:`Credential` (its authority: the
principal it speaks for and the scope node that bounds it) and a :class:`Principal` (the acting
``user -> team -> org`` identity the credential *derives* — a caller can never assert it).
Authorization is the pure predicate :func:`authorizes`: a credential may act on or reveal a
budget node iff that node is at or below the credential's own scope. This module is pure
policy over already-loaded rows — the lookups are I/O the application performs through a port —
and imports only sibling ``domain`` modules.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from tollgate.domain.ids import CredentialId, OrgId, PrincipalId, TeamId, UserId
from tollgate.domain.scopes import ScopeKind


class CredentialStatus(StrEnum):
    """Lifecycle of an API credential (mirrors the schema's CHECK)."""

    ACTIVE = "active"
    REVOKED = "revoked"


@dataclass(frozen=True, slots=True)
class Credential:
    """An authenticated credential's authority.

    ``principal_id`` is the ``user_principal`` the credential speaks for; the reserve derives the
    acting identity from it. ``scope_kind``/``scope_id`` bound the credential's authority — the
    node at or below which it may name a ``project`` on reserve and read budgets. ``status`` is
    carried so the *authenticator*, not the store, decides that a non-active credential fails
    authentication (a revoked token and an unknown token are rejected identically).
    """

    credential_id: CredentialId
    principal_id: PrincipalId
    scope_kind: ScopeKind
    scope_id: str
    status: CredentialStatus


@dataclass(frozen=True, slots=True)
class Principal:
    """The acting identity derived from a credential: a user with its team and org.

    A principal *is* a ``user_principal`` in V1 — the only principal kind — so ``user_id`` names
    the same node as the credential's ``principal_id`` (the ``api_credential.principal_id`` FK
    targets ``user_principal.user_id``). The distinct ``PrincipalId``/``UserId`` aliases record
    intent at the credential and tree layers respectively; this type is where they are bridged.
    """

    user_id: UserId
    team_id: TeamId
    org_id: OrgId


def authorizes(credential: Credential, target_ancestry: Mapping[ScopeKind, str]) -> bool:
    """Return whether ``credential`` may act on / reveal the node ``target_ancestry`` describes.

    ``target_ancestry`` maps each scope level at or above the target node to the id of the
    target's ancestor at that level — a user node is ``{ORG: o, TEAM: t, USER: u}``; a project
    node is ``{ORG: o, PROJECT: p}`` (the project axis is orthogonal, so it carries no team or
    user). The credential authorizes the target iff, **at the credential's own scope level**,
    that ancestor is exactly the credential's node::

        target_ancestry.get(credential.scope_kind) == credential.scope_id

    So an org-scoped credential covers every team, user, and project under its org; a team-scoped
    one covers its team and that team's users but no project (a project has no team ancestor); a
    user- or project-scoped credential covers only its own node. This is the "at or below the
    credential's scope" rule, expressed without walking the tree.

    The caller must build ``target_ancestry`` from trusted, server-derived ancestry
    (the principal's resolved tree and the looked-up project's org), never from
    request-asserted scope ids — this predicate cannot defend against a forged map.
    """
    return target_ancestry.get(credential.scope_kind) == credential.scope_id
