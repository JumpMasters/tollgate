# 0004 — Invariant-guarded conditional writes for budget balances

- Status: Accepted
- Date: 2026-06-22

## Context

Many concurrent reservers compete for the same budget row. A read-modify-write
(read the balance, check headroom, then write) has a gap in which two reservers
both observe headroom and both write, breaching the limit. A version column with
optimistic concurrency control closes the gap, but makes contended reservers
read the same version, lose the compare-and-set, and retry — a retry storm on
the hot row with no correctness benefit.

## Decision

Mutate budget balances with a single conditional `UPDATE` whose `WHERE` clause is
the guard:

```sql
UPDATE budget_balance
   SET reserved_micro = reserved_micro + :est
 WHERE budget_id = :b AND period_start = :p
   AND limit_micro - reserved_micro - committed_micro - overage_micro >= :est;
```

Zero rows updated means no headroom — deny, definitively, with no retry. There
is no version column on the hot path. Storage-tier `CHECK` constraints make
over-reserve and over-commit impossible at the row level even under a bug.

## Consequences

- No read-modify-write gap: the database evaluates the guard against the locked,
  committed row.
- Non-violating reserves serialize on the row lock and all succeed; only
  genuinely over-budget ones fail.
- The conditional write beats version-column OCC under contention, which is one
  of the comparisons the load harness measures.
