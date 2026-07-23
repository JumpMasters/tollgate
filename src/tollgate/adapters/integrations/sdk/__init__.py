"""The Tollgate SDK guard — reserve-before-dispatch enforcement for model calls.

``async with guard(client, ...)`` reserves worst-case budget before a call dispatches (deny →
never dispatch), heartbeats the reservation while it runs, and commits or cancels in a finally.
Fail-closed by default (a datastore outage raises :class:`EnforcementUnavailable`); opt-in grace
is a deferred future addition behind the same seam.
"""

from __future__ import annotations

from tollgate.adapters.integrations.sdk.client import (
    CancelResult,
    CommitResult,
    ExtendResult,
    MeterResult,
    ProviderUsage,
    ReserveResult,
    TollgateClient,
)
from tollgate.adapters.integrations.sdk.config import SdkConfig
from tollgate.adapters.integrations.sdk.errors import (
    AuthenticationFailed,
    BudgetDenied,
    BudgetNotFound,
    EnforcementUnavailable,
    IdempotencyKeyReuse,
    InternalError,
    InvalidRequest,
    NotAuthorized,
    ReservationNotHeld,
    TollgateApiError,
    UnknownModel,
)
from tollgate.adapters.integrations.sdk.guard import GuardedCall, guard
from tollgate.adapters.integrations.sdk.tokenizer import (
    HeuristicTokenizer,
    Tokenizer,
    input_bound_tokens,
    try_tiktoken,
)

__all__ = [
    "AuthenticationFailed",
    "BudgetDenied",
    "BudgetNotFound",
    "CancelResult",
    "CommitResult",
    "EnforcementUnavailable",
    "ExtendResult",
    "GuardedCall",
    "HeuristicTokenizer",
    "IdempotencyKeyReuse",
    "InternalError",
    "InvalidRequest",
    "MeterResult",
    "NotAuthorized",
    "ProviderUsage",
    "ReservationNotHeld",
    "ReserveResult",
    "SdkConfig",
    "Tokenizer",
    "TollgateApiError",
    "TollgateClient",
    "UnknownModel",
    "guard",
    "input_bound_tokens",
    "try_tiktoken",
]
