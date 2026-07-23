# 0030 — Grace backfill resolves budgets, price, and period at backfill time

- Status: Accepted
- Date: 2026-07-16

## Context

Under opt-in grace the SDK may dispatch calls while enforcement is
unreachable and must backfill the spend once connectivity returns. A grace window
has no reservation: nothing stamped a price-book version, and no reservation lines
pinned budgets or a period. The backfill command needs a cost basis, a budget set,
and a period — and a rule for a request no budget governs.

## Decision

The backfill command carries **provider-reported usage** (token counts — never a
caller-asserted amount, matching the commit trust model). The handler
resolves everything server-side at backfill time:

- the **applicable set** — credential ancestry ∪ authorized project, the same
  policy and in-transaction authorization re-check as reserve;
- the **price** — the current (latest-published) price-book version, ADR 0028;
- the **period** — the current UTC calendar month, ADR 0027, lazily rolled with
  `ensure_period`.

Spend lands via the same live-remaining split as the late commit (ADR 0029):
committed up to `remaining`, the excess as audited overage. One `grace_backfill`
ledger row per node carries both deltas plus the token counts, provider, and the
stamped version; `reservation_id` is NULL. An **empty applicable set is rejected**
(`BudgetNotFound`): with no governing budget there is no balance to reconcile
against — the mirror of default-deny.

## Consequences

- Spend from a window that spanned a period roll or a price-book publication lands
  against the period/version current at backfill, not at dispatch; the drift is
  bounded by the outage length. The recorded token counts allow cost
  re-derivation under any version for calls without cached input; the
  cached-input subset is not a ledger column, so re-pricing a cached call
  exactly would need it — an additive follow-up, like the grace-cap column.
- The backfill is exactly-once by idempotency key; the SDK must persist
  the key alongside the tracked usage so a crash cannot double-backfill.
- A denial (unknown model, unauthorized project, empty set) rolls back and must be
  surfaced by the SDK — grace spend that cannot be attributed is an operational
  alert, never silent loss.
- The per-budget grace *cap* is tracked and enforced SDK-side during the outage;
  the `budget` schema carries no grace column in V1, so the server does
  not re-validate the cap at backfill. Server-side cap validation would be an
  additive follow-up (schema column + a guard here).
