"""End-to-end HTTP lifecycle against real Postgres: the wire contract (ADR 0031) on the real app."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tollgate.adapters.postgres.schema import (
    api_credential,
    budget,
    org,
    price,
    price_book,
    team,
    user_principal,
)
from tollgate.app import build_app
from tollgate.application.auth import hash_token
from tollgate.config.settings import Settings

_TOKEN = "e2e-bearer-token"
_SECRET = "e2e-pepper"
_PUBLISHED_AT = datetime(2026, 6, 1, tzinfo=UTC)
_RESERVE_BODY = {
    "provider": "anthropic",
    "model": "claude",
    "input_bound_tokens": 100,
    "max_output_tokens": 100,
    "labels": {"env": "prod"},
}  # estimate = 100*1 + 100*2 = 300


def _headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_TOKEN}", "Idempotency-Key": key}


async def _seed(engine: AsyncEngine, *, user_limit: int = 1_000, org_limit: int = 1_000) -> None:
    async with engine.begin() as conn:
        await conn.execute(price_book.insert().values(version="pb-1", published_at=_PUBLISHED_AT))
        await conn.execute(
            price.insert().values(
                price_book_version="pb-1",
                provider="anthropic",
                model="claude",
                input_micro_per_token=Decimal("1"),
                output_micro_per_token=Decimal("2"),
                cached_input_micro_per_token=Decimal("0.5"),
            )
        )
        await conn.execute(org.insert().values(org_id="o1", name="Acme"))
        await conn.execute(team.insert().values(team_id="t1", org_id="o1", name="Payments"))
        await conn.execute(
            user_principal.insert().values(user_id="u1", team_id="t1", external_ref=None)
        )
        await conn.execute(
            api_credential.insert().values(
                credential_id="cred-1",
                principal_id="u1",
                scope_kind="user",
                scope_id="u1",
                token_hash=hash_token(_TOKEN, secret=_SECRET),
                status="active",
            )
        )
        for budget_id, scope_kind, scope_id, limit in (
            ("b-org", "org", "o1", org_limit),
            ("b-user", "user", "u1", user_limit),
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


async def _scalar(engine: AsyncEngine, sql: str, params: dict[str, Any] | None = None) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(text(sql), params or {})).scalar_one()


@pytest_asyncio.fixture
async def client(
    committing_engine: AsyncEngine, postgres_url: str
) -> AsyncIterator[httpx.AsyncClient]:
    """The real composition root served in-process over ASGI against the container."""
    app = build_app(Settings(database_url=postgres_url, token_hash_secret=_SECRET))
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://tollgate") as http:
            yield http
    finally:
        await app.state.engine.dispose()


async def test_a_missing_bearer_token_is_401(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/reserve", json=_RESERVE_BODY, headers={"Idempotency-Key": "k"}
    )
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.json()["error"]["code"] == "authentication_failed"


async def test_an_unknown_token_is_rejected_identically(
    client: httpx.AsyncClient, committing_engine: AsyncEngine
) -> None:
    await _seed(committing_engine)
    response = await client.post(
        "/v1/reserve",
        json=_RESERVE_BODY,
        headers={"Authorization": "Bearer wrong-token", "Idempotency-Key": "k"},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_failed"


async def test_reserve_round_trips_the_result(
    client: httpx.AsyncClient, committing_engine: AsyncEngine
) -> None:
    await _seed(committing_engine)
    response = await client.post("/v1/reserve", json=_RESERVE_BODY, headers=_headers("idem-res"))
    assert response.status_code == 200
    body = response.json()
    assert body["estimated_micro"] == 300
    assert body["price_book_version"] == "pb-1"
    assert body["reservation_id"]
    datetime.fromisoformat(body["ttl_deadline"])  # a valid ISO 8601 instant
    assert (
        await _scalar(committing_engine, "SELECT count(*) FROM reservation WHERE status='held'")
        == 1
    )


async def test_reserve_replays_the_stored_response(
    client: httpx.AsyncClient, committing_engine: AsyncEngine
) -> None:
    await _seed(committing_engine)
    first = await client.post("/v1/reserve", json=_RESERVE_BODY, headers=_headers("idem-res"))
    second = await client.post("/v1/reserve", json=_RESERVE_BODY, headers=_headers("idem-res"))
    assert second.status_code == 200
    assert second.json() == first.json()
    assert await _scalar(committing_engine, "SELECT count(*) FROM reservation") == 1


async def test_key_reuse_with_a_different_command_is_409(
    client: httpx.AsyncClient, committing_engine: AsyncEngine
) -> None:
    await _seed(committing_engine)
    await client.post("/v1/reserve", json=_RESERVE_BODY, headers=_headers("idem-res"))
    changed = {**_RESERVE_BODY, "max_output_tokens": 200}
    response = await client.post("/v1/reserve", json=changed, headers=_headers("idem-res"))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "idempotency_key_reuse"


async def test_a_denied_reserve_is_402_and_names_the_binding_node(
    client: httpx.AsyncClient, committing_engine: AsyncEngine
) -> None:
    await _seed(committing_engine, user_limit=100)  # estimate 300 > 100
    response = await client.post("/v1/reserve", json=_RESERVE_BODY, headers=_headers("idem-res"))
    assert response.status_code == 402
    body = response.json()
    assert body["error"]["code"] == "insufficient_budget"
    assert "u1" in body["error"]["message"]  # names the binding node (section 4)
    # a denial never persists: no reservation, and the key rolled back (section 5.1)
    assert await _scalar(committing_engine, "SELECT count(*) FROM reservation") == 0
    assert await _scalar(committing_engine, "SELECT count(*) FROM idempotency_key") == 0


async def test_an_unpriced_model_is_422(
    client: httpx.AsyncClient, committing_engine: AsyncEngine
) -> None:
    await _seed(committing_engine)
    body = {**_RESERVE_BODY, "model": "unpriced"}
    response = await client.post("/v1/reserve", json=body, headers=_headers("idem-res"))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unknown_model"


async def test_an_unknown_project_is_403_without_an_existence_leak(
    client: httpx.AsyncClient, committing_engine: AsyncEngine
) -> None:
    await _seed(committing_engine)
    body = {**_RESERVE_BODY, "project_id": "p-nope"}
    response = await client.post("/v1/reserve", json=body, headers=_headers("idem-res"))
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "scope_not_authorized"


async def test_the_full_lifecycle_over_http(
    client: httpx.AsyncClient, committing_engine: AsyncEngine
) -> None:
    await _seed(committing_engine)
    reserved = (
        await client.post("/v1/reserve", json=_RESERVE_BODY, headers=_headers("idem-res"))
    ).json()
    reservation_id = reserved["reservation_id"]

    extended = await client.post(
        "/v1/extend",
        json={"reservation_id": reservation_id},
        headers={"Authorization": f"Bearer {_TOKEN}"},  # no Idempotency-Key (section 4)
    )
    assert extended.status_code == 200
    assert extended.json()["reservation_id"] == reservation_id

    committed = await client.post(
        "/v1/commit",
        json={
            "reservation_id": reservation_id,
            "usage": {"input_tokens": 100, "output_tokens": 50},
        },
        headers=_headers("idem-commit"),
    )  # actual = 100*1 + 50*2 = 200
    assert committed.status_code == 200
    assert committed.json() == {
        "reservation_id": reservation_id,
        "committed_micro": 200,
        "overage_micro": 0,
    }

    cancel_after_commit = await client.post(
        "/v1/cancel",
        json={"reservation_id": reservation_id},
        headers=_headers("idem-cancel"),
    )
    assert cancel_after_commit.status_code == 409
    assert cancel_after_commit.json()["error"]["code"] == "reservation_not_held"

    committed_micro = await _scalar(
        committing_engine,
        "SELECT committed_micro FROM budget_balance WHERE budget_id = 'b-user'",
    )
    assert committed_micro == 200


async def test_grace_backfill_over_http(
    client: httpx.AsyncClient, committing_engine: AsyncEngine
) -> None:
    await _seed(committing_engine)
    response = await client.post(
        "/v1/grace-backfill",
        json={
            "provider": "anthropic",
            "model": "claude",
            "usage": {"input_tokens": 100, "output_tokens": 50},
        },
        headers=_headers("idem-grace"),
    )
    assert response.status_code == 200
    assert response.json() == {"actual_micro": 200, "price_book_version": "pb-1"}
    assert (
        await _scalar(committing_engine, "SELECT count(*) FROM ledger WHERE kind='grace_backfill'")
        == 2
    )
