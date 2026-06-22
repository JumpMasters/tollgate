# 0015 — Per-principal authentication and scope-based authorization

- Status: Accepted
- Date: 2026-06-22

## Context

A budget that gates an identity the caller merely asserts is not enforceable —
the caller could claim someone else's headroom. The usage numbers a caller
reports are equally untrustworthy: a client under budget pressure has every
incentive to under-report.

## Decision

Every command and read carries a bearer credential (API key or service token),
stored only as a salted hash. Tollgate looks up an active credential and
**derives** the acting `user → team → org` from it; the caller cannot assert a
different identity, and may name a `project` only if the credential's scope
authorizes it. The chargeback read API authorizes each query to budgets at or
below the credential's scope. Usage actuals on `commit` are taken server-side
from the provider-reported usage, never from caller-asserted numbers.

## Consequences

- Budgets gate the identity the credential proves, not one the caller claims.
- Tokens are never stored in the clear.
- Full multi-tenant isolation and SSO remain out of scope; this is per-principal
  authentication within a single deployment.
