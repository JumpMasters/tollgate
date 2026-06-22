# 0013 — Import-linter boundary enforcement

- Status: Accepted
- Date: 2026-06-22

## Context

The architecture depends on a layering: `domain` is a pure leaf, `application`
declares the ports, `adapters` implement them, and only the composition root
wires concretes. Boundaries like these erode silently as a codebase grows unless
something checks them.

## Decision

Enforce the import graph in CI with import-linter contracts declared in
`pyproject.toml`: `domain` imports nothing internal; `application` does not
import `adapters`, `api`, or `workers`; and `api` and `workers` do not import
`adapters`. A breach fails the build, and the contracts run as part of
`make verify`.

## Consequences

- The dependency rule is mechanically enforced, not merely documented.
- The CounterStore seam (ADR 0009) stays honest: the application physically
  cannot reach a concrete store.
- New layers or packages must state their allowed edges explicitly.
