# 0016 — Tokenizer-derived input bound with bounded, audited overage

- Status: Accepted
- Date: 2026-06-22

## Context

The worst-case reserve needs the input cost up front, but the exact tokenization
of a prompt is not known until the provider tokenizes it. A different tokenizer,
BPE or version drift, multibyte text, or provider- and gateway-injected content
can make the realized input exceed the estimate. Output is different:
`max_output_tokens` is a true provider ceiling, so it cannot drift.

## Decision

Reserve the input as a tokenizer-derived upper bound — the client-side token
count plus a configurable `provider_margin` — at the full, non-cached price,
which is the safe over-reserve direction. Reconcile on `commit`: move at most the
reserved amount into `committed`, and record any excess as **audited overage** —
a ledger row, a hard alert, and a charge against the node's `remaining`. Because
`commit` moves at most the reserved amount, `committed <= limit` holds at every
node unconditionally; overage is the only way real spend can exceed a limit, and
it is never silently absorbed.

State the guarantee honestly: there is **no universal mathematical bound** on a
single call's overage without an assumption about provider-side prompt expansion.
Under stable provider tokenization, per-call overage is expected to be small (and
is tightened by `provider_margin`); overage is self-limiting sequentially because
each recorded unit shrinks future headroom, but it is not bounded a priori across
many simultaneously in-flight calls.

## Consequences

- `committed <= limit` at every node, always and by construction.
- Every unit of real spend beyond the reservation is recorded and attributed.
- The boundedness of overage is conditional on provider behaviour, and we say so
  rather than over-claiming a guarantee we cannot keep.
