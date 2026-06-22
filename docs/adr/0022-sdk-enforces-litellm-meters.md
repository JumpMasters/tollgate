# 0022 — The SDK guard enforces; the LiteLLM callback only meters

- Status: Accepted
- Date: 2026-06-22

## Context

True pre-charge enforcement must run before dispatch and be able to deny. A
LiteLLM `CustomLogger` callback's hooks fire around or after the provider call,
so by design they cannot abort dispatch. Shipping the callback as if it enforced
would be dishonest about what it can do.

## Decision

Ship enforcement in the Tollgate SDK guard: it reserves before dispatch
(deny → never dispatch), then commits or cancels in a `finally` block, with
`extend` heartbeats for long streams. Ship the LiteLLM callback as metering and
chargeback only — it observes completed calls and records spend with
provider-reported usage, and is explicitly not an enforcement path. The only
LiteLLM mechanism that can deny pre-dispatch is a proxy-side pre-call hook, which
is deferred with proxy mode.

## Consequences

- The integration story is honest: one path enforces, the other only meters.
- LiteLLM users get accurate chargeback immediately, with a clear note that
  pre-dispatch denial needs the SDK guard (or, later, proxy mode).
- Proxy/sidecar enforcement is on the roadmap, not in V1 (see ADR 0012).
