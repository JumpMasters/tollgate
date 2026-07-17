"""End-to-end chargeback reads against real Postgres: the read wire contract (ADR 0032)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from tollgate.adapters.postgres.schema import (
    api_credential,
    budget,
    budget_alert,
    budget_balance,
    org,
    project,
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
    period_start = calendar_month_start(datetime.now(UTC))
    async with engine.begin() as conn:
        await conn.execute(org.insert().values(org_id="o1", name="Acme"))
        await conn.execute(team.insert().values(team_id="t1", org_id="o1", name="Payments"))
        await conn.execute(
            user_principal.insert().values(user_id="u1", team_id="t1", external_ref=None)
        )
        await conn.execute(project.insert().values(project_id="p1", org_id="o1", key="checkout"))
        for credential_id, principal_id, scope_kind, scope_id, token in (
            ("cred-org", "u1", "org", "o1", _ORG_TOKEN),
            ("cred-user", "u1", "user", "u1", _USER_TOKEN),
        ):
            await conn.execute(
                api_credential.insert().values(
                    credential_id=credential_id,
                    principal_id=principal_id,
                    scope_kind=scope_kind,
                    scope_id=scope_id,
                    token_hash=hash_token(token, secret=_SECRET),
                    status="active",
                )
            )
        for budget_id, scope_kind, scope_id, limit in (
            ("b-org", "org", "o1", 10_000),
            ("b-t1", "team", "t1", 5_000),
            ("b-u1", "user", "u1", 1_000),
            ("b-p1", "project", "p1", 2_000),
        ):
            await conn.execute(
                budget.insert().values(
                    budget_id=budget_id,
                    scope_kind=scope_kind,
                    scope_id=scope_id,
                    period_kind="calendar_month",
                    hard_limit_micro=limit,
                )
            )
        # org budget has activity + an alert this period; user budget has none (zero-state path).
        await conn.execute(
            budget_balance.insert().values(
                budget_id="b-org",
                period_start=period_start,
                limit_micro=10_000,
                reserved_micro=3_000,
                committed_micro=6_000,
                overage_micro=0,
            )
        )
        await conn.execute(budget_alert.insert().values(budget_id="b-org", threshold_pct=90))
        await conn.execute(budget_alert.insert().values(budget_id="b-org", threshold_pct=50))


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


async def test_missing_bearer_token_is_401(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/budgets")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.json()["error"]["code"] == "authentication_failed"


async def test_org_credential_sees_the_whole_subtree(
    client: httpx.AsyncClient, committing_engine: AsyncEngine
) -> None:
    await _seed(committing_engine)
    response = await client.get("/v1/budgets", headers=_auth(_ORG_TOKEN))
    assert response.status_code == 200
    body = response.json()
    seen = {(b["scope_kind"], b["scope_id"]) for b in body["budgets"]}
    assert seen == {("org", "o1"), ("team", "t1"), ("user", "u1"), ("project", "p1")}


async def test_org_node_reports_utilization_and_crossed_alerts(
    client: httpx.AsyncClient, committing_engine: AsyncEngine
) -> None:
    await _seed(committing_engine)
    body = (await client.get("/v1/budgets", headers=_auth(_ORG_TOKEN))).json()
    org_node = next(b for b in body["budgets"] if b["scope_id"] == "o1")
    # spent = 3000 + 6000 = 9000 of 10000 -> 90%
    assert org_node["remaining_micro"] == 1_000
    assert org_node["utilization_pct"] == 90
    assert org_node["alerts"] == [
        {"threshold_pct": 50, "crossed": True},
        {"threshold_pct": 90, "crossed": True},
    ]


async def test_user_credential_sees_only_its_own_node_as_zero_state(
    client: httpx.AsyncClient, committing_engine: AsyncEngine
) -> None:
    await _seed(committing_engine)
    body = (await client.get("/v1/budgets", headers=_auth(_USER_TOKEN))).json()
    assert len(body["budgets"]) == 1
    node = body["budgets"][0]
    # no balance row for b-user this period -> synthesized zero state against hard_limit_micro
    assert node == {
        "scope_kind": "user",
        "scope_id": "u1",
        "limit_micro": 1_000,
        "reserved_micro": 0,
        "committed_micro": 0,
        "overage_micro": 0,
        "remaining_micro": 1_000,
        "utilization_pct": 0,
        "alerts": [],
    }


async def test_scope_filter_reroots_the_subtree(
    client: httpx.AsyncClient, committing_engine: AsyncEngine
) -> None:
    await _seed(committing_engine)
    body = (
        await client.get("/v1/budgets", params={"scope": "team:t1"}, headers=_auth(_ORG_TOKEN))
    ).json()
    seen = {(b["scope_kind"], b["scope_id"]) for b in body["budgets"]}
    assert seen == {("team", "t1"), ("user", "u1")}  # team subtree; no org, no project


async def test_filter_outside_scope_is_403_without_an_existence_leak(
    client: httpx.AsyncClient, committing_engine: AsyncEngine
) -> None:
    await _seed(committing_engine)
    response = await client.get(
        "/v1/budgets", params={"scope": "org:o1"}, headers=_auth(_USER_TOKEN)
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "scope_not_authorized"


async def test_unknown_filter_node_is_403_identically(
    client: httpx.AsyncClient, committing_engine: AsyncEngine
) -> None:
    await _seed(committing_engine)
    response = await client.get(
        "/v1/budgets", params={"scope": "team:ghost"}, headers=_auth(_ORG_TOKEN)
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "scope_not_authorized"


async def test_malformed_scope_is_422(
    client: httpx.AsyncClient, committing_engine: AsyncEngine
) -> None:
    await _seed(committing_engine)
    response = await client.get(
        "/v1/budgets", params={"scope": "not-a-scope"}, headers=_auth(_ORG_TOKEN)
    )
    assert response.status_code == 422
