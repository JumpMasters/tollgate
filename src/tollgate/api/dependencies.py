"""Route dependencies: bearer-credential authentication (ADR 0031).

The composition root places an authenticate callable on ``app.state``; the
dependency extracts the RFC 6750 bearer token and resolves it to an
``AuthContext`` before any handler runs. Every failure - missing header,
wrong scheme, empty or unrecognized token - is rejected identically as 401,
giving probes no distinguishing signal.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from tollgate.application.auth import AuthContext
from tollgate.domain.errors import AuthenticationFailed

type TokenAuthenticator = Callable[[str], Awaitable[AuthContext]]

_bearer = HTTPBearer(auto_error=False)


async def authenticated(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> AuthContext:
    """Resolve the request's bearer token to an ``AuthContext``, or reject with 401."""
    if credentials is None or not credentials.credentials:
        raise AuthenticationFailed
    authenticate: TokenAuthenticator = request.app.state.authenticate
    return await authenticate(credentials.credentials)


RequestAuth = Annotated[AuthContext, Depends(authenticated)]
