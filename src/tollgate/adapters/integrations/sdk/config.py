"""Client configuration for the Tollgate SDK guard."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SdkConfig:
    """How the guard reaches the Tollgate control plane and shapes its estimates.

    Timeouts are tight on purpose: the guard sits on the synchronous path of every model call,
    so a slow datastore must fail *fast* (fail-closed) rather than add latency to every call.
    """

    base_url: str
    token: str
    connect_timeout_seconds: float = 2.0
    read_timeout_seconds: float = 2.0
    provider_margin_tokens: int = 16
    default_max_output_tokens: int = 1024
    strict_uncapped: bool = False
    heartbeat_interval_seconds: float = 60.0
