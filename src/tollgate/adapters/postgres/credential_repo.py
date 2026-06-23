"""PostgresCredentialRepository: the read-only lookups behind authentication (§5.0).

Two equality lookups on one bound connection: a credential by its deterministic ``token_hash``
(ADR 0026), and a principal's ``user -> team -> org`` identity by joining ``user_principal`` to
``team``. Rows are returned faithfully — a revoked credential included — so the authenticator,
not the store, enforces the active-only rule. Explicit async SQLAlchemy Core, no ORM; like the
other repositories it never imports ``application`` and satisfies the port structurally.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from tollgate.adapters.postgres.schema import api_credential, team, user_principal
from tollgate.domain.credentials import Credential, CredentialStatus, Principal
from tollgate.domain.ids import CredentialId, OrgId, PrincipalId, TeamId, UserId
from tollgate.domain.scopes import ScopeKind


class PostgresCredentialRepository:
    """Credential and principal lookups on one bound connection."""

    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def find_by_token_hash(self, token_hash: str) -> Credential | None:
        """Return the credential whose ``token_hash`` matches, or ``None`` (§5.0)."""
        row = (
            await self._conn.execute(
                select(
                    api_credential.c.credential_id,
                    api_credential.c.principal_id,
                    api_credential.c.scope_kind,
                    api_credential.c.scope_id,
                    api_credential.c.status,
                ).where(api_credential.c.token_hash == token_hash)
            )
        ).first()
        if row is None:
            return None
        return Credential(
            credential_id=CredentialId(row.credential_id),
            principal_id=PrincipalId(row.principal_id),
            scope_kind=ScopeKind(row.scope_kind),
            scope_id=row.scope_id,
            status=CredentialStatus(row.status),
        )

    async def load_principal(self, principal_id: PrincipalId) -> Principal | None:
        """Resolve a principal to its ``user -> team -> org`` identity, or ``None`` (§5.0)."""
        row = (
            await self._conn.execute(
                select(
                    user_principal.c.user_id,
                    user_principal.c.team_id,
                    team.c.org_id,
                )
                .select_from(user_principal.join(team, user_principal.c.team_id == team.c.team_id))
                .where(user_principal.c.user_id == principal_id)
            )
        ).first()
        if row is None:
            return None
        return Principal(
            user_id=UserId(row.user_id),
            team_id=TeamId(row.team_id),
            org_id=OrgId(row.org_id),
        )
