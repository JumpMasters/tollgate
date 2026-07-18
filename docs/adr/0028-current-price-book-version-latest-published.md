# 0028 — Current price-book version is the latest published

- Status: Accepted
- Date: 2026-06-23

## Context

Prices live in a versioned, immutable price book (§3, ADR 0010, ADR 0021): a
`price_book` version is append-only and a price correction ships as a **new**
version, never an edit. A reserve must resolve the price for a `(provider, model)`
and **stamp the version** on the reservation so the matching commit reconciles
against the same basis (§4, §8). The schema has no explicit "active version"
pointer, so "which version is current" needs a rule.

## Decision

The **current** version is the `price_book` row with the **latest `published_at`**.
`PriceBookRepository.resolve_price(provider, model)` joins `price` to `price_book`,
filters the pair, orders by `published_at` descending, and takes the first row —
returning that version and its rates, or `None` when the pair is unpriced (the
reserve then denies with `UnknownModel`).

## Consequences

- Publishing a new `price_book` version makes it current atomically the moment its
  `published_at` is the greatest — no separate "activate" step, no mutable pointer.
- Immutability (ADR 0010/0021) means a historical commit re-derives its cost exactly
  from the version it stamped, independent of later publications.
- `published_at` ties are unspecified; in practice publish times are distinct. If
  ties ever matter, a monotonic version sequence can break them — a future, additive
  refinement.
- The lookup is **per pair**: a `(provider, model)` omitted from a newer version still
  resolves to the latest version that priced it, rather than becoming unknown.
  Retiring a model therefore requires an explicit signal (out of scope here), not
  mere omission from the newest book.
