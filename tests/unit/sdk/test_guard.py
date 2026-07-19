"""Tests for the guard context manager against a fake TollgateClient."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime

import pytest

from tollgate.adapters.integrations.sdk.client import (
    CancelResult,
    CommitResult,
    ExtendResult,
    ProviderUsage,
    ReserveResult,
)
from tollgate.adapters.integrations.sdk.config import SdkConfig
from tollgate.adapters.integrations.sdk.errors import BudgetDenied, InvalidRequest
from tollgate.adapters.integrations.sdk.guard import GuardedCall, guard
from tollgate.adapters.integrations.sdk.tokenizer import HeuristicTokenizer

_DEADLINE = datetime(2026, 6, 23, 12, 10, tzinfo=UTC)
_CONFIG = SdkConfig(base_url="http://t", token="tok", heartbeat_interval_seconds=3600.0)


class _FakeClient:
    def __init__(self, *, reserve_error: Exception | None = None) -> None:
        self._reserve_error = reserve_error
        self.reserved: dict[str, object] | None = None
        self.committed: ProviderUsage | None = None
        self.cancelled = False
        self.extends = 0

    async def reserve(self, **kwargs: object) -> ReserveResult:
        if self._reserve_error is not None:
            raise self._reserve_error
        self.reserved = kwargs
        return ReserveResult("res-1", 300, "pb-1", _DEADLINE)

    async def commit(
        self, *, reservation_id: str, usage: ProviderUsage, idempotency_key: str
    ) -> CommitResult:
        self.committed = usage
        return CommitResult(reservation_id, 300, 0)

    async def cancel(self, *, reservation_id: str, idempotency_key: str) -> CancelResult:
        self.cancelled = True
        return CancelResult(reservation_id, 300)

    async def extend(self, *, reservation_id: str) -> ExtendResult:
        self.extends += 1
        return ExtendResult(reservation_id, _DEADLINE)


def _guard(client: _FakeClient, **overrides: object) -> AbstractAsyncContextManager[GuardedCall]:
    kwargs: dict[str, object] = {
        "config": _CONFIG,
        "tokenizer": HeuristicTokenizer(),
        "provider": "anthropic",
        "model": "claude",
        "prompt": "abcdef",  # ceil(6/3)=2 tokens
        "max_output_tokens": 100,
        "new_key": lambda: "fixed-key",
    }
    kwargs.update(overrides)
    return guard(client, **kwargs)  # type: ignore[arg-type]


async def test_reserve_then_commit_on_clean_exit_with_usage() -> None:
    client = _FakeClient()
    async with _guard(client) as call:
        assert call.reservation_id == "res-1"
        assert call.estimated_micro == 300
        call.record_usage(input_tokens=90, output_tokens=40, cache_creation_tokens=3)
    assert client.reserved is not None
    # input bound = 2 tokens + default margin 16 = 18
    assert client.reserved["input_bound_tokens"] == 18
    assert client.reserved["max_output_tokens"] == 100
    assert client.committed == ProviderUsage(
        input_tokens=90, output_tokens=40, cached_input_tokens=0, cache_creation_tokens=3
    )
    assert client.cancelled is False


async def test_clean_exit_without_usage_cancels() -> None:
    client = _FakeClient()
    async with _guard(client):
        pass  # never recorded usage (e.g. the caller decided not to dispatch)
    assert client.committed is None
    assert client.cancelled is True


async def test_exception_in_body_cancels_and_propagates() -> None:
    client = _FakeClient()
    with pytest.raises(RuntimeError, match="provider 500"):
        async with _guard(client) as call:
            call.record_usage(input_tokens=1, output_tokens=1)
            raise RuntimeError("provider 500")
    assert client.committed is None  # a failed call is not committed
    assert client.cancelled is True


async def test_denied_reserve_never_enters_the_body() -> None:
    client = _FakeClient(
        reserve_error=BudgetDenied("no headroom", status=402, code="insufficient_budget")
    )
    entered = False
    with pytest.raises(BudgetDenied):
        async with _guard(client):
            entered = True
    assert entered is False  # reserve denied -> the call never dispatches
    assert client.cancelled is False  # nothing was reserved, so nothing to cancel


async def test_cleanup_cancel_failure_does_not_mask_the_body_exception() -> None:
    client = _FakeClient()

    async def _boom(*, reservation_id: str, idempotency_key: str) -> object:
        raise RuntimeError("control plane down during cleanup")

    client.cancel = _boom  # type: ignore[assignment]
    with pytest.raises(ValueError, match="provider failed"):
        async with _guard(client) as call:
            call.record_usage(input_tokens=1, output_tokens=1)
            raise ValueError("provider failed")


async def test_strict_mode_rejects_an_uncapped_call() -> None:
    client = _FakeClient()
    strict = SdkConfig(base_url="http://t", token="tok", strict_uncapped=True)
    with pytest.raises(InvalidRequest, match="max_output_tokens"):
        async with _guard(client, config=strict, max_output_tokens=None):
            pass
    assert client.reserved is None  # never reserved
