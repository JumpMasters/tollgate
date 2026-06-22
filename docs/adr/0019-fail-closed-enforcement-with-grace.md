# 0019 — Fail-closed enforcement with an opt-in grace allowance

- Status: Accepted
- Date: 2026-06-22

## Context

The gate sits on the synchronous path of every call, so its behaviour when
Postgres is unreachable is a first-class decision: fail open (admit the call and
abandon the guarantee) or fail closed (deny, and accept an availability
dependency on the datastore).

## Decision

Default to fail-closed. If the reserve transaction cannot complete — connection
failure, statement timeout, failover, pool exhaustion — the SDK raises a typed
`EnforcementUnavailable` and the call is not dispatched; tight, separately
configurable connect and statement timeouts make a slow datastore fail fast. A
budget may opt into a capped **grace allowance**: a small amount spendable while
enforcement is unavailable, tracked per SDK instance and backfilled to the ledger
(`grace_backfill`) when connectivity returns.

Be explicit about what grace costs. Because the datastore is by definition
unreachable during a grace window, instances cannot coordinate, so the allowance
is enforced per instance: the worst-case untracked exposure for a budget is
`N_live_instances × per-budget grace`, not a single grace amount. `committed <=
limit` holds only while the datastore is reachable or grace is zero; with grace
enabled the posture degrades to "hard up to the limit, plus a
per-instance-bounded, fully-backfilled-and-audited grace overrun during outages."

## Consequences

- By default, a datastore outage produces denials and zero untracked spend.
- Grace trades strict enforcement for availability, explicitly and per budget,
  with the exposure quantified so operators can size it.
- Every grace micro-USD is reconciled and alerted on reconnect.
