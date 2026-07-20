# 0038 — Cache only successful idempotent responses

- Status: Accepted
- Date: 2026-07-20
- Supersedes: [ADR 0007](0007-idempotency-caching-policy.md)

## Context

ADR 0007 set the idempotency caching policy: claim a per-principal key with a unique-index
`INSERT` in the same transaction as the effect, replay the stored response on a duplicate, and
roll back an insufficient-budget denial so it is never stale-cached. But it also said to cache
*hard validation rejections* (for example, an unknown `(provider, model)`), and reserved a `status`
column on the idempotency row to distinguish a cached success from a cached rejection.

That half was never built. Every command handler caches a response only on success — each denial,
validation error included, raises and rolls the whole transaction back — so `claim` decides
FRESH/REPLAY/MISMATCH from `command_fingerprint` and `response` alone and never reads `status`. The
column has one value forever (`'succeeded'`) and is dead weight, and the ADR, the schema, and the
code disagree.

The as-built behaviour is also the *safer* policy. Caching a hard rejection would pin a stale
answer past the point it stops being true: an unknown-model rejection cached under a key would keep
replaying even after a later price-book version adds that model — exactly the stale-cache hazard
ADR 0007 forbids for budget denials. Re-evaluating a rejected command on its next distinct-key
attempt is correct; caching it is not.

## Decision

- **Cache only successes.** A command persists its idempotency record only when it succeeds; every
  denial (insufficient budget, unknown model, unauthorized/unknown scope, key reuse) raises and
  rolls its transaction back, so no rejection is ever cached and each is re-evaluated on retry.
- **Drop the `status` column.** With only one cached outcome there is nothing to distinguish, so
  the write-only `status` column is removed from both idempotency tables (`idempotency_key` and the
  durable `metered_receipt`, ADR 0037), and `store_response` no longer takes a status argument
  (migration `0007`).

Everything else in ADR 0007 stands: exactly-once via the claimed key, no stale-caching of budget
denials, key-reuse rejection on a differing fingerprint, and composition with the reservation
identity-claim (ADR 0017).

## Consequences

- The idempotency row records exactly what it means — a completed command's response — with no
  vestigial column implying an unbuilt rejection-caching path.
- A retried command that previously failed validation is always re-evaluated, so it can succeed
  once the underlying cause (an unpriced model, say) is fixed, rather than replaying a stale error.
- If durable hard-rejection caching is ever wanted, it is a deliberate future decision that must
  also solve the stale-cache invalidation this ADR avoids by not caching rejections at all.
