# 0039 — Optional declared cache-creation bound in the reserve estimate

- Status: Accepted
- Date: 2026-07-23

## Context

The worst-case reserve estimate priced two quantities: the tokenizer-derived input
bound at the full input rate, and the provider output ceiling at the output rate. The
reconciled actual cost, however, prices a third billable quantity — tokens **written**
to the provider's prompt cache — at a dedicated cache-creation rate that is a premium and
may exceed the input rate (ADR 0036). The estimate carried no cache-creation term.

A call that writes a large prompt to the provider cache therefore reconciled to *more*
than it reserved. The invariant `committed <= limit` still held at every node — the
excess simply became audited overage (ADR 0016), split against each node's remaining
(ADR 0029) — but the "hard pre-charge" property did not cover cache writes: their cost
was admitted after the fact rather than gated before dispatch. Unlike output, a cache
write is a caller intention known before the call, so it can be reserved up front; unlike
the realized input drift, it is not an unavoidable tokenizer estimate.

## Decision

Add an **optional** caller-declared `cache_creation_bound_tokens` (default `0`) that a
caller supplies when it intends a cache write, alongside the existing input bound and
output ceiling. The reserve estimate gains the term
`cache_creation_micro_per_token * cache_creation_bound_tokens`, priced at the same
cache-creation rate the commit reconciles against.

- The field travels end to end: the HTTP reserve body, the domain `ReserveCommand`, the
  reserve idempotency fingerprint (so a retry declaring a different bound is key reuse,
  and the same bound matches), and the SDK client and guard. The caller declares the
  bound; the tokenizer does **not** estimate it — cache-write intent is the caller's, not
  a property of the prompt text.
- Omitting the field (every existing caller) leaves the estimate and all behaviour exactly
  as before: input bound plus output ceiling, cache write unreserved.
- The commit path is unchanged. It already prices realized cache-creation tokens as a
  disjoint additive term and reconciles the actual against the reservation; a larger
  reservation simply means the realized cache-write cost lands in committed spend instead
  of spilling into overage.

## Consequences

- Pre-charge now covers a **declared** cache write: a reserve that declares the bound holds
  the worst-case cache-write cost, and a commit that realizes those tokens reconciles with
  no overage.
- An **undeclared** cache write still over-runs into audited overage, exactly as before —
  the safe direction. Declaring the bound is opt-in fidelity, not a new requirement, so no
  caller is forced to change.
- No schema or migration change: the cache-creation rate already lives in the price book
  and the realized count already travels on usage. The wire gains one optional,
  backward-compatible field.
- The bound is a caller estimate, so it is subject to the same conditional-boundedness
  caveat as the input bound (ADR 0016): a cache write larger than the declared bound over-runs
  the reserved amount and the excess is overage.
