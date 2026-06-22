# 0011 — Conservation and the per-node spend bound as an offline oracle

- Status: Accepted
- Date: 2026-06-22

## Context

The storage-tier `CHECK` constraints catch local violations (`reserved +
committed <= limit` on a single row), but not the lost-update or double-apply
bugs that can occur across a multi-budget reservation. Summing the append-only
ledger on the command path to catch those would be prohibitively expensive.

## Decision

Keep the ledger append-only and never sum it on the command path. Verify
conservation offline: for each `(budget, period)`, `Σ delta_reserved ==
reserved`, `Σ delta_committed == committed`, and `Σ delta_overage == overage`.
The load harness runs this oracle over the resulting ledger after a run, together
with the per-node spend bound — `committed <= limit` always, and `committed +
overage <= limit + Σ(audited per-call overage)` with every unit of overage backed
by a ledger row.

## Consequences

- Cross-row correctness is demonstrated against a real ledger, not asserted.
- The command path stays fast; verification is a bounded, post-run audit.
- Continuous large-scale verification would use periodic ledger snapshots, which
  is deferred beyond V1.
