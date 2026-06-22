# 0012 — Gateway-neutral control plane, not a proxy

- Status: Accepted
- Date: 2026-06-22

## Context

Many spend tools are gateways that proxy model traffic. Coupling enforcement to a
proxy ties adoption to replacing the data path and puts a new failure domain on
the hot path of every call.

## Decision

Tollgate is a control plane, not a gateway. It exposes `reserve` / `commit` /
`cancel` and is consulted beside whatever gateway or SDK a team already runs. It
does not route or proxy model traffic.

## Consequences

- Teams adopt enforcement without re-platforming how their model traffic flows.
- Tollgate is not in the data path of the provider response, so it is not a
  bottleneck or a single point of failure for the call itself.
- A proxy/sidecar enforcement mode — which would enable drop-in LiteLLM
  pre-dispatch denial — is a deliberate non-goal for V1 (see ADR 0022).
