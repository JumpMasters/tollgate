# 35. SDK guard enforcement contract: reserve-before-dispatch, fail-closed, pluggable tokenizer

Date: 2026-07-19

## Status

Accepted

## Context

ADR 0022 drew the line between enforcement and metering: only a hook that runs *before* dispatch
and can deny is honestly an enforcement path, so the SDK guard carries that responsibility while
the LiteLLM callback stays metering-only. That decision fixed the shape (reserve, then commit or
cancel) but left the guard's own contract open — how it fails when the control plane is
unreachable, how it keeps a long-running call from being reaped out from under it, and how it
turns a prompt into the tokenizer-derived bound ADR 0016 requires. This record pins those
decisions for the client-side guard that wraps a model call.

The guard sits on the synchronous path of every call it wraps: whatever it does between "decide"
and "dispatch" becomes latency and availability surface for the caller. It talks to the command
routes fixed by ADR 0031 and composes with the reservation heartbeat and self-healing commit of
ADR 0018, the fail-closed default of ADR 0019, and the grace-backfill contract of ADR 0030.

## Decision

- **Reserve-before-dispatch, resolve-on-exit.** `guard()` is an async context manager: it reserves
  the worst-case budget before yielding control to the caller's block, so a denial
  (`BudgetDenied`, `NotAuthorized`) or an unreachable control plane (`EnforcementUnavailable`)
  raises *before* the wrapped call ever dispatches — deny means never dispatch, not dispatch then
  refund. On exit the reservation is always resolved exactly once: a clean exit with usage
  recorded commits it; a clean exit with no usage recorded, or the block raising, cancels it. The
  resolution runs unconditionally on the way out, so a reservation is never left dangling by a
  normal return or an exception from the wrapped call.
- **Fail-closed by default (ADR 0019).** A reserve that cannot get a decision — connection failure,
  timeout, an unmapped 5xx — raises the typed `EnforcementUnavailable` rather than admitting the
  call. Connect and read timeouts are configured tight and separately from other calls, so an
  unreachable or slow control plane fails fast instead of adding latency to every guarded call.
  The guard ships with no default degrade-and-continue behavior; a caller that wants to keep
  dispatching through a control-plane outage must opt in explicitly and accept the exposure that
  entails.
- **Pluggable tokenizer, safe over-reserve direction (ADR 0016).** The default tokenizer is a
  dependency-free heuristic (a fixed characters-per-token upper bound) so the guard has no hard
  native dependency and every unit test exercises the real default path. A `tiktoken`-backed
  tokenizer is available for callers who install it, trading a coarser bound for a tighter one;
  either implementation feeds the same input-bound calculation, which adds a fixed provider margin
  on top of the raw count. Swapping tokenizers only changes how tight the reservation is, never
  which direction it can be wrong in — both implementations reserve at or above the true count.
- **Automatic background heartbeat (ADR 0018).** For the duration of the wrapped block, the guard
  runs a background task that periodically extends the reservation's TTL, so a live long-running
  or streamed call is never mistaken for abandoned by the TTL reaper. A missed heartbeat is logged
  and swallowed rather than raised: the reaper is the backstop for a truly abandoned reservation,
  so a transient heartbeat failure must not tear down an otherwise healthy call.
- **A decoupled exception taxonomy.** The guard defines its own exception hierarchy mapped from
  the wire error envelope (ADR 0031) by HTTP status and error code, rather than importing the
  control plane's internal domain errors. A caller catches a small, stable set of SDK-owned types
  without depending on server internals; an unrecognized error code fails closed to the same
  `EnforcementUnavailable`/`InvalidRequest` split the wire contract defines.
- **Runtime dependency footprint stays deliberate.** The HTTP transport is a genuine runtime
  dependency (the guard cannot function without it) and is declared as such. The tokenizer that
  trades accuracy for an extra package is declared as an optional extra, not a default dependency,
  so installing the SDK does not pull in a tokenizer library a caller may not want, and the tested
  default path never depends on it being present.
- **Opt-in grace is deferred, not designed away.** ADR 0019 already allows a capped, per-instance
  grace allowance during an outage, and ADR 0030 already fixes how a grace window backfills to the
  ledger once connectivity returns. The guard does not yet implement the client side of that
  allowance — tracking spend locally during an `EnforcementUnavailable` window and replaying it on
  reconnect — and defaults to strict fail-closed until it does. Adding it later is additive: it
  slots behind the same `guard()` seam and reuses the already-accepted backfill contract, rather
  than requiring a new enforcement path.

## Consequences

- A guarded call has a bounded worst case by construction: a denial or an outage is caught before
  dispatch, a live stream cannot be prematurely reaped out from under an in-flight reservation, and
  every reservation the guard opens is resolved on every exit path.
- The default install stays dependency-light; a caller who wants a tighter reservation opts into
  the tokenizer extra without changing any other behavior.
- Until client-side grace ships, a control-plane outage stops every guarded call for every caller
  that has not built its own fallback — an explicit, documented trade favoring correctness over
  availability, consistent with the fail-closed default.
- The SDK's exception-to-wire-code mapping must be kept in step with ADR 0031 as new error codes
  are added there; an unmapped code degrades safely (closed) rather than silently, but a stale
  mapping loses the more specific typed exception a caller may be catching for.
