# 0017 — Reservation identity-claim guard for exactly-once terminal effects

- Status: Accepted
- Date: 2026-06-22

## Context

`commit`, `cancel`, and the reaper each move a reservation to a terminal state
and apply a balance change. But `reserved_micro` is a fungible aggregate shared
across all reservations on a node, so the balance guard alone cannot distinguish
a first commit from a duplicate — a second commit would double-apply the same
spend.

## Decision

Guard the terminal transition on the reservation's own identity:

```sql
UPDATE reservation
   SET status = :next            -- committed | released | reaped
 WHERE reservation_id = :r AND status = 'held';
```

Exactly one caller matches the row and wins the claim; a second
commit / cancel / reaper for the same reservation matches zero rows and routes to
idempotent replay — or to self-heal (ADR 0018) — instead of double-applying.

## Consequences

- Exactly one terminal effect per reservation, enforced by the write rather than
  asserted.
- It composes with idempotency (ADR 0007): idempotency dedups the external retry;
  the identity-claim dedups the terminal effect on a specific reservation.
- The balance guard still keeps `reserved` non-negative, but it is not what makes
  the effect exactly-once.
