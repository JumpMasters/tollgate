"""A LiteLLM ``CustomLogger`` that meters completed (and truncated) model calls into Tollgate.

LiteLLM invokes ``async_log_success_event`` once a call finishes and ``async_log_failure_event``
when it errors. This callback reads the provider-reported usage off those events, maps it to
Tollgate's disjoint token convention (ADR 0036), and records the spend via the SDK's ``meter``
command. It is metering only: enforcement is the guard's job (ADR 0022), so a metering failure is
logged and swallowed — it must never break the host application (mirrors the guard heartbeat).

The usage mapping targets litellm's real ``Usage`` shape (verified against litellm 1.93):
``prompt_tokens`` already folds in cache read and cache creation, while the per-class breakdown
lives on ``prompt_tokens_details`` (``cached_tokens`` = cache read, ``cache_creation_tokens`` =
cache creation). Tollgate wants ``input_tokens`` to include the cache-read subset but *exclude*
cache creation (creation is disjoint and additive), so ``input_tokens = prompt_tokens -
cache_creation``. Everything is read defensively with ``.get(...)`` because the shape is
version-sensitive; unknown or absent counts default to zero.

This module stays SDK-local: it imports only the SDK client types and litellm, never
``tollgate.domain``/``api``/``workers`` (enforced by import-linter).
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from litellm.integrations.custom_logger import CustomLogger

from tollgate.adapters.integrations.sdk import ProviderUsage, TollgateClient

logger = logging.getLogger(__name__)


class TollgateMeteringCallback(CustomLogger):
    """Meters litellm calls into Tollgate; errors are logged, never raised."""

    def __init__(self, client: TollgateClient) -> None:
        super().__init__()
        self._client = client

    async def async_log_success_event(
        self, kwargs: dict[str, Any], response_obj: Any, start_time: Any, end_time: Any
    ) -> None:
        """Meter a completed call with its provider-reported usage (``truncated=False``)."""
        try:
            usage = _map_usage(_find_usage(kwargs, response_obj))
            provider, model = _extract_provider_model(kwargs)
            await self._client.meter(
                provider=provider,
                model=model,
                usage=usage,
                idempotency_key=_idempotency_key(response_obj, kwargs),
                labels=_extract_labels(kwargs),
                truncated=False,
            )
        except Exception:  # metering must never break the host application (cf. guard heartbeat)
            logger.warning("tollgate metering (success) failed; spend not recorded", exc_info=True)

    async def async_log_failure_event(
        self, kwargs: dict[str, Any], response_obj: Any, start_time: Any, end_time: Any
    ) -> None:
        """Meter the last-seen partial usage of a failed call as ``truncated=True``.

        A failure often carries no usage (the call never produced tokens); only meter when a
        positive token count is present, so an empty failure records nothing.
        """
        try:
            usage = _map_usage(_find_usage(kwargs, response_obj))
            if not _has_positive_usage(usage):
                return
            provider, model = _extract_provider_model(kwargs)
            await self._client.meter(
                provider=provider,
                model=model,
                usage=usage,
                idempotency_key=_idempotency_key(response_obj, kwargs),
                labels=_extract_labels(kwargs),
                truncated=True,
            )
        except Exception:  # metering must never break the host application (cf. guard heartbeat)
            logger.warning("tollgate metering (failure) failed; spend not recorded", exc_info=True)


def _as_mapping(obj: object) -> dict[str, Any]:
    """Normalise a litellm object (pydantic model or dict) to a plain dict for ``.get`` access."""
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        dumped = dump()
        if isinstance(dumped, dict):
            return dumped
    if isinstance(obj, dict):
        return dict(obj)
    return {}


def _int(value: object) -> int:
    """Coerce a token count to a non-negative int; anything unusable becomes 0."""
    if isinstance(value, bool | int | float):
        return max(int(value), 0)
    return 0


def _find_usage(kwargs: dict[str, Any], response_obj: Any) -> dict[str, Any]:
    """Locate the usage block on the response (or, defensively, in kwargs)."""
    usage_obj = getattr(response_obj, "usage", None)
    if usage_obj is None and isinstance(response_obj, dict):
        usage_obj = response_obj.get("usage")
    if usage_obj is None:
        usage_obj = kwargs.get("usage")
    return _as_mapping(usage_obj)


def _map_usage(usage: dict[str, Any]) -> ProviderUsage:
    """Map litellm's usage to Tollgate's disjoint convention (ADR 0036).

    ``input_tokens`` includes the cache-read subset but excludes cache creation; the cache-read
    and cache-creation counts are read from ``prompt_tokens_details`` first, falling back to the
    top-level Anthropic fields when present.
    """
    prompt_tokens = _int(usage.get("prompt_tokens"))
    completion_tokens = _int(usage.get("completion_tokens"))
    details = _as_mapping(usage.get("prompt_tokens_details"))
    cache_read = _int(details.get("cached_tokens")) or _int(usage.get("cache_read_input_tokens"))
    cache_creation = _int(details.get("cache_creation_tokens")) or _int(
        usage.get("cache_creation_input_tokens")
    )
    # Relies on litellm's prompt_tokens being the SUM of raw + cache-read + cache-creation, so
    # subtracting creation still leaves the cache-read subset in input_tokens and Tollgate's
    # cached_input_tokens <= input_tokens invariant holds. If a future litellm/provider stops
    # summing this way, the server rejects the meter and the failure is logged-and-swallowed
    # above (no host-app break).
    return ProviderUsage(
        input_tokens=max(prompt_tokens - cache_creation, 0),
        output_tokens=completion_tokens,
        cached_input_tokens=cache_read,
        cache_creation_tokens=cache_creation,
    )


def _has_positive_usage(usage: ProviderUsage) -> bool:
    # cached_input is a subset of input_tokens, so those three fields cover every token class.
    return usage.input_tokens > 0 or usage.output_tokens > 0 or usage.cache_creation_tokens > 0


def _extract_provider_model(kwargs: dict[str, Any]) -> tuple[str, str]:
    litellm_params = _as_mapping(kwargs.get("litellm_params"))
    provider = kwargs.get("custom_llm_provider") or litellm_params.get("custom_llm_provider") or ""
    model = kwargs.get("model") or ""
    return str(provider), str(model)


def _extract_labels(kwargs: dict[str, Any]) -> dict[str, str]:
    """Read chargeback labels ONLY from the dedicated ``metadata["tollgate_labels"]`` namespace.

    litellm's call metadata (``kwargs["metadata"]`` / ``kwargs["litellm_params"]["metadata"]``) is
    shared with litellm-internal bookkeeping (e.g. ``user_api_key_alias``, ``model_group`` on the
    proxy/router path), so forwarding it wholesale would leak those keys into chargeback labels.
    Callers opt a value into labels explicitly by nesting it under ``tollgate_labels``, e.g.
    ``litellm.acompletion(..., metadata={"tollgate_labels": {"env": "prod", "team": "x"}})``. Every
    other metadata key is ignored. Keys and values are coerced to ``str``; if ``tollgate_labels`` is
    absent or not a mapping, no labels are recorded.
    """
    litellm_params = _as_mapping(kwargs.get("litellm_params"))
    merged = {**_as_mapping(kwargs.get("metadata")), **_as_mapping(litellm_params.get("metadata"))}
    tollgate_labels = merged.get("tollgate_labels")
    if not isinstance(tollgate_labels, dict):
        return {}
    return {str(key): str(value) for key, value in tollgate_labels.items()}


def _idempotency_key(response_obj: Any, kwargs: dict[str, Any]) -> str:
    """A stable metering key derived from the response id (retries meter under the same key).

    Falls back to litellm's per-call id, then a random key only when no stable id exists.
    """
    response_id = getattr(response_obj, "id", None)
    if response_id is None and isinstance(response_obj, dict):
        response_id = response_obj.get("id")
    if response_id is None:
        response_id = kwargs.get("litellm_call_id") or kwargs.get("id")
    if response_id:
        return f"litellm-meter-{response_id}"
    return f"litellm-meter-{uuid4().hex}"
