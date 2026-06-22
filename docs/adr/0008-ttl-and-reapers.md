# 0008 — TTL and reapers for orphaned reservations and idempotency keys

- Status: Accepted
- Date: 2026-06-22

## Context

A client can crash or abandon a stream after reserving, leaving headroom held
forever. Idempotency keys accumulate without bound if never collected.

## Decision

Give reservations a TTL (default 10 minutes, configurable) via `ttl_deadline`,
and run a polled reaper that releases held reservations past their deadline with
no recent heartbeat, in bounded per-item transactions using `FOR UPDATE SKIP
LOCKED`. A second reaper batch-deletes idempotency keys past a 24-hour retention.
Reaper-freed headroom is reservable as soon as the reaper transaction commits —
there is no separate propagation step. An index on `(status, ttl_deadline)`
keeps the reaper's scan cheap.

## Consequences

- Abandoned reservations cannot strand budget indefinitely.
- The reaper composes with the heartbeat and self-healing late commit
  (ADR 0018) so a slow-but-live stream is not reaped into lost or double-counted
  spend.
- Bounded per-item transactions avoid one giant fan-out and keep the workers
  restartable and idempotent.
