# 0029 — Self-healing late commit applies spend against live remaining

- Status: Accepted
- Date: 2026-07-16

## Context

A reservation can be reaped while its call is in fact still alive — a stream slower
than the TTL, a missed heartbeat — and the design requires that a later `commit` for that
reservation records the real, already-incurred spend rather than silently no-oping.
Three mechanics are left open by the design:

1. **Exactly-once.** The `held → committed` identity guard cannot make a late commit
   exactly-once: the status is already `reaped`, so the guard matches zero rows for
   *every* attempt, and nothing distinguishes the first late commit from a duplicate
   under a fresh idempotency key.
2. **Balances.** The reap already released the estimate. Unconditionally adding the
   actual to `committed_micro` could violate the row CHECK
   (`reserved + committed <= limit`) on a node whose freed headroom was re-reserved
   in the meantime — and an aborted transaction would lose the spend record.
3. **The result.** Per-node headroom differs, so the committed/overage split differs
   per node; `CommitResult` carries a single pair.

## Decision

1. `reaped → committed` becomes a **legal transition** (the lifecycle diagram
   already draws `reaped → late commit → committed`). The late commit claims it with
   a second guarded UPDATE — `claim_late_commit`: `... WHERE status = 'reaped'` —
   the same identity-guard mechanism as `held → terminal`, so exactly one late
   commit wins. `committed` and `released` remain dead ends.
2. The actual cost is applied per line against the line's **own**
   `(budget_id, period_start)` — the `reservation_line` is the sole fan-out record,
   replayed exactly — via a new `CounterStore` primitive **`apply_spend`**:
   committed takes `min(actual, remaining)` where
   `remaining = max(limit − reserved − committed − overage, 0)` (the same
   *remaining* the reserve guard enforces), and the excess is recorded as audited
   overage. The row CHECK holds by construction and the transaction never aborts, so
   real spend is always recorded — against live headroom when there is any, as
   overage when there is none.
3. `CommitResult` reports the **most-restrictive node's** split — the greatest
   per-node overage, with `committed = actual − overage` — so
   `committed + overage == actual` on both commit paths. Per-node splits are on the
   ledger (`commit_adjust` / `overage` rows, `ref = 'late_commit'`,
   `delta_reserved = 0` because the reap already released the hold).

## Consequences

- `reaped` is no longer terminal in the domain state machine; "exactly one terminal
  effect" still holds per *effect*: the reap released the estimate exactly once, the
  late commit records the spend exactly once, and their ledger deltas conserve.
- `apply_spend` reads the balance row `FOR UPDATE` and updates it under that lock —
  a locked read-modify-write rather than the hot path's single guarded statement,
  because the caller needs the split back for its ledger rows. The lock is held to
  COMMIT, so there is no read-modify-write gap; the path is a rare recovery path,
  not the reserve hot path.
- A commit arriving after a late commit finds `status = 'committed'` and is rejected
  with `ReservationNotHeld` unless it replays its own idempotency key.
- Grace backfill (ADR 0030) reuses `apply_spend` for the identical
  no-reservation-headroom problem.
