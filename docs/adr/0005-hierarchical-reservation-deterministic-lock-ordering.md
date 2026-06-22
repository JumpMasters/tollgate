# 0005 — Hierarchical reservation with deterministic lock ordering

- Status: Accepted
- Date: 2026-06-22

## Context

A reserve touches several balance rows at once — user, team, org, and project.
Sibling users share their parent rows (the team and org budgets), so two reserves
that acquire rows in different orders can form a lock cycle and deadlock.

## Decision

Resolve the applicable budget set as the ancestry path (skipping nodes without a
budget) plus the request's project budget. Acquire rows in one canonical order
everywhere: the idempotency-key insert, then (for terminal commands) the
reservation row, then the `budget_balance` rows ordered by
`(scope_kind rank: org < team < user < project, then scope_id, then
period_start)`. The reaper uses the same order. The reserve is all-or-nothing and
most-restrictive: if any node's guarded update returns zero rows, the whole
transaction rolls back and the denial names that node.

## Consequences

- Overlapping operations cannot form a lock cycle.
- A denial tells the caller which budget was binding (for example, "user `alice`
  exhausted; team `payments` had room").
- Every command and worker shares the single ordering, which is a small
  invariant to maintain but a large source of deadlock safety.
