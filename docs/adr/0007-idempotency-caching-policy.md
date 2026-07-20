# 0007 — Idempotency caching policy

- Status: Superseded by [ADR 0038](0038-cache-only-successful-idempotent-responses.md) (2026-07-20)
- Date: 2026-06-22

> **Superseded.** This record's "cache successes *and* hard validation rejections" decision was
> never implemented — the code caches only successes, which is the safer policy. ADR 0038 records
> the as-built decision and drops the vestigial `status` column (#96). The rest of this record
> (exactly-once via the claimed key, no stale-caching of budget denials) still holds.

## Context

Clients retry, and a retried command must apply its effect exactly once. But
caching the response of every outcome would be wrong: caching an
insufficient-budget denial would pin a stale "over budget" answer even after the
budget frees or the period rolls.

## Decision

Use a Stripe-style idempotency key claimed by a unique-index `INSERT` in the same
transaction as the effect; a concurrent duplicate blocks on the index, then
replays the stored response. Cache successes and hard validation rejections (for
example, an unknown `(provider, model)` under strict policy). Roll the whole
transaction back on an insufficient-budget denial, so the key is not persisted
and an identical retry can succeed later. A key reused with a different command
fingerprint is rejected as key reuse.

## Consequences

- Retried commands apply their effect exactly once.
- Budget denials are never stale-cached; they are always re-evaluated.
- The policy composes with the reservation identity-claim (ADR 0017):
  idempotency dedups the external retry, the identity-claim dedups the terminal
  effect on a specific reservation.
