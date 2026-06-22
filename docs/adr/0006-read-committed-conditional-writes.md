# 0006 — READ COMMITTED with conditional writes on the hot paths

- Status: Accepted
- Date: 2026-06-22

## Context

Stronger isolation (`SERIALIZABLE`) would rule out anomalies wholesale, but it
adds serialization failures and retries under exactly the contention the hot
paths see most.

## Decision

Run the hot paths at `READ COMMITTED`. The conditional `WHERE` on each `UPDATE`
is the guard and re-checks against the locked, committed row, so there is no
read-modify-write gap that would require `SERIALIZABLE`. The locks each `UPDATE`
takes are held to commit, so the all-or-nothing reservation set is evaluated
against a stable, serialized view of the contended rows.

## Consequences

- No `SERIALIZABLE` retry overhead on the path that matters most.
- Correctness rests on the conditional writes, not on the isolation level —
  which is precisely where the load harness aims its falsification controls.
- Read endpoints that need a point-in-time view use an explicit repeatable-read
  snapshot for reporting, separate from the hot path.
