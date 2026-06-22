# 0009 — The CounterStore port as the Redis-ready seam

- Status: Accepted
- Date: 2026-06-22

## Context

V1 enforces on Postgres, whose throughput on a single hot parent row is bounded
by row-lock serialization: every reserve under a shared org budget contends for
the same row. A future Redis fast-path could raise that ceiling, but it must not
compromise the ledger of record or leak into the domain.

## Decision

Express the budget-balance primitives (`reserve`, `commit`, `release`) as a
`CounterStore` Protocol in the application layer. V1 ships `PostgresCounterStore`
(the guarded conditional writes of ADR 0004). A future `RedisCounterStore` —
sub-millisecond counters reconciled back to the Postgres ledger of record —
slots in behind the same port without touching ledger semantics or the domain.

## Consequences

- The hot-row ceiling is a measured, named limitation that the load harness
  quantifies, not a hidden one.
- Redis is deferred, not designed out; the seam exists from day one.
- The application depends only on the port, which import-linter enforces
  (ADR 0013).
