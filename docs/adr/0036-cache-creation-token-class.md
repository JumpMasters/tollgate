# 0036 — Cache-creation token class in the cost model

- Status: Accepted
- Date: 2026-07-19

## Context

Providers that support prompt caching bill three distinct input-side quantities, not
one: standard (uncached) input, tokens **read** from the cache (discounted), and tokens
**written** to the cache (a premium — for Anthropic, above the standard input rate).
The cost model priced only the first two: `ModelPrice` carried an input rate and a
cached-input rate constrained to be no greater than it, and `actual_micro` treated the
cached count as a subset of the input count. A cache-**creation** token had nowhere to
live and, worse, could not be represented even in principle — a rate above the input
rate was rejected. Any cached call that created cache entries was therefore undercharged.

## Decision

Add a fourth token class, **cache creation**, to the cost model:

- `ModelPrice` gains `cache_creation_micro_per_token`, a required rate guarded only for
  non-negativity. It carries **no** upper bound relative to the input rate: cache
  creation is legitimately a premium.
- `actual_micro` gains a `cache_creation_tokens` count that is **disjoint and additive**
  — it is not drawn from `input_tokens` (unlike the cached-read subset) and is priced on
  top at the creation rate.
- `estimate_micro` is unchanged: cache creation is a **reconcile-time drift class**, not
  a reserved quantity. A reserve cannot know a call's cache behaviour in advance, so
  creation cost surfaces on `commit`/backfill as committed spend (or audited overage when
  it pushes past the reservation), never as a pre-charge.
- The price book stores the rate in a new `price.cache_creation_micro_per_token` column;
  the usage count travels on `ProviderUsage`, the HTTP `usage` body, and the SDK, and is
  folded into the commit and backfill idempotency fingerprints.

The documented provider mapping (Anthropic Messages API, whose `input_tokens`,
`cache_read_input_tokens`, and `cache_creation_input_tokens` are disjoint): set
`input_tokens = input_tokens + cache_read_input_tokens`,
`cached_input_tokens = cache_read_input_tokens`, and
`cache_creation_tokens = cache_creation_input_tokens`.

## Consequences

- Cached calls that create cache entries are priced correctly instead of undercharged.
- The usage count defaults to zero, so every existing caller and stored row is unchanged;
  only a caller that reports cache-creation tokens is affected.
- This is the usage-fidelity floor for the metering command and for ingesting spend from
  clients that meter all four token classes.
- The ledger still records only total input/output token counts for provenance; recording
  the per-class token breakdown on the ledger remains a separate, deferred audit concern.
