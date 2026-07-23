"""Wire schemas for the command routes (ADR 0031).

Request models mirror the domain command types field-for-field, minus the
``Idempotency-Key`` (which travels as a header), and reject
unknown fields so a client typo surfaces as a 422 instead of silently
weakening enforcement. Response models mirror the domain result types;
``datetime`` fields serialize as ISO 8601.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

#: Generous but finite ceiling on any single token-count field. Comfortably above
#: any real request, yet small enough that a worst-case estimate stays within the
#: signed int8 balance/ledger columns for any realistic price -- so oversized wire
#: input becomes a clean 422 instead of an overflow 500 (#66).
MAX_TOKENS: Final = 1_000_000_000

#: Upper bound on identifier-like strings (provider, model, reservation/project ids,
#: and the Idempotency-Key header). Each is written verbatim to a row, so the cap
#: bounds the per-request storage and memory cost (#68).
MAX_STR_LEN: Final = 256

#: Bounds on chargeback labels: the number of entries, and each key/value length (#68).
MAX_LABELS: Final = 64
MAX_LABEL_KEY_LEN: Final = 128
MAX_LABEL_VALUE_LEN: Final = 256

_LabelKey = Annotated[str, StringConstraints(min_length=1, max_length=MAX_LABEL_KEY_LEN)]
_LabelValue = Annotated[str, StringConstraints(max_length=MAX_LABEL_VALUE_LEN)]


class _RequestModel(BaseModel):
    """Base for request bodies: unknown fields are rejected (ADR 0031)."""

    model_config = ConfigDict(extra="forbid")


class UsageBody(_RequestModel):
    """Provider-reported token usage (never caller-asserted amounts)."""

    input_tokens: int = Field(ge=0, le=MAX_TOKENS)
    output_tokens: int = Field(ge=0, le=MAX_TOKENS)
    cached_input_tokens: int = Field(default=0, ge=0, le=MAX_TOKENS)
    cache_creation_tokens: int = Field(default=0, ge=0, le=MAX_TOKENS)

    @model_validator(mode="after")
    def _cached_within_input(self) -> UsageBody:
        """The cached subset can never exceed the input it is drawn from."""
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("cached_input_tokens cannot exceed input_tokens")
        return self


class ReserveRequest(_RequestModel):
    """Body of ``POST /v1/reserve``."""

    provider: str = Field(min_length=1, max_length=MAX_STR_LEN)
    model: str = Field(min_length=1, max_length=MAX_STR_LEN)
    input_bound_tokens: int = Field(ge=0, le=MAX_TOKENS)
    max_output_tokens: int = Field(ge=0, le=MAX_TOKENS)
    labels: dict[_LabelKey, _LabelValue] = Field(default_factory=dict, max_length=MAX_LABELS)
    project_id: str | None = Field(default=None, min_length=1, max_length=MAX_STR_LEN)


class CommitRequest(_RequestModel):
    """Body of ``POST /v1/commit``."""

    reservation_id: str = Field(min_length=1, max_length=MAX_STR_LEN)
    usage: UsageBody


class CancelRequest(_RequestModel):
    """Body of ``POST /v1/cancel``."""

    reservation_id: str = Field(min_length=1, max_length=MAX_STR_LEN)


class ExtendRequest(_RequestModel):
    """Body of ``POST /v1/extend``."""

    reservation_id: str = Field(min_length=1, max_length=MAX_STR_LEN)


class GraceBackfillRequest(_RequestModel):
    """Body of ``POST /v1/grace-backfill``."""

    provider: str = Field(min_length=1, max_length=MAX_STR_LEN)
    model: str = Field(min_length=1, max_length=MAX_STR_LEN)
    usage: UsageBody
    project_id: str | None = Field(default=None, min_length=1, max_length=MAX_STR_LEN)


class MeterRequest(_RequestModel):
    """Body of ``POST /v1/meter``."""

    provider: str = Field(min_length=1, max_length=MAX_STR_LEN)
    model: str = Field(min_length=1, max_length=MAX_STR_LEN)
    usage: UsageBody
    labels: dict[_LabelKey, _LabelValue] = Field(default_factory=dict, max_length=MAX_LABELS)
    project_id: str | None = Field(default=None, min_length=1, max_length=MAX_STR_LEN)
    truncated: bool = False


class ReserveResponse(BaseModel):
    """Success body of ``POST /v1/reserve``."""

    reservation_id: str
    estimated_micro: int
    price_book_version: str
    ttl_deadline: datetime


class CommitResponse(BaseModel):
    """Success body of ``POST /v1/commit``."""

    reservation_id: str
    committed_micro: int
    overage_micro: int


class CancelResponse(BaseModel):
    """Success body of ``POST /v1/cancel``."""

    reservation_id: str
    released_micro: int


class ExtendResponse(BaseModel):
    """Success body of ``POST /v1/extend``."""

    reservation_id: str
    ttl_deadline: datetime


class GraceBackfillResponse(BaseModel):
    """Success body of ``POST /v1/grace-backfill``."""

    actual_micro: int
    price_book_version: str


class MeterResponse(BaseModel):
    """Success body of ``POST /v1/meter``."""

    actual_micro: int
    price_book_version: str


class BudgetAlertState(BaseModel):
    """One configured soft threshold and whether current utilization has reached it."""

    threshold_pct: int
    crossed: bool


class BudgetStateResponse(BaseModel):
    """One budget node's current-period state in a chargeback read."""

    scope_kind: str
    scope_id: str
    limit_micro: int
    reserved_micro: int
    committed_micro: int
    overage_micro: int
    remaining_micro: int
    utilization_pct: int
    alerts: list[BudgetAlertState]


class BudgetStatesResponse(BaseModel):
    """Body of ``GET /v1/budgets``: the states at or below the credential's scope."""

    period_start: datetime
    budgets: list[BudgetStateResponse]


class SpendGroupResponse(BaseModel):
    """One group of a spend rollup; ``group`` is null for unattributed spend."""

    group: str | None
    spend_micro: int


class SpendRollupResponse(BaseModel):
    """Body of ``GET /v1/spend``: realized spend for a node, grouped by a dimension."""

    period_start: datetime
    group_by: str
    groups: list[SpendGroupResponse]


class ErrorBody(BaseModel):
    """The ``error`` object inside every error envelope (ADR 0031)."""

    code: str
    message: str


class ErrorEnvelope(BaseModel):
    """Every non-2xx body from the command routes: ``{"error": {...}}``."""

    error: ErrorBody
