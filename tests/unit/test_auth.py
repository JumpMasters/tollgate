"""Tests for credential authentication: the keyed token hash (and, later, the authenticator)."""

from __future__ import annotations

from tollgate.application.auth import hash_token

_SECRET = "pepper"


def test_hash_token_is_deterministic() -> None:
    assert hash_token("tok", secret=_SECRET) == hash_token("tok", secret=_SECRET)


def test_hash_token_depends_on_the_token() -> None:
    assert hash_token("tok-a", secret=_SECRET) != hash_token("tok-b", secret=_SECRET)


def test_hash_token_depends_on_the_secret() -> None:
    assert hash_token("tok", secret="pepper-a") != hash_token("tok", secret="pepper-b")


def test_hash_token_is_a_sha256_hex_digest() -> None:
    digest = hash_token("tok", secret=_SECRET)
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


def test_hash_token_never_contains_the_raw_token() -> None:
    assert "tok" not in hash_token("tok", secret=_SECRET)
