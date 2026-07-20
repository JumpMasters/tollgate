"""Client configuration for the Tollgate SDK guard."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class SdkConfig:
    """How the guard reaches the Tollgate control plane and shapes its estimates.

    Timeouts are tight on purpose: the guard sits on the synchronous path of every model call,
    so a slow datastore must fail *fast* (fail-closed) rather than add latency to every call.

    The bearer token is kept out of the auto-generated ``repr`` (``field(repr=False)``) so it does
    not leak through f-strings, structured logs, or tracebacks that capture the config (#95); read
    it only at the HTTP boundary. ``__post_init__`` rejects values that would silently degrade
    enforcement — a negative margin under-reserves, a non-positive timeout disables the fast-fail,
    and a non-``http(s)`` base URL never reaches the control plane (#107).
    """

    base_url: str
    token: str = field(repr=False)
    connect_timeout_seconds: float = 2.0
    read_timeout_seconds: float = 2.0
    provider_margin_tokens: int = 16
    default_max_output_tokens: int = 1024
    strict_uncapped: bool = False
    heartbeat_interval_seconds: float = 60.0

    def __post_init__(self) -> None:
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError("base_url must be an http(s) URL")
        if self.connect_timeout_seconds <= 0:
            raise ValueError("connect_timeout_seconds must be positive")
        if self.read_timeout_seconds <= 0:
            raise ValueError("read_timeout_seconds must be positive")
        if self.provider_margin_tokens < 0:
            raise ValueError("provider_margin_tokens must not be negative")
        if self.default_max_output_tokens <= 0:
            raise ValueError("default_max_output_tokens must be positive")
        if self.heartbeat_interval_seconds < 0:
            raise ValueError("heartbeat_interval_seconds must not be negative")
