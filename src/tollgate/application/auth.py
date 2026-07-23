"""Credential authentication and scope authorization.

Authentication is a precondition that runs **before** any command transaction: hash the
presented bearer token, look the credential up through the :class:`CredentialRepository` port,
reject anything not matching an **active** row, and derive the acting :class:`Principal`
(``user -> team -> org``) — an identity the caller can never assert. :func:`hash_token` is the
deterministic keyed hash that makes the single-column lookup possible (ADR 0026); the raw token
is never stored. Authorization of a named project or a read scope is the pure
:func:`tollgate.domain.credentials.authorizes` predicate, wrapped by :func:`require_scope` into a
typed error.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass

from tollgate.application.ports import CredentialRepository
from tollgate.domain.credentials import (
    Credential,
    CredentialStatus,
    Principal,
    authorizes,
)
from tollgate.domain.errors import AuthenticationFailed, ScopeNotAuthorized
from tollgate.domain.scopes import ScopeKind


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


@dataclass(frozen=True, slots=True)
class AuthContext:
    """The result of authenticating a command: the credential and its derived principal."""

    credential: Credential
    principal: Principal


class CredentialAuthenticator:
    """Resolves a bearer token to an :class:`AuthContext`, or rejects it.

    ``token_secret`` is the server pepper injected at the composition root (never imported from
    ``config`` — boundary). The authenticator runs entirely *before* the command transaction.
    """

    def __init__(self, credentials: CredentialRepository, *, token_secret: str) -> None:
        self._credentials = credentials
        self._token_secret = token_secret

    async def authenticate(self, presented_token: str) -> AuthContext:
        """Authenticate ``presented_token`` before any transaction opens.

        Raises :class:`AuthenticationFailed` if the token matches no credential, the matched
        credential is not ``active``, or its principal cannot be loaded — all rejected identically
        so the failure never reveals which credentials exist.
        """
        token_hash = hash_token(presented_token, secret=self._token_secret)
        credential = await self._credentials.find_by_token_hash(token_hash)
        if credential is None or credential.status is not CredentialStatus.ACTIVE:
            raise AuthenticationFailed
        principal = await self._credentials.load_principal(credential.principal_id)
        if principal is None:
            raise AuthenticationFailed
        return AuthContext(credential=credential, principal=principal)


def require_scope(
    credential: Credential,
    target_ancestry: Mapping[ScopeKind, str],
    *,
    target: str,
) -> None:
    """Raise :class:`ScopeNotAuthorized` unless ``credential`` covers the target node.

    ``target_ancestry`` describes the node being accessed (see
    :func:`tollgate.domain.credentials.authorizes`); ``target`` is the human-readable scope used
    in the error (e.g. ``"project:checkout"``).
    """
    if not authorizes(credential, target_ancestry):
        raise ScopeNotAuthorized(target)
