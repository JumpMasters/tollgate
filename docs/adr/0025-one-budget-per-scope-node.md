# 0025 — At most one budget per scope node in V1

- Status: Accepted
- Date: 2026-06-22

## Context

A budget governs a scope node — an `org`, `team`, `user`, or `project`. The
`budget` table currently keys its uniqueness on `(scope_kind, scope_id,
period_kind)`, which permits a single node to carry two budgets at once: a
`calendar_month` limit *and* a `rolling_days` limit, with different `budget_id`.

The reservation model, however, is built around **one budget per node on the
enforcement path**. `resolve_applicable_set` assembles the ancestry path ∪ the
project budget; `lock_order_key` deliberately omits `period_start` because,
within a single reserve, every applicable balance shares one period; and
`budget_balance` is keyed `(budget_id, period_start)`, with a reserve stamping one
estimate against lines that all share that period. Two budgets on the same node
with different `period_kind` would resolve to different `period_start` values
within one reserve, which the single-period-per-reserve model cannot represent.

Because resolution de-duplicates by `(scope_kind, scope_id)` and keeps the first
node seen, a second budget on the same node is silently dropped rather than
enforced — a latent way to admit a reserve that a budget should have denied. The
schema permits a configuration the enforcement path cannot honour.

## Decision

V1 enforces **at most one budget per `(scope_kind, scope_id)` node.**

- A node's single budget is either calendar-month or rolling-days, never both.
- The `budget` uniqueness is `(scope_kind, scope_id)`; `period_kind` stays a
  column of that one budget. (Tightened from `(scope_kind, scope_id,
  period_kind)`.)
- Applicable-set resolution de-duplicates by `budget_id` — the row the store
  actually locks and updates per line — and treats a `(scope_kind, scope_id)`
  collision carrying a *different* `budget_id` as an error, not a silent
  first-wins. The database constraint and the resolution layer then agree, and any
  violation fails loudly rather than dropping a budget.

Charging multiple period budgets against one node (for example a monthly cap and
a rolling weekly cap on the same team) is **deferred**: it requires a reserve to
gate across more than one period per node, a change to the single-period-per-
reserve model that is out of V1 scope.

## Consequences

- "Gate against all applicable budgets" becomes exact: each node contributes
  exactly one budget, nothing is silently dropped, and the schema prevents the
  ambiguous configuration at the source instead of relying on the application to
  resolve it.
- `lock_order_key` may keep omitting `period_start` at the domain level — with one
  budget per node and one period per reserve, `(scope_kind rank, scope_id)` is
  already a total order over the applicable set. `period_start` remains the final
  tiebreak only in the storage-layer `ORDER BY` for cross-period scans (the
  reaper).
- This record **complements** 0005 (which fixed lock ordering and the applicable-
  set shape but left per-node cardinality implicit) and 0020 (empty set → deny);
  it supersedes neither.
- The implementation — tightening the `budget` UNIQUE and switching the resolution
  de-dup key to `budget_id` with a fail-fast on collision — is tracked in
  [#15](https://github.com/JumpMasters/tollgate/issues/15). Until it lands, the
  schema and resolution still permit the configuration this record rules out.
- If multi-period-per-node limits later become a requirement, revisit with a
  superseding record that extends the reserve to multiple periods per node (a
  roadmap item) rather than re-permitting silent drops.
