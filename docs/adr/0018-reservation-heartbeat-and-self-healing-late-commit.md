# 0018 — Reservation heartbeat and self-healing late commit

- Status: Accepted
- Date: 2026-06-22

## Context

A streaming call can run longer than the reservation TTL. If the reaper releases
a reservation whose call is in fact still alive, the spend is real and must not
be lost — yet the headroom that reservation held may already have been
reallocated to another request.

## Decision

For long-running calls the SDK sends a lightweight heartbeat (`extend`) that
advances `ttl_deadline` while tokens are still flowing; only a reservation past
its deadline **with no recent heartbeat** is reaped. If a `commit` nonetheless
arrives for a reaped reservation, it does not no-op: the identity-claim
(ADR 0017) matches zero rows, which routes to a reconciliation path that replays
the actual cost against **exactly the recorded `reservation_line` set** — each
line's own `(budget_id, period_start)` row, which remains the correct attribution
even if the period has since rolled, because the spend was incurred in the line's
period, not the current one. `reservation_line` is immutable once written and is
the sole source of truth for a reservation's balance fan-out, so a late commit
can never apply to a different or larger node set than the original reserve.

## Consequences

- A heartbeating live stream is never reaped.
- Real spend is always recorded; a premature reap only changes which headroom the
  spend lands against (possibly inducing audited overage), never whether it is
  recorded.
- One premature reap induces at most one call's overage; across many
  simultaneously reaped-yet-live streams those overages add up — there is no
  a-priori aggregate bound, only the per-call one — which is exactly why the
  heartbeat exists.
