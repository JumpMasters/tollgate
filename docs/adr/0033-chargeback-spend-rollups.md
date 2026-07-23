# 0033 — Chargeback spend rollups over the ledger

- Status: Accepted
- Date: 2026-07-17

## Context

ADR 0032 delivered per-budget state and the by-scope rollup for free from
`budget_balance`. The remaining chargeback promise is spend broken
down by tags — provider, model, and label keys like env / cost-center — which
`budget_balance` cannot express, since it is not tagged. That breakdown lives
only in the append-only `ledger`, joined to the reservation for the model and
labels. Summing the ledger has so far been reserved for the offline conservation
oracle; a read API needs a bounded, deliberate exception.

## Decision

- **One read route:** `GET /v1/spend?group_by=<dim>` returns a scope node's
  realized spend for one period, grouped by `<dim>`, as
  `{period_start, group_by, groups: [{group, spend_micro}]}`. `group_by` is
  `provider`, `model`, or `label:<key>` (so env / cost-center are `label:env` /
  `label:cost-center`, and any custom label groups the same way). A malformed
  `group_by` is a `422`.
- **Realized spend** is `SUM(delta_committed_micro + delta_overage_micro)` over
  the node's rows; reserve/release/reap rows contribute zero and groups with
  zero total are omitted. Reserved is provisional and is not spend.
- **Single-node aggregation, not a subtree union.** A reservation writes a
  ledger row on every budget it drew on, so a node's own budget ledger already
  aggregates every reservation beneath it. The rollup sums exactly the requested
  node's budget rows — summing a subtree would multiply-count spend shared across
  ancestor budgets. Picking the node *is* choosing the rollup breadth.
- **Reads may sum the ledger, off the command path.** This is the first read to
  aggregate the append-only ledger. The command path still never sums it; this
  exception is a read-only `SELECT` and mutates nothing.
- **Unattributable spend is an explicit null group.** `provider` is on every
  ledger row; `model` and labels come from a `LEFT JOIN` to the reservation, so
  a grace-backfill row (no reservation) — or a reservation missing the requested
  label key — falls into the `group: null` bucket. Every rollup's groups
  therefore sum to the node's total realized spend, reconciling with the state
  read.
- **Authorization and period** match ADR 0032: the node must be at or below the
  credential's scope (server-derived ancestry, identical 401/403, no existence
  leak); the period is the current UTC calendar month, with an optional
  `period_start` snapped to its month.

## Consequences

- The chargeback surface is complete: per-budget state, by-scope rollups (both
  from `budget_balance`), and tag rollups (from the ledger).
- The ledger's `(budget_id, ts)` index does not cover `(budget_id,
  period_start)` filtering with a reservation join; at V1 data volumes this is a
  bounded per-node scan. A covering index is a future optimization if needed.
- Token-level rollups are deliberately out of scope: overage rows repeat their
  commit sibling's token counts, so token sums would need per-kind care; only
  monetary spend is exposed here.
