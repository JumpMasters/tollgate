"""Constraint tests for the tenancy & identity tables (org/team/user/project/credential)."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection


async def _seed_principal(conn: AsyncConnection) -> None:
    await conn.execute(text("INSERT INTO org (org_id, name) VALUES ('o1', 'Org')"))
    await conn.execute(text("INSERT INTO team (team_id, org_id, name) VALUES ('t1', 'o1', 'Team')"))
    await conn.execute(text("INSERT INTO user_principal (user_id, team_id) VALUES ('u1', 't1')"))


async def test_identity_tables_exist(db_conn: AsyncConnection) -> None:
    for table in ("org", "team", "user_principal", "project", "api_credential"):
        await db_conn.execute(text(f"SELECT * FROM {table} LIMIT 0"))


async def test_team_requires_an_existing_org(db_conn: AsyncConnection) -> None:
    with pytest.raises(IntegrityError):
        await db_conn.execute(
            text("INSERT INTO team (team_id, org_id, name) VALUES ('t1', 'missing', 'Team')")
        )


async def test_credential_scope_kind_check_rejects_unknown(db_conn: AsyncConnection) -> None:
    await _seed_principal(db_conn)
    with pytest.raises(IntegrityError):
        await db_conn.execute(
            text(
                "INSERT INTO api_credential "
                "(credential_id, principal_id, scope_kind, scope_id, token_hash) "
                "VALUES ('c1', 'u1', 'galaxy', 'g1', 'hash1')"
            )
        )


async def test_credential_status_check_rejects_unknown(db_conn: AsyncConnection) -> None:
    await _seed_principal(db_conn)
    with pytest.raises(IntegrityError):
        await db_conn.execute(
            text(
                "INSERT INTO api_credential "
                "(credential_id, principal_id, scope_kind, scope_id, token_hash, status) "
                "VALUES ('c1', 'u1', 'team', 't1', 'hash1', 'bogus')"
            )
        )


async def test_credential_token_hash_is_unique(db_conn: AsyncConnection) -> None:
    await _seed_principal(db_conn)
    await db_conn.execute(
        text(
            "INSERT INTO api_credential "
            "(credential_id, principal_id, scope_kind, scope_id, token_hash) "
            "VALUES ('c1', 'u1', 'team', 't1', 'dup')"
        )
    )
    with pytest.raises(IntegrityError):
        await db_conn.execute(
            text(
                "INSERT INTO api_credential "
                "(credential_id, principal_id, scope_kind, scope_id, token_hash) "
                "VALUES ('c2', 'u1', 'team', 't1', 'dup')"
            )
        )
