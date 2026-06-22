# 0010 — Cost normalization to micro-USD via an immutable, versioned price book

- Status: Accepted
- Date: 2026-06-22

## Context

Provider prices are small per-token amounts that must be summed exactly and
reproduced for audit long after a call ran. Binary floating point cannot
represent them exactly, and prices change over time, so a balance computed today
must remain explainable from the prices in force when the call happened.

## Decision

Normalize all cost to integer micro-USD (millionths of a US dollar), computed
with `Decimal`. Prices live in a versioned price book; every `reserve` stamps the
`price_book_version` it used and `commit` reconciles against that same version.
Published price-book versions are immutable — append-only, never updated or
deleted. A price correction ships as a new version, never an in-place edit.

## Consequences

- Balance arithmetic is exact, with no floating-point drift.
- Any historical commit can be re-derived from the exact version it stamped, so
  the audit trail is stable.
- Corrections are additive, which keeps each version a faithful record of what
  was charged at the time (see also ADR 0021 for provider qualification).
