# 0027 — Reserve period_start is the current UTC calendar month

- Status: Accepted
- Date: 2026-06-23

## Context

A `budget_balance` is keyed by `(budget_id, period_start)`, and the multi-budget
reserve (plan 07) applies a **single** `period_start` to every applicable node in
one call. The schema (§3) defines two `period_kind`s — `calendar_month` and
`rolling_days` — but only `calendar_month` is fully specified: `rolling_days` has
**no anchor column** from which to derive a window's start (rolling from when?), so
its period start is undefined by the current schema. The reserve path nonetheless
needs one concrete `period_start` for the current period.

## Decision

V1 derives `period_start` as the **first instant of the current UTC calendar
month** (`domain.periods.calendar_month_start`), shared by every applicable budget
in a reserve. `rolling_days` remains a schema-level, forward-compatibility column;
selecting it on the reserve path is **deferred**.

- The derivation is pure and server-side (period roll is lazy, §5.5): the first
  reserve in a month lazily creates that month's balance row.
- Every applicable node in a single reserve shares the one `period_start`, which is
  exactly what plan 07's `reserve(nodes, period_start, amount)` expects.

## Consequences

- Budgets behave as calendar-month budgets in V1 regardless of stored `period_kind`;
  mixing period models within one reserve is not supported.
- Supporting `rolling_days` later needs a per-budget anchor (e.g. an epoch column)
  **and** a per-node `period_start` on the reserve path — a superseding record plus
  a schema migration and a change to the plan-07 reserve interface.
- The calendar month is taken in **UTC**; a deployment in another zone sees periods
  roll at UTC midnight on the 1st. Per-tenant period zones are out of V1 scope.
