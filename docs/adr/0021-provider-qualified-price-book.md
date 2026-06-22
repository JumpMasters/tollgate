# 0021 — Provider-qualified price book

- Status: Accepted
- Date: 2026-06-22

## Context

The same model name is served by more than one provider, often at different
prices. A reserve's estimate must be reconciled on `commit` against the identical
cost basis it used, or the two can disagree.

## Decision

Key prices by `(price_book_version, provider, model)`. `reserve` resolves the
cost from the provider + model + version triple and stamps it; `commit`
reconciles against the same triple. This extends the immutable, versioned price
book of ADR 0010 with provider as a first-class part of the key.

## Consequences

- A model served by two providers prices independently and correctly.
- `reserve` and `commit` cannot drift onto different prices for the same call.
- The price book treats provider as part of the identity of a price, not as a
  tag on the side.
