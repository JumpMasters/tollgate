"""Unit tests for the shared worker polling loop (§5.5)."""

from __future__ import annotations

import asyncio

from tollgate.workers.runner import run_forever


class _CountingTick:
    def __init__(self, stop: asyncio.Event, *, stop_after: int, fail_on: int | None = None) -> None:
        self._stop = stop
        self._stop_after = stop_after
        self._fail_on = fail_on
        self.calls = 0

    async def run_once(self) -> object:
        self.calls += 1
        if self._fail_on == self.calls:
            raise RuntimeError("transient failure")
        if self.calls >= self._stop_after:
            self._stop.set()
        return self.calls


async def test_run_forever_polls_until_stop_is_set() -> None:
    stop = asyncio.Event()
    tick = _CountingTick(stop, stop_after=3)
    await run_forever(tick, interval_seconds=0, stop=stop, name="test")
    assert tick.calls == 3


async def test_run_forever_does_not_tick_when_already_stopped() -> None:
    stop = asyncio.Event()
    stop.set()
    tick = _CountingTick(stop, stop_after=1)
    await run_forever(tick, interval_seconds=0, stop=stop, name="test")
    assert tick.calls == 0  # graceful shutdown before the first poll


async def test_run_forever_survives_a_failing_tick() -> None:
    stop = asyncio.Event()
    tick = _CountingTick(stop, stop_after=3, fail_on=1)  # first tick raises
    await run_forever(tick, interval_seconds=0, stop=stop, name="test")
    assert tick.calls == 3  # the loop kept polling after the failure
