"""Unit tests for the shared worker polling loop."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from tollgate.workers.runner import WorkerStalled, _backoff_seconds, run_forever


@dataclass(frozen=True)
class _Report:
    """Stand-in for a ReapReport-shaped tick result the runner inspects by duck type."""

    reaped: int
    failed: int = 0


class _ReportingTick:
    def __init__(self, stop: asyncio.Event, results: list[_Report]) -> None:
        self._stop = stop
        self._results = results
        self.calls = 0

    async def run_once(self) -> object:
        result = self._results[self.calls]
        self.calls += 1
        if self.calls >= len(self._results):
            self._stop.set()
        return result


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


async def test_run_forever_escalates_when_a_tick_reports_failures_without_progress() -> None:
    # A per-item reap failure returns a normal report (it does not raise), so the #75 escalation
    # would never fire and a poison row could recirculate forever while the worker looks healthy
    # (#91). A tick that reaped nothing but hit failures made no forward progress: treat it like a
    # raised tick so backoff/escalation surface the stall.
    stop = asyncio.Event()
    tick = _ReportingTick(stop, [_Report(reaped=0, failed=5)] * 10)
    with pytest.raises(WorkerStalled):
        await run_forever(
            tick,
            interval_seconds=0,
            stop=stop,
            name="test",
            max_consecutive_failures=3,
            backoff_base_seconds=0,
            backoff_max_seconds=0,
        )
    assert tick.calls == 3  # exits on the third stalled tick


async def test_run_forever_tolerates_failures_while_making_progress() -> None:
    # As long as the reaper still clears healthy reservations (reaped > 0), per-item failures must
    # not take the whole reaper down — progress resets the counter (#91).
    stop = asyncio.Event()
    tick = _ReportingTick(stop, [_Report(reaped=2, failed=1)] * 5)
    await run_forever(
        tick,
        interval_seconds=0,
        stop=stop,
        name="test",
        max_consecutive_failures=3,
        backoff_base_seconds=0,
        backoff_max_seconds=0,
    )
    assert tick.calls == 5  # never escalates; runs to the stop


async def test_run_forever_ignores_failed_field_on_scalar_results() -> None:
    # The idempotency reaper returns a plain int; it has no reaped/failed fields, so it is never
    # judged stalled.
    stop = asyncio.Event()
    tick = _CountingTick(stop, stop_after=3)
    await run_forever(tick, interval_seconds=0, stop=stop, name="test")
    assert tick.calls == 3


def test_backoff_seconds_does_not_overflow_for_a_large_failure_count() -> None:
    # worker_max_consecutive_failures only enforces ge=1, so a large value must not make
    # base * 2 ** (n - 1) raise OverflowError before it is clamped to the ceiling (#107).
    assert _backoff_seconds(5000, base=1.0, cap=60.0) == 60.0


def test_backoff_seconds_grows_then_clamps() -> None:
    assert _backoff_seconds(1, base=1.0, cap=60.0) == 1.0
    assert _backoff_seconds(3, base=1.0, cap=60.0) == 4.0
    assert _backoff_seconds(10, base=1.0, cap=60.0) == 60.0  # clamped
