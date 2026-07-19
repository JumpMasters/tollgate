"""The shared background-worker polling loop (§5.5).

A worker is a pure ``run_once`` tick (a reaper) wrapped by ``run_forever``, which polls it on a
fixed interval until a stop event is set. Each tick is bounded and idempotent, so a *transient*
failure is logged and the loop continues — a reaper is a backstop and must not die on one bad
poll. But a *persistent* failure (a wrong ``TOLLGATE_DATABASE_URL``, revoked grants, unapplied
migrations) would otherwise fail every tick forever while looking healthy to the orchestrator, so
consecutive failures back off exponentially and, past a threshold, the loop exits non-zero for the
orchestrator to restart and alert (#75). This module imports only the standard library: the tick
logic lives in the application layer and the concrete wiring in ``app.py`` (``app → workers``), so
``workers`` never imports ``adapters``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Protocol

logger = logging.getLogger(__name__)


class SupportsRunOnce(Protocol):
    """One bounded, idempotent unit of polled work (a reaper's ``run_once``)."""

    async def run_once(self) -> object: ...


async def run_forever(
    tick: SupportsRunOnce,
    *,
    interval_seconds: float,
    stop: asyncio.Event,
    name: str,
    max_consecutive_failures: int = 10,
    backoff_base_seconds: float = 1.0,
    backoff_max_seconds: float = 60.0,
) -> None:
    """Poll ``tick.run_once`` every ``interval_seconds`` until ``stop`` is set (§5.5).

    Between ticks the loop waits the interval but wakes immediately when ``stop`` is set (graceful
    shutdown). A tick that raises is logged and the loop continues after an exponential backoff
    (``backoff_base_seconds`` doubling up to ``backoff_max_seconds``); a success resets the backoff.
    After ``max_consecutive_failures`` in a row the loop re-raises, so the process exits non-zero
    and the orchestrator restarts and alerts rather than a wedged worker looking healthy (#75).
    """
    consecutive_failures = 0
    while not stop.is_set():
        try:
            result = await tick.run_once()
            logger.info("%s tick complete: %r", name, result)
            consecutive_failures = 0
            wait = interval_seconds
        except Exception:
            consecutive_failures += 1
            logger.exception(
                "%s tick failed (%d in a row); backing off", name, consecutive_failures
            )
            if consecutive_failures >= max_consecutive_failures:
                logger.error(
                    "%s failed %d ticks in a row; exiting for the orchestrator to restart",
                    name,
                    consecutive_failures,
                )
                raise
            wait = min(backoff_base_seconds * 2 ** (consecutive_failures - 1), backoff_max_seconds)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=wait)
