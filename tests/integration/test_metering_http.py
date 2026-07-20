"""End-to-end HTTP proof of ``POST /v1/meter`` against real Postgres (section 6, ADR 0037).

Mirrors ``test_api_http.py``: drives ``build_app`` over ``httpx.ASGITransport`` against the real
PG17 container, seeding a credential, budgets, and a price row by hand. Proves the three
properties that make metering safe to ship: an over-budget meter books audited overage and never
denies, the metered spend rolls up on the self-describing ledger (model + label), and a replayed
``Idempotency-Key`` does not double-count.
"""

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

_TOKEN = "e2e-meter-token"
_SECRET = "e2e-pepper"
_PUBLISHED_AT = datetime(2026, 6, 1, tzinfo=UTC)
_METER_BODY = {
    "provider": "anthropic",
    "model": "claude",
    "usage": {"input_tokens": 100, "output_tokens": 50},
    "labels": {"env": "prod"},
}  # actual = 100*1 + 50*2 = 200


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {_TOKEN}"}


def _headers(key: str) -> dict[str, str]:
    return {**_auth(), "Idempotency-Key": key}


async def _seed(engine: AsyncEngine, *, user_limit: int, org_limit: int = 1_000_000) -> None:
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
                cache_creation_micro_per_token=Decimal("1.25"),
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


async def test_over_budget_meter_books_overage_and_never_denies(
    client: httpx.AsyncClient, committing_engine: AsyncEngine
) -> None:
    await _seed(committing_engine, user_limit=100)  # actual (200) exceeds the user's headroom
    response = await client.post("/v1/meter", json=_METER_BODY, headers=_headers("idem-meter"))
    assert response.status_code == 200
    assert response.json() == {"actual_micro": 200, "price_book_version": "pb-1"}

    budgets = (await client.get("/v1/budgets", headers=_auth())).json()
    node = next(b for b in budgets["budgets"] if b["scope_id"] == "u1")
    # remaining before the meter was 100; committed is capped there, the rest is audited overage.
    assert node["committed_micro"] == 100
    assert node["overage_micro"] == 100


async def test_rollup_by_model_and_label_attributes_to_the_metered_values(
    client: httpx.AsyncClient, committing_engine: AsyncEngine
) -> None:
    await _seed(committing_engine, user_limit=10_000)
    response = await client.post("/v1/meter", json=_METER_BODY, headers=_headers("idem-meter"))
    assert response.status_code == 200

    by_model = (await client.get("/v1/spend", params={"group_by": "model"}, headers=_auth())).json()
    assert {g["group"]: g["spend_micro"] for g in by_model["groups"]} == {"claude": 200}

    by_label = (
        await client.get("/v1/spend", params={"group_by": "label:env"}, headers=_auth())
    ).json()
    assert {g["group"]: g["spend_micro"] for g in by_label["groups"]} == {"prod": 200}


async def test_idempotent_replay_returns_the_same_result_without_double_counting(
    client: httpx.AsyncClient, committing_engine: AsyncEngine
) -> None:
    await _seed(committing_engine, user_limit=10_000)
    first = await client.post("/v1/meter", json=_METER_BODY, headers=_headers("idem-meter"))
    assert first.status_code == 200

    committed_after_first = await _scalar(
        committing_engine, "SELECT committed_micro FROM budget_balance WHERE budget_id = 'b-user'"
    )

    second = await client.post("/v1/meter", json=_METER_BODY, headers=_headers("idem-meter"))
    assert second.status_code == 200
    assert second.json() == first.json()

    committed_after_second = await _scalar(
        committing_engine, "SELECT committed_micro FROM budget_balance WHERE budget_id = 'b-user'"
    )
    assert committed_after_second == committed_after_first
    assert await _scalar(committing_engine, "SELECT count(*) FROM ledger WHERE kind='meter'") == 2


async def test_meter_dedup_survives_the_idempotency_key_reaper(
    client: httpx.AsyncClient, committing_engine: AsyncEngine
) -> None:
    # A meter applies spend with no reservation, so its dedup once lived only in the idempotency_key
    # row the reaper deletes at its TTL — a retry after that re-ran apply_spend and double-applied
    # the spend (#92). The durable metered_receipt persists past the reaper, so the retry replays.
    await _seed(committing_engine, user_limit=10_000)
    first = await client.post("/v1/meter", json=_METER_BODY, headers=_headers("idem-meter"))
    assert first.status_code == 200
    committed_after_first = await _scalar(
        committing_engine, "SELECT committed_micro FROM budget_balance WHERE budget_id = 'b-user'"
    )
    # The dedup record lives in metered_receipt, not idempotency_key.
    assert await _scalar(committing_engine, "SELECT count(*) FROM metered_receipt") == 1
    assert await _scalar(committing_engine, "SELECT count(*) FROM idempotency_key") == 0

    # Simulate the idempotency-key reaper deleting every key past its TTL.
    async with committing_engine.begin() as conn:
        await conn.execute(text("DELETE FROM idempotency_key"))

    second = await client.post("/v1/meter", json=_METER_BODY, headers=_headers("idem-meter"))
    assert second.status_code == 200
    assert second.json() == first.json()  # exact replay from the durable receipt
    committed_after_second = await _scalar(
        committing_engine, "SELECT committed_micro FROM budget_balance WHERE budget_id = 'b-user'"
    )
    assert committed_after_second == committed_after_first  # not double-applied past the TTL
    assert await _scalar(committing_engine, "SELECT count(*) FROM ledger WHERE kind='meter'") == 2
