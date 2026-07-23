"""Tests for TollgateClient over an httpx.MockTransport (no network)."""

from __future__ import annotations

import json
from typing import cast

import httpx
import pytest

from tollgate.adapters.integrations.sdk.client import MeterResult, ProviderUsage, TollgateClient
from tollgate.adapters.integrations.sdk.config import SdkConfig
from tollgate.adapters.integrations.sdk.errors import BudgetDenied, EnforcementUnavailable

_CONFIG = SdkConfig(base_url="http://tollgate.test", token="tok-1")


def _client(handler: httpx.MockTransport) -> TollgateClient:
    http = httpx.AsyncClient(base_url=_CONFIG.base_url, transport=handler)
    return TollgateClient(_CONFIG, http=http)


async def test_reserve_sends_the_wire_shape_and_parses_the_result() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["idem"] = request.headers.get("Idempotency-Key")
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "reservation_id": "res-1",
                "estimated_micro": 300,
                "price_book_version": "pb-1",
                "ttl_deadline": "2026-06-23T12:10:00+00:00",
            },
        )

    client = _client(httpx.MockTransport(handler))
    result = await client.reserve(
        provider="anthropic",
        model="claude",
        input_bound_tokens=100,
        max_output_tokens=100,
        idempotency_key="idem-1",
        project="p1",
        labels={"env": "prod"},
    )
    assert result.reservation_id == "res-1"
    assert result.estimated_micro == 300
    assert str(seen["url"]).endswith("/v1/reserve")
    assert seen["idem"] == "idem-1"
    assert seen["auth"] == "Bearer tok-1"
    assert seen["body"] == {
        "provider": "anthropic",
        "model": "claude",
        "input_bound_tokens": 100,
        "max_output_tokens": 100,
        "cache_creation_bound_tokens": 0,
        "labels": {"env": "prod"},
        "project_id": "p1",
    }


async def test_reserve_sends_a_declared_cache_creation_bound() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "reservation_id": "res-1",
                "estimated_micro": 400,
                "price_book_version": "pb-1",
                "ttl_deadline": "2026-06-23T12:10:00+00:00",
            },
        )

    client = _client(httpx.MockTransport(handler))
    await client.reserve(
        provider="anthropic",
        model="claude",
        input_bound_tokens=100,
        max_output_tokens=100,
        idempotency_key="idem-1",
        cache_creation_bound_tokens=80,
    )
    assert cast(dict[str, object], seen["body"])["cache_creation_bound_tokens"] == 80


async def test_reserve_maps_402_to_budget_denied() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            402,
            json={
                "error": {
                    "code": "insufficient_budget",
                    "message": "insufficient budget at user:u1",
                }
            },
        )

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(BudgetDenied) as exc:
        await client.reserve(
            provider="a", model="m", input_bound_tokens=1, max_output_tokens=1, idempotency_key="k"
        )
    assert exc.value.status == 402
    assert "user:u1" in str(exc.value)


async def test_connectivity_failure_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = _client(httpx.MockTransport(handler))
    with pytest.raises(EnforcementUnavailable):
        await client.reserve(
            provider="a", model="m", input_bound_tokens=1, max_output_tokens=1, idempotency_key="k"
        )


async def test_commit_sends_the_wire_shape_and_parses_the_result() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["idem"] = request.headers.get("Idempotency-Key")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"reservation_id": "res-1", "committed_micro": 200, "overage_micro": 50},
        )

    client = _client(httpx.MockTransport(handler))
    result = await client.commit(
        reservation_id="res-1",
        usage=ProviderUsage(
            input_tokens=100, output_tokens=50, cached_input_tokens=10, cache_creation_tokens=5
        ),
        idempotency_key="idem-c",
    )
    assert result.committed_micro == 200
    assert result.overage_micro == 50
    assert str(seen["url"]).endswith("/v1/commit")
    assert seen["idem"] == "idem-c"
    assert seen["body"] == {
        "reservation_id": "res-1",
        "usage": {
            "input_tokens": 100,
            "output_tokens": 50,
            "cached_input_tokens": 10,
            "cache_creation_tokens": 5,
        },
    }


async def test_cancel_sends_the_wire_shape_and_parses_the_result() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["idem"] = request.headers.get("Idempotency-Key")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"reservation_id": "res-1", "released_micro": 300})

    client = _client(httpx.MockTransport(handler))
    result = await client.cancel(reservation_id="res-1", idempotency_key="idem-x")
    assert result.released_micro == 300
    assert str(seen["url"]).endswith("/v1/cancel")
    assert seen["idem"] == "idem-x"
    assert seen["body"] == {"reservation_id": "res-1"}


async def test_extend_sends_no_idempotency_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("Idempotency-Key") is None  # extend is monotonic
        return httpx.Response(
            200, json={"reservation_id": "res-1", "ttl_deadline": "2026-06-23T12:20:00+00:00"}
        )

    client = _client(httpx.MockTransport(handler))
    result = await client.extend(reservation_id="res-1")
    assert result.reservation_id == "res-1"


async def test_meter_sends_the_wire_shape_and_parses_the_result() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["idem"] = request.headers.get("Idempotency-Key")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"actual_micro": 175, "price_book_version": "pb-1"})

    client = _client(httpx.MockTransport(handler))
    result = await client.meter(
        provider="anthropic",
        model="claude",
        usage=ProviderUsage(
            input_tokens=100, output_tokens=50, cached_input_tokens=10, cache_creation_tokens=5
        ),
        idempotency_key="idem-m",
        project="p1",
        labels={"env": "prod"},
        truncated=True,
    )
    assert result == MeterResult(actual_micro=175, price_book_version="pb-1")
    assert str(seen["url"]).endswith("/v1/meter")
    assert seen["idem"] == "idem-m"
    assert seen["body"] == {
        "provider": "anthropic",
        "model": "claude",
        "usage": {
            "input_tokens": 100,
            "output_tokens": 50,
            "cached_input_tokens": 10,
            "cache_creation_tokens": 5,
        },
        "labels": {"env": "prod"},
        "truncated": True,
        "project_id": "p1",
    }


async def test_meter_omits_project_id_when_not_given() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"actual_micro": 100, "price_book_version": "pb-1"})

    client = _client(httpx.MockTransport(handler))
    await client.meter(
        provider="anthropic",
        model="claude",
        usage=ProviderUsage(input_tokens=10, output_tokens=5),
        idempotency_key="idem-m2",
    )
    assert "project_id" not in cast(dict[str, object], seen["body"])
