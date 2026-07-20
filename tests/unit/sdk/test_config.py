"""Tests for SdkConfig: bearer-token redaction (#95) and constructor validation (#107)."""

from __future__ import annotations

import pytest

from tollgate.adapters.integrations.sdk.config import SdkConfig


def test_repr_does_not_leak_the_bearer_token() -> None:
    config = SdkConfig(base_url="http://tollgate.test", token="super-secret-token")
    rendered = repr(config)
    assert "super-secret-token" not in rendered
    # Non-secret fields remain visible for debugging; only the token is dropped from the repr.
    assert "base_url=" in rendered
    assert "token=" not in rendered


def test_token_is_still_readable_at_the_http_boundary() -> None:
    config = SdkConfig(base_url="http://tollgate.test", token="tok-1")
    assert config.token == "tok-1"


def test_rejects_negative_provider_margin_tokens() -> None:
    # A negative margin under-reserves — the unsafe direction — so it must be rejected.
    with pytest.raises(ValueError, match="provider_margin_tokens"):
        SdkConfig(base_url="http://t", token="tok", provider_margin_tokens=-1)


def test_rejects_non_positive_connect_timeout() -> None:
    with pytest.raises(ValueError, match="connect_timeout_seconds"):
        SdkConfig(base_url="http://t", token="tok", connect_timeout_seconds=0.0)


def test_rejects_non_positive_read_timeout() -> None:
    with pytest.raises(ValueError, match="read_timeout_seconds"):
        SdkConfig(base_url="http://t", token="tok", read_timeout_seconds=-1.0)


def test_rejects_non_positive_default_max_output_tokens() -> None:
    with pytest.raises(ValueError, match="default_max_output_tokens"):
        SdkConfig(base_url="http://t", token="tok", default_max_output_tokens=0)


def test_rejects_negative_heartbeat_interval() -> None:
    with pytest.raises(ValueError, match="heartbeat_interval_seconds"):
        SdkConfig(base_url="http://t", token="tok", heartbeat_interval_seconds=-1.0)


@pytest.mark.parametrize("base_url", ["", "ftp://host", "tollgate.test", "ws://host"])
def test_rejects_non_http_base_url(base_url: str) -> None:
    with pytest.raises(ValueError, match="base_url"):
        SdkConfig(base_url=base_url, token="tok")


@pytest.mark.parametrize("base_url", ["http://host", "https://host/path"])
def test_accepts_http_and_https_base_urls(base_url: str) -> None:
    config = SdkConfig(base_url=base_url, token="tok")
    assert config.base_url == base_url
