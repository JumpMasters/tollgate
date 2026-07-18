"""Tests for the wire schemas (ADR 0031)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from tollgate.api.schemas import ErrorEnvelope, ReserveRequest, ReserveResponse, UsageBody


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
