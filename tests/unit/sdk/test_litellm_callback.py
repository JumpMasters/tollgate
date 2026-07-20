"""The LiteLLM metering callback, exercised against the real ``CustomLogger`` and ``Usage`` types.

These tests build realistic litellm ``kwargs``/``response_obj`` payloads (litellm 1.93 shapes)
and inject a fake :class:`TollgateClient` recording every ``meter`` call. The callback maps
litellm's usage to Tollgate's disjoint token convention (ADR 0036), forwards call metadata as
labels, derives a stable idempotency key from the response id, and never lets a metering error
escape into the host application.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import pytest
from litellm.types.utils import ModelResponse, Usage

from tollgate.adapters.integrations.litellm_callback import TollgateMeteringCallback
from tollgate.adapters.integrations.sdk import EnforcementUnavailable, MeterResult, ProviderUsage

_START = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
_END = datetime(2026, 7, 19, 12, 0, 1, tzinfo=UTC)


class _FakeClient:
    """Records every ``meter`` call; optionally raises to prove the hook swallows it."""

    def __init__(self, *, raises: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._raises = raises

    async def meter(self, **kwargs: Any) -> MeterResult:
        self.calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        return MeterResult(actual_micro=100, price_book_version="pb-1")


def _usage(*, prompt: int, completion: int, cached: int = 0, creation: int = 0) -> Usage:
    """A litellm ``Usage`` in the real Anthropic shape (``prompt_tokens`` folds in cache read
    and cache creation; the per-class breakdown lives on ``prompt_tokens_details``)."""
    return Usage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        # A plain dict here; litellm coerces it into a PromptTokensDetailsWrapper on validation.
        prompt_tokens_details={
            "cached_tokens": cached,
            "cache_creation_tokens": creation,
            "text_tokens": prompt - cached - creation,
        },
        cache_creation_input_tokens=creation,
        cache_read_input_tokens=cached,
    )


def _response(usage: Usage | None, *, id: str = "chatcmpl-abc") -> ModelResponse:
    response = ModelResponse(id=id)
    if usage is not None:
        response.usage = usage  # type: ignore[attr-defined]
    return response


def _kwargs(
    *,
    provider: str = "anthropic",
    model: str = "claude-3-5-sonnet-20241022",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "model": model,
        "custom_llm_provider": provider,
        "litellm_call_id": "call-xyz",
        "litellm_params": {"metadata": metadata if metadata is not None else {}},
    }


def _callback(client: _FakeClient) -> TollgateMeteringCallback:
    return TollgateMeteringCallback(client)  # type: ignore[arg-type]


async def test_success_meters_with_disjoint_mapping_and_labels() -> None:
    client = _FakeClient()
    callback = _callback(client)
    usage = _usage(prompt=1100, completion=50, cached=300, creation=100)
    await callback.async_log_success_event(
        kwargs=_kwargs(metadata={"team": "research", "env": "prod"}),
        response_obj=_response(usage),
        start_time=_START,
        end_time=_END,
    )

    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["provider"] == "anthropic"
    assert call["model"] == "claude-3-5-sonnet-20241022"
    assert call["truncated"] is False
    # Disjoint convention (ADR 0036): input excludes cache creation but includes the cache-read
    # subset; creation is separate and additive.
    assert call["usage"] == ProviderUsage(
        input_tokens=1000,  # prompt_tokens (1100) - cache creation (100)
        output_tokens=50,
        cached_input_tokens=300,
        cache_creation_tokens=100,
    )
    assert call["labels"] == {"team": "research", "env": "prod"}


async def test_success_maps_uncached_openai_style_usage() -> None:
    client = _FakeClient()
    callback = _callback(client)
    # No cache activity: input_tokens == prompt_tokens, creation == 0.
    usage = _usage(prompt=200, completion=40)
    await callback.async_log_success_event(
        kwargs=_kwargs(provider="openai", model="gpt-4o"),
        response_obj=_response(usage),
        start_time=_START,
        end_time=_END,
    )
    assert client.calls[0]["usage"] == ProviderUsage(input_tokens=200, output_tokens=40)
    assert client.calls[0]["provider"] == "openai"


async def test_failure_with_partial_usage_meters_truncated() -> None:
    client = _FakeClient()
    callback = _callback(client)
    usage = _usage(prompt=200, completion=5)
    await callback.async_log_failure_event(
        kwargs=_kwargs(),
        response_obj=_response(usage),
        start_time=_START,
        end_time=_END,
    )
    assert len(client.calls) == 1
    assert client.calls[0]["truncated"] is True
    assert client.calls[0]["usage"] == ProviderUsage(input_tokens=200, output_tokens=5)


async def test_failure_with_no_usage_does_not_meter() -> None:
    client = _FakeClient()
    callback = _callback(client)
    await callback.async_log_failure_event(
        kwargs=_kwargs(),
        response_obj=_response(None),
        start_time=_START,
        end_time=_END,
    )
    assert client.calls == []


async def test_failure_with_all_zero_usage_does_not_meter() -> None:
    client = _FakeClient()
    callback = _callback(client)
    await callback.async_log_failure_event(
        kwargs=_kwargs(),
        response_obj=_response(_usage(prompt=0, completion=0)),
        start_time=_START,
        end_time=_END,
    )
    assert client.calls == []


async def test_meter_error_is_swallowed_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _FakeClient(raises=EnforcementUnavailable("down", status=503, code=None))
    callback = _callback(client)
    with caplog.at_level(logging.WARNING):
        # Must not propagate: metering can never break the host application.
        await callback.async_log_success_event(
            kwargs=_kwargs(),
            response_obj=_response(_usage(prompt=10, completion=5)),
            start_time=_START,
            end_time=_END,
        )
    assert len(client.calls) == 1  # it tried
    assert any(r.levelno == logging.WARNING for r in caplog.records)


async def test_idempotency_key_is_stable_for_the_same_response_id() -> None:
    client = _FakeClient()
    callback = _callback(client)
    usage = _usage(prompt=10, completion=5)
    for _ in range(2):
        await callback.async_log_success_event(
            kwargs=_kwargs(),
            response_obj=_response(usage, id="chatcmpl-stable"),
            start_time=_START,
            end_time=_END,
        )
    keys = {call["idempotency_key"] for call in client.calls}
    assert len(keys) == 1  # a retry meters under the same key


async def test_idempotency_key_differs_across_response_ids() -> None:
    client = _FakeClient()
    callback = _callback(client)
    usage = _usage(prompt=10, completion=5)
    for rid in ("chatcmpl-a", "chatcmpl-b"):
        await callback.async_log_success_event(
            kwargs=_kwargs(),
            response_obj=_response(usage, id=rid),
            start_time=_START,
            end_time=_END,
        )
    keys = [call["idempotency_key"] for call in client.calls]
    assert keys[0] != keys[1]  # derived from the response id


async def test_failure_hook_swallows_meter_error() -> None:
    client = _FakeClient(raises=EnforcementUnavailable("down", status=503, code=None))
    callback = _callback(client)
    # Must not propagate from the failure hook either.
    await callback.async_log_failure_event(
        kwargs=_kwargs(),
        response_obj=_response(_usage(prompt=10, completion=5)),
        start_time=_START,
        end_time=_END,
    )
    assert len(client.calls) == 1  # it tried, then swallowed


async def test_labels_coerce_scalar_metadata_and_drop_nested() -> None:
    client = _FakeClient()
    callback = _callback(client)
    metadata = {"team": "research", "attempt": 3, "sampled": True, "trace": {"nested": "dropped"}}
    await callback.async_log_success_event(
        kwargs=_kwargs(metadata=metadata),
        response_obj=_response(_usage(prompt=10, completion=5)),
        start_time=_START,
        end_time=_END,
    )
    # Scalars are stringified; nested/complex values are dropped.
    assert client.calls[0]["labels"] == {"team": "research", "attempt": "3", "sampled": "True"}


async def test_reads_usage_and_id_from_dict_response_obj() -> None:
    client = _FakeClient()
    callback = _callback(client)
    response_obj = {
        "id": "resp-dict",
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "prompt_tokens_details": {"cached_tokens": 0, "cache_creation_tokens": 0},
        },
    }
    await callback.async_log_success_event(
        kwargs=_kwargs(),
        response_obj=response_obj,
        start_time=_START,
        end_time=_END,
    )
    assert client.calls[0]["usage"] == ProviderUsage(input_tokens=100, output_tokens=20)
    assert client.calls[0]["idempotency_key"] == "litellm-meter-resp-dict"


async def test_idempotency_key_falls_back_when_no_id_available() -> None:
    client = _FakeClient()
    callback = _callback(client)
    # No response id and no litellm_call_id: usage lives in kwargs, key falls back to a random one.
    kwargs = {
        "model": "m",
        "custom_llm_provider": "anthropic",
        "litellm_params": {"metadata": {}},
        "usage": {"prompt_tokens": 50, "completion_tokens": 5},
    }
    await callback.async_log_failure_event(
        kwargs=kwargs,
        response_obj=None,
        start_time=_START,
        end_time=_END,
    )
    assert len(client.calls) == 1
    assert client.calls[0]["idempotency_key"].startswith("litellm-meter-")
    assert client.calls[0]["truncated"] is True
