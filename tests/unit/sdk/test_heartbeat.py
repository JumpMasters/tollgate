"""The guard heartbeats extend while the block is open (spec §5.4)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from tollgate.adapters.integrations.sdk.client import (
    CancelResult,
    CommitResult,
    ExtendResult,
    ReserveResult,
)
from tollgate.adapters.integrations.sdk.config import SdkConfig
from tollgate.adapters.integrations.sdk.guard import guard
from tollgate.adapters.integrations.sdk.tokenizer import HeuristicTokenizer

_DEADLINE = datetime(2026, 6, 23, 12, 10, tzinfo=UTC)


class _FakeClient:
    def __init__(self) -> None:
        self.extends = 0

    async def reserve(self, **kwargs: object) -> ReserveResult:
        return ReserveResult("res-1", 300, "pb-1", _DEADLINE)

    async def commit(self, **kwargs: object) -> CommitResult:
        return CommitResult("res-1", 300, 0)

    async def cancel(self, **kwargs: object) -> CancelResult:
        return CancelResult("res-1", 300)

    async def extend(self, *, reservation_id: str) -> ExtendResult:
        self.extends += 1
        return ExtendResult(reservation_id, _DEADLINE)


async def test_heartbeat_fires_while_open_and_stops_after_exit() -> None:
    client = _FakeClient()
    config = SdkConfig(base_url="http://t", token="tok", heartbeat_interval_seconds=0.01)
    async with guard(
        client,  # type: ignore[arg-type]
        config=config,
        tokenizer=HeuristicTokenizer(),
        provider="a",
        model="m",
        prompt="hello",
        max_output_tokens=10,
        new_key=lambda: "k",
    ) as call:
        await asyncio.sleep(0.05)  # ~5 intervals
        call.record_usage(input_tokens=1, output_tokens=1)
    fired_while_open = client.extends
    assert fired_while_open >= 1  # heartbeated during the call
    await asyncio.sleep(0.03)
    assert client.extends == fired_while_open  # stopped once the block exited


async def test_heartbeat_disabled_when_interval_non_positive() -> None:
    client = _FakeClient()
    config = SdkConfig(base_url="http://t", token="tok", heartbeat_interval_seconds=0.0)
    async with guard(
        client,  # type: ignore[arg-type]
        config=config,
        tokenizer=HeuristicTokenizer(),
        provider="a",
        model="m",
        prompt="hello",
        max_output_tokens=10,
        new_key=lambda: "k",
    ) as call:
        await asyncio.sleep(0.02)
        call.record_usage(input_tokens=1, output_tokens=1)
    assert client.extends == 0
