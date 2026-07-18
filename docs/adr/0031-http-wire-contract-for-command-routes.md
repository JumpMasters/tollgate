# 0031 — HTTP wire contract for the command routes

- Status: Accepted
- Date: 2026-07-17

## Context

The design locks the command semantics — bearer-credential authentication
(§5.0), an `Idempotency-Key` on every mutation (§5.1), most-restrictive
denial naming the binding node (§4, §5.3), and a thin FastAPI `api/` layer
(§6) — but deliberately leaves the wire contract open: URL shape, HTTP
methods, status codes, header syntax, and body schemas. The command routes
need those pinned, and they become hard to change the moment an SDK or any
external caller depends on them.

## Decision

- **RPC-style command paths under a version prefix:** `POST /v1/reserve`,
  `POST /v1/commit`, `POST /v1/cancel`, `POST /v1/extend`,
  `POST /v1/grace-backfill`. Commands are verbs, not resources; POST-only
  keeps idempotency in the command layer (§5.1), where it already lives.
  `/healthz` stays unversioned — infrastructure, not contract.
- **Bearer credentials per RFC 6750:** `Authorization: Bearer <token>`.
  Every authentication failure — missing header, wrong scheme, empty,
  unknown, or revoked token — is 401 with `WWW-Authenticate: Bearer` and an
  identical body, giving probes no distinguishing signal (§5.0).
- **`Idempotency-Key` is a required request header** on reserve, commit,
  cancel, and grace-backfill (Stripe-style, §5.1). Extend takes none — it
  is naturally idempotent (§4); an Idempotency-Key sent to extend is
  ignored. The key never appears in request bodies.
- **Uniform `200 OK` success responses** whose bodies mirror the domain
  result types field-for-field (`datetime` as ISO 8601). A replayed command
  returns the stored response, so the status must not depend on
  first-versus-replayed; a uniform 200 removes that dependence.
- **Error envelope** `{"error": {"code": "<snake_case>", "message": "…"}}`
  on every mapped non-2xx. `message` is the exception text (it names the
  binding node on a denial, §4); `code` is the stable programmatic key.
- **Typed-error to status mapping** (unmapped `TollgateError` subtypes fail
  closed to `500` / `internal_error`):

  | Domain error | Status | `code` |
  |---|---|---|
  | `AuthenticationFailed` | 401 | `authentication_failed` |
  | `InsufficientBudget` | 402 | `insufficient_budget` |
  | `ScopeNotAuthorized` | 403 | `scope_not_authorized` |
  | `BudgetNotFound` | 403 | `budget_not_found` |
  | `IdempotencyKeyReuse` | 409 | `idempotency_key_reuse` |
  | `ReservationNotHeld` | 409 | `reservation_not_held` |
  | `UnknownModel` | 422 | `unknown_model` |
  | `ConflictingBudgetScope` | 500 | `conflicting_budget_scope` |
  | `EnforcementUnavailable` | 503 | `enforcement_unavailable` |

  402 marks the budget denial distinctly: it is retryable under the same
  key (denials are never cached, §5.1), unlike the validation rejections.
  `BudgetNotFound` is 403 because default-deny is a policy outcome (§5.3),
  not exhaustion. `UnknownModel` shares 422 with framework validation; the
  envelope's `code` disambiguates.
- **Request bodies reject unknown fields** (`extra="forbid"`): on a spend
  gate, a silently dropped misspelled field (say `max_output_token`) would
  weaken enforcement invisibly.
- **Grace backfill is a first-class command route.** The handler exists
  (ADR 0030) and the SDK's reconnection path (§5.6) needs an HTTP target;
  the SDK integration must not grow routes of its own.

## Consequences

- The SDK and the chargeback read API compose onto a stable, documented
  contract; FastAPI's OpenAPI output describes it for free.
- Identical 401s and identical unknown/foreign 403s keep §5.0's
  no-existence-leak property at the wire.
- FastAPI's request-validation 422s use its default `{"detail": …}` body,
  not the envelope, so two error shapes exist; clients should treat any
  non-2xx without an `error` key as a validation failure. Folding
  validation errors into the envelope is a possible follow-up.
- 402 Payment Required is formally "reserved for future use" (RFC 9110);
  it is nonetheless the de-facto quota/billing signal and unambiguous here.
- The response stored for idempotent replay (§5.1) is the handler-level
  JSON, so a replayed wire body is identical by construction.
