"""End-to-end HTTP spend rollups against real Postgres: the wire contract (ADR 0033)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from tollgate.adapters.postgres.schema import (
    api_credential,
    budget,
    ledger,
    org,
    price_book,
    reservation,
    team,
    user_principal,
)
from tollgate.app import build_app
from tollgate.application.auth import hash_token
from tollgate.config.settings import Settings
from tollgate.domain.periods import calendar_month_start

_SECRET = "e2e-pepper"
_ORG_TOKEN = "org-admin-token"
_USER_TOKEN = "user-token"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _seed(engine: AsyncEngine) -> None:
    period = calendar_month_start(datetime.now(UTC))
    async with engine.begin() as conn:
        await conn.execute(org.insert().values(org_id="o1", name="Acme"))
        await conn.execute(team.insert().values(team_id="t1", org_id="o1", name="Payments"))
        await conn.execute(
            user_principal.insert().values(user_id="u1", team_id="t1", external_ref=None)
        )
        for cid, kind, sid, token in (
            ("cred-org", "org", "o1", _ORG_TOKEN),
            ("cred-user", "user", "u1", _USER_TOKEN),
        ):
            await conn.execute(
                api_credential.insert().values(
                    credential_id=cid,
                    principal_id="u1",
                    scope_kind=kind,
                    scope_id=sid,
                    token_hash=hash_token(token, secret=_SECRET),
                    status="active",
                )
            )
        for bid, kind, sid in (("b-org", "org", "o1"), ("b-user", "user", "u1")):
            await conn.execute(
                budget.insert().values(
                    budget_id=bid,
                    scope_kind=kind,
                    scope_id=sid,
                    period_kind="calendar_month",
                    hard_limit_micro=1_000_000,
                )
            )
        # reservation.price_book_version is a NOT NULL FK to price_book.version.
        await conn.execute(price_book.insert().values(version="pb-1", published_at=period))
        for rid, provider, model, env in (
            ("r1", "anthropic", "claude", "prod"),
            ("r2", "openai", "gpt", "dev"),
        ):
            await conn.execute(
                reservation.insert().values(
                    reservation_id=rid,
                    idempotency_key=f"idem-{rid}",
                    status="committed",
                    principal_id="u1",
                    provider=provider,
                    model=model,
                    price_book_version="pb-1",
                    estimated_micro=0,
                    input_bound_tokens=0,
                    max_output_tokens=0,
                    labels={"env": env},
                    ttl_deadline=period,
                )
            )
        # commit rows on BOTH org and user budgets (reservation drew on both), + a grace row on user
        rows: list[tuple[str, str, str | None, str, int, int, str]] = [
            ("e1o", "b-org", "r1", "anthropic", 200, 0, "commit_adjust"),
            ("e1u", "b-user", "r1", "anthropic", 200, 0, "commit_adjust"),
            ("e2o", "b-org", "r2", "openai", 100, 0, "commit_adjust"),
            ("e2u", "b-user", "r2", "openai", 100, 0, "commit_adjust"),
            ("e3u", "b-user", None, "anthropic", 40, 10, "grace_backfill"),
            ("e3o", "b-org", None, "anthropic", 40, 10, "grace_backfill"),
        ]
        for eid, bid, res_id, prov, committed, overage, kind in rows:
            await conn.execute(
                ledger.insert().values(
                    entry_id=eid,
                    kind=kind,
                    budget_id=bid,
                    period_start=period,
                    reservation_id=res_id,
                    delta_committed_micro=committed,
                    delta_overage_micro=overage,
                    provider=prov,
                )
            )


@pytest_asyncio.fixture
async def client(
    committing_engine: AsyncEngine, postgres_url: str
) -> AsyncIterator[httpx.AsyncClient]:
    app = build_app(Settings(database_url=postgres_url, token_hash_secret=_SECRET))
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://tollgate") as http:
            yield http
    finally:
        await app.state.engine.dispose()


async def test_missing_bearer_is_401(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/spend", params={"group_by": "provider"})
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.json()["error"]["code"] == "authentication_failed"


async def test_org_provider_rollup(
    client: httpx.AsyncClient, committing_engine: AsyncEngine
) -> None:
    await _seed(committing_engine)
    body = (
        await client.get("/v1/spend", params={"group_by": "provider"}, headers=_auth(_ORG_TOKEN))
    ).json()
    assert body["group_by"] == "provider"
    groups = {g["group"]: g["spend_micro"] for g in body["groups"]}
    assert groups == {"anthropic": 250, "openai": 100}  # 200 + grace(40+10); no double-count


async def test_org_model_rollup_buckets_grace_as_null(
    client: httpx.AsyncClient, committing_engine: AsyncEngine
) -> None:
    await _seed(committing_engine)
    body = (
        await client.get("/v1/spend", params={"group_by": "model"}, headers=_auth(_ORG_TOKEN))
    ).json()
    groups = {
        (g["group"] if g["group"] is not None else "_null_"): g["spend_micro"]
        for g in body["groups"]
    }
    assert groups == {"claude": 200, "gpt": 100, "_null_": 50}  # grace -> null bucket


async def test_label_env_rollup(client: httpx.AsyncClient, committing_engine: AsyncEngine) -> None:
    await _seed(committing_engine)
    body = (
        await client.get("/v1/spend", params={"group_by": "label:env"}, headers=_auth(_ORG_TOKEN))
    ).json()
    groups = {
        (g["group"] if g["group"] is not None else "_null_"): g["spend_micro"]
        for g in body["groups"]
    }
    assert groups == {"prod": 200, "dev": 100, "_null_": 50}  # grace has no reservation -> null


async def test_user_scope_sees_only_its_own_node(
    client: httpx.AsyncClient, committing_engine: AsyncEngine
) -> None:
    await _seed(committing_engine)
    body = (
        await client.get("/v1/spend", params={"group_by": "provider"}, headers=_auth(_USER_TOKEN))
    ).json()
    groups = {g["group"]: g["spend_micro"] for g in body["groups"]}
    assert groups == {"anthropic": 250, "openai": 100}  # user budget rows only, still not doubled


async def test_org_filter_to_user_reroots(
    client: httpx.AsyncClient, committing_engine: AsyncEngine
) -> None:
    await _seed(committing_engine)
    # Make the org node's own rollup diverge from the user node's, so a re-root that
    # actually targets user:u1 is distinguishable from one that ignores the filter and
    # falls back to the org credential's own scope.
    period = calendar_month_start(datetime.now(UTC))
    async with committing_engine.begin() as conn:
        await conn.execute(
            ledger.insert().values(
                entry_id="e4o",
                kind="commit_adjust",
                budget_id="b-org",
                period_start=period,
                reservation_id="r1",
                delta_committed_micro=500,
                delta_overage_micro=0,
                provider="anthropic",
            )
        )

    reroot = await client.get(
        "/v1/spend", params={"group_by": "provider", "scope": "user:u1"}, headers=_auth(_ORG_TOKEN)
    )
    assert reroot.status_code == 200
    reroot_groups = {g["group"]: g["spend_micro"] for g in reroot.json()["groups"]}
    # Proves the filter re-rooted to user:u1's own numbers, not the org's inflated 750.
    assert reroot_groups == {"anthropic": 250, "openai": 100}

    own_scope = await client.get(
        "/v1/spend", params={"group_by": "provider"}, headers=_auth(_ORG_TOKEN)
    )
    assert own_scope.status_code == 200
    own_scope_groups = {g["group"]: g["spend_micro"] for g in own_scope.json()["groups"]}
    # Demonstrates the org node's own total genuinely differs from the re-rooted result.
    assert own_scope_groups == {"anthropic": 750, "openai": 100}


async def test_user_filter_to_org_is_403(
    client: httpx.AsyncClient, committing_engine: AsyncEngine
) -> None:
    await _seed(committing_engine)
    response = await client.get(
        "/v1/spend", params={"group_by": "provider", "scope": "org:o1"}, headers=_auth(_USER_TOKEN)
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "scope_not_authorized"


async def test_malformed_group_by_is_422(
    client: httpx.AsyncClient, committing_engine: AsyncEngine
) -> None:
    await _seed(committing_engine)
    response = await client.get(
        "/v1/spend", params={"group_by": "nonsense"}, headers=_auth(_ORG_TOKEN)
    )
    assert response.status_code == 422
    assert "detail" in response.json()


async def test_missing_group_by_is_422(
    client: httpx.AsyncClient, committing_engine: AsyncEngine
) -> None:
    await _seed(committing_engine)
    response = await client.get("/v1/spend", headers=_auth(_ORG_TOKEN))
    assert response.status_code == 422
    assert "detail" in response.json()


async def test_period_start_filters_to_an_empty_period(
    client: httpx.AsyncClient, committing_engine: AsyncEngine
) -> None:
    await _seed(committing_engine)
    response = await client.get(
        "/v1/spend",
        params={"group_by": "provider", "period_start": "2020-01-01T00:00:00Z"},
        headers=_auth(_ORG_TOKEN),
    )
    assert response.status_code == 200
    # _seed only writes ledger rows for the current calendar month; a route that ignored
    # period_start would fall back to that month and return the non-empty rollup the other
    # tests assert, so an empty groups list proves the query param actually flows through.
    assert response.json()["groups"] == []
