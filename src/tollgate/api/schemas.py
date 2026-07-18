"""Wire schemas for the command routes (ADR 0031).

Request models mirror the domain command types field-for-field, minus the
``Idempotency-Key`` (which travels as a header, section 5.1), and reject
unknown fields so a client typo surfaces as a 422 instead of silently
weakening enforcement. Response models mirror the domain result types;
``datetime`` fields serialize as ISO 8601.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class _RequestModel(BaseModel):
    """Base for request bodies: unknown fields are rejected (ADR 0031)."""

    model_config = ConfigDict(extra="forbid")


class UsageBody(_RequestModel):
    """Provider-reported token usage (section 4: never caller-asserted amounts)."""

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)


class ReserveRequest(_RequestModel):
    """Body of ``POST /v1/reserve``."""

    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    input_bound_tokens: int = Field(ge=0)
    max_output_tokens: int = Field(ge=0)
    labels: dict[str, str] = Field(default_factory=dict)
    project_id: str | None = None


class CommitRequest(_RequestModel):
    """Body of ``POST /v1/commit``."""

    reservation_id: str = Field(min_length=1)
    usage: UsageBody


class CancelRequest(_RequestModel):
    """Body of ``POST /v1/cancel``."""

    reservation_id: str = Field(min_length=1)


class ExtendRequest(_RequestModel):
    """Body of ``POST /v1/extend``."""

    reservation_id: str = Field(min_length=1)


class GraceBackfillRequest(_RequestModel):
    """Body of ``POST /v1/grace-backfill``."""

    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    usage: UsageBody
    project_id: str | None = None


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


class BudgetAlertState(BaseModel):
    """One configured soft threshold and whether current utilization has reached it (section 3)."""

    threshold_pct: int
    crossed: bool


class BudgetStateResponse(BaseModel):
    """One budget node's current-period state in a chargeback read (section 2)."""

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
    """Body of ``GET /v1/budgets``: the states at or below the credential's scope (section 2)."""

    period_start: datetime
    budgets: list[BudgetStateResponse]


class SpendGroupResponse(BaseModel):
    """One group of a spend rollup (section 2); ``group`` is null for unattributed spend."""

    group: str | None
    spend_micro: int


class SpendRollupResponse(BaseModel):
    """Body of ``GET /v1/spend``: realized spend for a node, grouped by a dimension (section 2)."""

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
