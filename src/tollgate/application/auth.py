"""Credential authentication and scope authorization (§5.0).

Authentication is a precondition that runs **before** any command transaction: hash the
presented bearer token, look the credential up through the :class:`CredentialRepository` port,
reject anything not matching an **active** row, and derive the acting principal — an identity the
caller can never assert. :func:`hash_token` is the deterministic keyed hash that makes the
single-column lookup possible (ADR 0026); the raw token is never stored.
"""

from __future__ import annotations

import hashlib
import hmac


def hash_token(token: str, *, secret: str) -> str:
    """Return the deterministic keyed hash of a bearer ``token`` (ADR 0026).

    HMAC-SHA-256 under a server-held ``secret`` (the pepper), hex-encoded. Deterministic, so a
    presented token hashes to the same value every time and the credential is found by one
    equality lookup on ``api_credential.token_hash``; keyed, so a leak of the hash column alone
    cannot be reversed without the secret. The raw token is never stored; this same function both
    authenticates a presented token and mints a credential's stored hash, so the two can never
    diverge.
    """
    return hmac.new(secret.encode("utf-8"), token.encode("utf-8"), hashlib.sha256).hexdigest()
