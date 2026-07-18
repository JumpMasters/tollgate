"""The shared background-worker polling loop (§5.5).

A worker is a pure ``run_once`` tick (a reaper, in plan 13) wrapped by ``run_forever``, which
polls it on a fixed interval until a stop event is set. Each tick is bounded and idempotent, so a
transient failure is logged and the loop continues — a reaper is a backstop and must not die on
one bad poll. This module imports only the standard library: the tick logic lives in the
application layer and the concrete wiring in ``app.py`` (``app → workers``), so ``workers`` never
imports ``adapters``.
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


class SupportsDispose(Protocol):
    """A resource the worker loop releases on shutdown (an engine's ``dispose``)."""

    async def dispose(self) -> None: ...


async def run_forever(
    tick: SupportsRunOnce, *, interval_seconds: float, stop: asyncio.Event, name: str
) -> None:
    """Poll ``tick.run_once`` every ``interval_seconds`` until ``stop`` is set (§5.5).

    Between ticks the loop waits the interval but wakes immediately when ``stop`` is set (graceful
    shutdown). A tick that raises is logged and the loop continues.
    """
    while not stop.is_set():
        try:
            result = await tick.run_once()
            logger.info("%s tick complete: %r", name, result)
        except Exception:
            logger.exception("%s tick failed; continuing", name)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
