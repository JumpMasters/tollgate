"""Integration tests for PostgresCredentialRepository (real Postgres)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncConnection

from tollgate.adapters.postgres.credential_repo import PostgresCredentialRepository
from tollgate.adapters.postgres.schema import api_credential, org, team, user_principal
from tollgate.application.auth import hash_token
from tollgate.domain.credentials import CredentialStatus
from tollgate.domain.ids import PrincipalId
from tollgate.domain.scopes import ScopeKind


async def _seed(
    conn: AsyncConnection,
    *,
    token_hash: str = "hash-1",
    status: str = "active",
) -> None:
    await conn.execute(org.insert().values(org_id="org-1", name="Acme"))
    await conn.execute(team.insert().values(team_id="team-1", org_id="org-1", name="Payments"))
    await conn.execute(
        user_principal.insert().values(user_id="user-1", team_id="team-1", external_ref=None)
    )
    await conn.execute(
        api_credential.insert().values(
            credential_id="cred-1",
            principal_id="user-1",
            scope_kind="team",
            scope_id="team-1",
            token_hash=token_hash,
            status=status,
        )
    )


async def test_find_by_token_hash_maps_an_active_credential(db_conn: AsyncConnection) -> None:
    await _seed(db_conn, token_hash="hash-1", status="active")
    repo = PostgresCredentialRepository(db_conn)
    credential = await repo.find_by_token_hash("hash-1")
    assert credential is not None
    assert credential.credential_id == "cred-1"
    assert credential.principal_id == "user-1"
    assert credential.scope_kind is ScopeKind.TEAM
    assert credential.scope_id == "team-1"
    assert credential.status is CredentialStatus.ACTIVE


async def test_find_by_token_hash_returns_revoked_faithfully(db_conn: AsyncConnection) -> None:
    # the store returns the row as-is; the authenticator (not the store) rejects non-active
    await _seed(db_conn, token_hash="hash-1", status="revoked")
    repo = PostgresCredentialRepository(db_conn)
    credential = await repo.find_by_token_hash("hash-1")
    assert credential is not None
    assert credential.status is CredentialStatus.REVOKED


async def test_find_by_token_hash_returns_none_for_unknown(db_conn: AsyncConnection) -> None:
    repo = PostgresCredentialRepository(db_conn)
    assert await repo.find_by_token_hash("nope") is None


async def test_load_principal_joins_user_team_org(db_conn: AsyncConnection) -> None:
    await _seed(db_conn)
    repo = PostgresCredentialRepository(db_conn)
    principal = await repo.load_principal(PrincipalId("user-1"))
    assert principal is not None
    assert (principal.user_id, principal.team_id, principal.org_id) == (
        "user-1",
        "team-1",
        "org-1",
    )


async def test_load_principal_returns_none_for_unknown(db_conn: AsyncConnection) -> None:
    repo = PostgresCredentialRepository(db_conn)
    assert await repo.load_principal(PrincipalId("ghost")) is None


async def test_deterministic_hash_round_trip_finds_the_credential(db_conn: AsyncConnection) -> None:
    # mint the stored hash exactly as authentication will recompute it
    stored = hash_token("secret-token", secret="pepper")
    await _seed(db_conn, token_hash=stored)
    repo = PostgresCredentialRepository(db_conn)
    found = await repo.find_by_token_hash(hash_token("secret-token", secret="pepper"))
    assert found is not None
    assert found.credential_id == "cred-1"
