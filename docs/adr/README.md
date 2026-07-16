# Architecture Decision Records

This directory records the significant, hard-to-reverse decisions made while
building Tollgate. Each record captures the context, the decision, and its
consequences, so the reasoning is available later even when the people change.

The format follows Michael Nygard's
[Documenting Architecture Decisions](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions.html).

## Index

- [0001 — Record architecture decisions](0001-record-architecture-decisions.md)
- [0002 — Relational, transactional core over event-sourcing](0002-relational-transactional-core.md)
- [0003 — Pre-charge reservation with reconcile-on-commit](0003-pre-charge-reservation-reconcile-on-commit.md)
- [0004 — Invariant-guarded conditional writes for budget balances](0004-invariant-guarded-conditional-writes.md)
- [0005 — Hierarchical reservation with deterministic lock ordering](0005-hierarchical-reservation-deterministic-lock-ordering.md)
- [0006 — READ COMMITTED with conditional writes on the hot paths](0006-read-committed-conditional-writes.md)
- [0007 — Idempotency caching policy](0007-idempotency-caching-policy.md)
- [0008 — TTL and reapers for orphaned reservations and idempotency keys](0008-ttl-and-reapers.md)
- [0009 — The CounterStore port as the Redis-ready seam](0009-counterstore-redis-ready-seam.md)
- [0010 — Cost normalization to micro-USD via an immutable, versioned price book](0010-micro-usd-immutable-versioned-price-book.md)
- [0011 — Conservation and the per-node spend bound as an offline oracle](0011-conservation-offline-oracle.md)
- [0012 — Gateway-neutral control plane, not a proxy](0012-gateway-neutral-control-plane.md)
- [0013 — Import-linter boundary enforcement](0013-import-linter-boundaries.md)
- [0014 — UUIDv7 ledger identifiers](0014-uuidv7-ledger-ids.md)
- [0015 — Per-principal authentication and scope-based authorization](0015-per-principal-authentication-and-authorization.md)
- [0016 — Tokenizer-derived input bound with bounded, audited overage](0016-tokenizer-bound-and-audited-overage.md)
- [0017 — Reservation identity-claim guard for exactly-once terminal effects](0017-reservation-identity-claim-guard.md)
- [0018 — Reservation heartbeat and self-healing late commit](0018-reservation-heartbeat-and-self-healing-late-commit.md)
- [0019 — Fail-closed enforcement with an opt-in grace allowance](0019-fail-closed-enforcement-with-grace.md)
- [0020 — Empty applicable-budget set is a denial](0020-empty-applicable-budget-set-denies.md)
- [0021 — Provider-qualified price book](0021-provider-qualified-price-book.md)
- [0022 — The SDK guard enforces; the LiteLLM callback only meters](0022-sdk-enforces-litellm-meters.md)
- [0023 — Async Alembic on asyncpg, no separate sync migration driver](0023-async-alembic-asyncpg-no-psycopg.md)
- [0024 — Baseline migration builds the schema from the canonical MetaData](0024-metadata-create-all-baseline-migration.md)
- [0025 — At most one budget per scope node in V1](0025-one-budget-per-scope-node.md)
- [0026 — Keyed deterministic token hash for credential lookup](0026-keyed-deterministic-token-hash.md)
- [0027 — Reserve period_start is the current UTC calendar month](0027-reserve-period-start-calendar-month.md)
- [0028 — Current price-book version is the latest published](0028-current-price-book-version-latest-published.md)

## Adding a record

Copy the structure of an existing record, give it the next number, and set its
status. Records are immutable once accepted: to change a decision, add a new
record that supersedes the old one and update the older record's status.
