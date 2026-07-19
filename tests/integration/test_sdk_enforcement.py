"""End-to-end: the SDK guard enforces against the real control plane (in-process ASGI).

Drives the guard over an ``httpx.ASGITransport`` wired to :func:`build_app` against the real,
migrated Postgres container (no mocks, no running server) — proving a within-budget call
reserves, dispatches, and commits (moving the ledger and the balance), while an over-budget call
is denied *before* the guarded body ever runs.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tollgate.adapters.integrations.sdk import BudgetDenied, SdkConfig, TollgateClient, guard
from tollgate.adapters.integrations.sdk.tokenizer import HeuristicTokenizer
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
_CONFIG = SdkConfig(base_url="http://tollgate", token=_TOKEN)


async def _seed(engine: AsyncEngine, *, user_limit: int = 1_000, org_limit: int = 1_000) -> None:
    """Lifted from ``test_api_http.py``: org/team/user, credential, price book, budgets."""
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


@asynccontextmanager
async def _sdk_client(postgres_url: str) -> AsyncIterator[TollgateClient]:
    """The real composition root served in-process over ASGI, wrapped by the SDK client.

    ``TollgateClient`` sets ``Authorization: Bearer`` per request from ``SdkConfig.token``, so
    the injected ``httpx.AsyncClient`` needs no auth header of its own.
    """
    app = build_app(Settings(database_url=postgres_url, token_hash_secret=_SECRET))
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://tollgate")
    client = TollgateClient(_CONFIG, http=http)
    try:
        yield client
    finally:
        await http.aclose()
        await app.state.engine.dispose()


async def test_guard_reserves_and_commits_within_budget(
    postgres_url: str, committing_engine: AsyncEngine
) -> None:
    await _seed(committing_engine)  # default limits (1_000 each) comfortably admit the estimate
    async with (
        _sdk_client(postgres_url) as client,
        guard(
            client,
            config=_CONFIG,
            tokenizer=HeuristicTokenizer(),
            provider="anthropic",
            model="claude",
            prompt="hello there",
            max_output_tokens=16,
        ) as call,
    ):
        assert call.reservation_id
        call.record_usage(input_tokens=8, output_tokens=4)

    # The call dispatched (the body ran) and committed for real: a commit ledger row exists,
    # and the user's balance moved off zero.
    assert (
        await _scalar(committing_engine, "SELECT count(*) FROM ledger WHERE kind='commit_adjust'")
        >= 1
    )
    committed_micro = await _scalar(
        committing_engine, "SELECT committed_micro FROM budget_balance WHERE budget_id = 'b-user'"
    )
    assert committed_micro > 0
    # actual = 8*1 + 4*2 = 16 micro, reconciled down from the worst-case estimate on commit
    assert committed_micro == 16
    assert (
        await _scalar(committing_engine, "SELECT count(*) FROM reservation WHERE status='held'")
        == 0
    )


async def test_guard_denies_over_budget_and_never_dispatches(
    postgres_url: str, committing_engine: AsyncEngine
) -> None:
    # input_bound = ceil(len("hello there")/3) + 16 margin = 4 + 16 = 20 tokens
    # estimate = 20*1 + 16*2 = 52 micro > user_limit=1 -> denied at the user node
    await _seed(committing_engine, user_limit=1)
    dispatched = False
    async with _sdk_client(postgres_url) as client:
        with pytest.raises(BudgetDenied):
            async with guard(
                client,
                config=_CONFIG,
                tokenizer=HeuristicTokenizer(),
                provider="anthropic",
                model="claude",
                prompt="hello there",
                max_output_tokens=16,
            ):
                dispatched = True

    # The denial happened before the guarded body ran — the model call never dispatched.
    assert dispatched is False
    # A denial never persists: no reservation was held and no idempotency key was claimed
    # (insufficient-budget denials roll back — no stale-deny caching, no balance row created).
    assert await _scalar(committing_engine, "SELECT count(*) FROM reservation") == 0
    assert await _scalar(committing_engine, "SELECT count(*) FROM idempotency_key") == 0
    assert await _scalar(committing_engine, "SELECT count(*) FROM budget_balance") == 0
