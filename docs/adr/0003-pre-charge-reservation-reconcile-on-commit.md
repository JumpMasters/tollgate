# 0003 — Pre-charge reservation with reconcile-on-commit

- Status: Accepted
- Date: 2026-06-22

## Context

A budget that is only checked after spend has happened cannot stop the request
that breaches it. Token usage compounds the problem: the exact cost of a call is
not known until it completes, and streaming calls complete slowly.

## Decision

Enforce spend as a reservation taken before dispatch and reconciled afterward —
"overbooking math for tokens." `reserve` holds a worst-case estimate against
every applicable budget; the call dispatches only if the reserve succeeds;
`commit` reconciles to the provider-reported actual usage, releasing the
over-reserved difference; `cancel` releases in full when the call never ran.

## Consequences

- A budget can deny a request before any spend occurs.
- Worst-case reservation can transiently reserve more than a call uses, which is
  corrected on commit — the safe direction (a too-large reserve under-admits; it
  never overspends).
- It requires estimating the worst case up front: an input bound plus a true
  output ceiling (see ADR 0016).
