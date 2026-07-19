"""Unit tests for the shared worker polling loop (§5.5)."""

from __future__ import annotations

import asyncio

import pytest

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


class _AlwaysFails:
    def __init__(self) -> None:
        self.calls = 0

    async def run_once(self) -> object:
        self.calls += 1
        raise RuntimeError("persistent failure")


class _ScriptedTick:
    def __init__(self, stop: asyncio.Event, outcomes: list[str]) -> None:
        self._stop = stop
        self._outcomes = outcomes
        self.calls = 0

    async def run_once(self) -> object:
        outcome = self._outcomes[self.calls]
        self.calls += 1
        if outcome == "stop":
            self._stop.set()
            return None
        if outcome == "fail":
            raise RuntimeError("transient")
        return None


async def test_run_forever_exits_after_consecutive_failure_threshold() -> None:
    # A persistently failing worker (bad DSN, revoked grants, unapplied migrations) must not fail
    # every tick forever while looking healthy: past the threshold the loop exits so the
    # orchestrator restarts and alerts (#75).
    stop = asyncio.Event()
    tick = _AlwaysFails()
    with pytest.raises(RuntimeError, match="persistent failure"):
        await run_forever(
            tick,
            interval_seconds=0,
            stop=stop,
            name="test",
            max_consecutive_failures=3,
            backoff_base_seconds=0,
            backoff_max_seconds=0,
        )
    assert tick.calls == 3  # exits on the third consecutive failure, not before


async def test_run_forever_resets_the_failure_counter_on_success() -> None:
    # A success between failures resets the counter, so transient blips never trip the exit (#75).
    stop = asyncio.Event()
    tick = _ScriptedTick(stop, ["fail", "fail", "ok", "fail", "fail", "stop"])
    await run_forever(
        tick,
        interval_seconds=0,
        stop=stop,
        name="test",
        max_consecutive_failures=3,
        backoff_base_seconds=0,
        backoff_max_seconds=0,
    )
    assert tick.calls == 6  # never three failures in a row, so it ran to the stop
