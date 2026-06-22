# 0002 — Relational, transactional core over event-sourcing

- Status: Accepted
- Date: 2026-06-22

## Context

Pre-charge enforcement requires that, at the moment a request is admitted, the
system can prove committed spend will not exceed a limit. That is a synchronous,
point-in-time guarantee over mutable balances under concurrency. An
event-sourced design would model spend as an append-only event stream with
balances derived by projection, pushing the invariant into aggregate
serialization and accepting projection lag — the opposite of what a hard,
pre-charge gate needs.

## Decision

Build on a relational, transactional core (Postgres). Budget balances are
mutable rows guarded by constraints and conditional writes; a separate
append-only ledger records every change for audit. The spend invariant is a
database guarantee enforced where the data lives, not an application-level
reconstruction.

## Consequences

- The invariant is enforced under real concurrency by constraints the database
  checks on every write.
- Audit and conservation remain available through the append-only ledger,
  summed offline rather than on the command path (see ADR 0011).
- We forgo the temporal replay event-sourcing offers; the ledger provides the
  audit trail we actually need without the projection machinery.
