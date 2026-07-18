"""Tests for the wire schemas (ADR 0031)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from tollgate.api.schemas import (
    MAX_LABEL_VALUE_LEN,
    MAX_LABELS,
    MAX_STR_LEN,
    MAX_TOKENS,
    ErrorEnvelope,
    GraceBackfillRequest,
    ReserveRequest,
    ReserveResponse,
    UsageBody,
)


def test_reserve_request_defaults() -> None:
    request = ReserveRequest(
        provider="anthropic", model="claude", input_bound_tokens=100, max_output_tokens=100
    )
    assert request.labels == {}
    assert request.project_id is None


def test_reserve_request_rejects_negative_tokens() -> None:
    with pytest.raises(ValidationError):
        ReserveRequest(
            provider="anthropic", model="claude", input_bound_tokens=-1, max_output_tokens=100
        )


def test_request_models_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ReserveRequest.model_validate(
            {
                "provider": "anthropic",
                "model": "claude",
                "input_bound_tokens": 1,
                "max_output_tokens": 1,
                "max_output_token": 5,
            }
        )


def test_usage_body_defaults_cached_tokens_to_zero() -> None:
    usage = UsageBody(input_tokens=1, output_tokens=2)
    assert usage.cached_input_tokens == 0


def test_usage_body_rejects_negative_counts() -> None:
    with pytest.raises(ValidationError):
        UsageBody(input_tokens=-1, output_tokens=0)


def test_reserve_request_admits_tokens_at_the_ceiling() -> None:
    request = ReserveRequest(
        provider="anthropic",
        model="claude",
        input_bound_tokens=MAX_TOKENS,
        max_output_tokens=MAX_TOKENS,
    )
    assert request.input_bound_tokens == MAX_TOKENS


def test_reserve_request_rejects_tokens_above_the_ceiling() -> None:
    with pytest.raises(ValidationError):
        ReserveRequest(
            provider="anthropic",
            model="claude",
            input_bound_tokens=MAX_TOKENS + 1,
            max_output_tokens=1,
        )
    with pytest.raises(ValidationError):
        ReserveRequest(
            provider="anthropic",
            model="claude",
            input_bound_tokens=1,
            max_output_tokens=MAX_TOKENS + 1,
        )


def test_usage_body_rejects_tokens_above_the_ceiling() -> None:
    with pytest.raises(ValidationError):
        UsageBody(input_tokens=MAX_TOKENS + 1, output_tokens=0)
    with pytest.raises(ValidationError):
        UsageBody(input_tokens=0, output_tokens=MAX_TOKENS + 1)
    with pytest.raises(ValidationError):
        UsageBody(input_tokens=1, output_tokens=0, cached_input_tokens=MAX_TOKENS + 1)


def test_usage_body_rejects_cached_exceeding_input() -> None:
    with pytest.raises(ValidationError):
        UsageBody(input_tokens=10, output_tokens=0, cached_input_tokens=11)


def test_usage_body_admits_cached_equal_to_input() -> None:
    usage = UsageBody(input_tokens=10, output_tokens=0, cached_input_tokens=10)
    assert usage.cached_input_tokens == 10


def test_reserve_request_rejects_oversized_provider_or_model() -> None:
    with pytest.raises(ValidationError):
        ReserveRequest(
            provider="p" * (MAX_STR_LEN + 1),
            model="claude",
            input_bound_tokens=1,
            max_output_tokens=1,
        )
    with pytest.raises(ValidationError):
        ReserveRequest(
            provider="anthropic",
            model="m" * (MAX_STR_LEN + 1),
            input_bound_tokens=1,
            max_output_tokens=1,
        )


def test_reserve_request_rejects_too_many_labels() -> None:
    labels = {f"k{i}": "v" for i in range(MAX_LABELS + 1)}
    with pytest.raises(ValidationError):
        ReserveRequest(
            provider="anthropic",
            model="claude",
            input_bound_tokens=1,
            max_output_tokens=1,
            labels=labels,
        )


def test_reserve_request_rejects_oversized_label_value() -> None:
    with pytest.raises(ValidationError):
        ReserveRequest(
            provider="anthropic",
            model="claude",
            input_bound_tokens=1,
            max_output_tokens=1,
            labels={"env": "v" * (MAX_LABEL_VALUE_LEN + 1)},
        )


def test_reserve_request_rejects_empty_project_id() -> None:
    with pytest.raises(ValidationError):
        ReserveRequest(
            provider="anthropic",
            model="claude",
            input_bound_tokens=1,
            max_output_tokens=1,
            project_id="",
        )


def test_grace_backfill_request_rejects_empty_project_id() -> None:
    with pytest.raises(ValidationError):
        GraceBackfillRequest(
            provider="anthropic",
            model="claude",
            usage=UsageBody(input_tokens=1, output_tokens=1),
            project_id="",
        )


def test_reserve_response_serializes_ttl_deadline_as_iso8601() -> None:
    deadline = datetime(2026, 6, 23, 12, 10, tzinfo=UTC)
    response = ReserveResponse(
        reservation_id="r1",
        estimated_micro=300,
        price_book_version="pb-1",
        ttl_deadline=deadline,
    )
    dumped = response.model_dump(mode="json")
    assert datetime.fromisoformat(dumped["ttl_deadline"]) == deadline


def test_error_envelope_shape() -> None:
    envelope = ErrorEnvelope.model_validate({"error": {"code": "unknown_model", "message": "boom"}})
    assert envelope.error.code == "unknown_model"
    assert envelope.error.message == "boom"
