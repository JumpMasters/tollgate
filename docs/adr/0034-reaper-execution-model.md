# 34. Reaper execution model: polled run-once ticks reusing the command UnitOfWork

Date: 2026-07-18

## Status

Accepted

## Context

ADR 0008 established the reaper architecture: reservations carry a TTL; a polled reservation reaper
releases abandoned held reservations in bounded per-item transactions using `FOR UPDATE SKIP
LOCKED`; a second reaper batch-deletes idempotency keys past a 24h retention; an index on
`(status, ttl_deadline)` keeps the scan cheap; and the reaper composes with the heartbeat and
self-healing late commit (ADR 0018, refined by ADR 0029). The reaper workers implement that
architecture. This record captures the execution-model decisions ADR 0008 left open — how a tick is
structured, which transaction seam it uses, and where the worker code lives given the import-linter
boundary
(ADR 0013). It refines ADR 0008; it does not supersede it.

## Decision

- **Pure tick + thin loop.** Each reaper is a pure `run_once()` handler in
  `application/handlers/reap.py` (over ports only), wrapped by one shared polling loop `run_forever`
  in `workers/runner.py`. Scheduling is separated from work: `run_once()` waits for nothing and is
  exercised directly in tests; `run_forever` polls it on an interval, catches and logs per-tick
  exceptions (a backstop must not die on one bad poll), and wakes immediately on SIGINT/SIGTERM.
- **Reuse the command `UnitOfWork`.** Both reapers open the existing command
  `UnitOfWork`/`CommandContext` — one transaction per reaped reservation, one per delete batch —
  rather than a separate reaper transaction seam. The reservation reaper's claim-and-reap is a
  single `UPDATE reservation SET status='reaped' WHERE reservation_id = (SELECT ... WHERE
  status='held' AND ttl_deadline < now ORDER BY ttl_deadline FOR UPDATE SKIP LOCKED LIMIT 1)
  RETURNING ...`, and it releases the reservation's lines in the canonical lock order within
  that same transaction. The status flip (`held → reaped`) is the exactly-once guard, so a mainline
  commit that loses the race routes to the ADR 0029 self-heal; taking the reservation-row lock
  before the balance rows (in canonical order) means the reaper forms no lock cycle with
  commit/cancel.
- **Placement forced by the boundary (ADR 0013).** Because `workers` must not import `adapters`, the
  reap logic lives in `application` (ports only) and the concrete wiring + process entrypoints live
  in the composition root `app.py`, exposed as console scripts (`tollgate-reservation-reaper`,
  `tollgate-idempotency-reaper`). The dependency direction is `app → workers`, mirroring
  `app → api`; `workers/` imports only the standard library.
- **`ttl_deadline < now` is "no recent heartbeat."** `extend` advances `ttl_deadline` monotonically,
  so the reaper needs no separate last-heartbeat column: a reservation is abandoned exactly when its
  (possibly heartbeat-extended) deadline has passed.

## Consequences

- No migration: ADR 0008's `(status, ttl_deadline)` index, `idempotency_key.created_at`, and
  `LedgerKind.reap` already exist. Deleting an aged idempotency key is safe because
  `reservation.idempotency_key` is UNIQUE, not a foreign key.
- Cadence and batch sizes are configuration; the `reaper_batch_size` and `idempotency_reaper_batch_size`
  fields carry a `ge=1` floor, which also guarantees the idempotency reaper's drain loop terminates
  (a `batch_size` of 0 would spin). Operators may instead drive a single `run_once()` from an
  external scheduler.
- The tick is idempotent and crash-safe: a process death mid-tick rolls back only the in-flight
  per-item transaction; the next tick resumes.
- `run_once()` is exercised directly in unit and integration tests without sleeping; the shared
  `run_forever` loop is tested with a zero interval and a stop-setting fake tick.
