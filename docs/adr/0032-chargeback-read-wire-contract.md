# 0032 — Chargeback read wire contract

- Status: Accepted
- Date: 2026-07-17

## Context

ADR 0031 pinned the command wire contract but deliberately left the read
surface open. The spend model gives reads a clean seam: every budget node's
`budget_balance` already aggregates everything at or below it (section 3), so a
listing of a scope's subtree *is* the org/team/user/project rollup, needing no
ledger summation. What still needs pinning is how a caller selects nodes, how
authorization runs in the inverse direction from commands (section 5.0), and the
response shape — before an SDK or dashboard depends on them.

## Decision

- **One read route:** `GET /v1/budgets` under the `/v1` prefix. It returns the
  current-period state of every budget node **at or below** the bearer
  credential's scope: `limit` / `reserved` / `committed` / `overage` /
  `remaining` (micro-USD), a reserved-inclusive `utilization_pct`, and the
  node's configured soft-alert thresholds each flagged `crossed`.
- **Optional `?scope=<kind>:<id>` filter** re-roots the returned subtree at a
  named node. The node must itself be at or below the credential, checked
  against **server-derived** ancestry (never request-asserted ids, per the
  `authorizes` caveat). An unknown node and a node outside the credential's
  scope are both refused with an identical `403` / `scope_not_authorized` — no
  existence leak (section 5.0), mirroring ADR 0031's unknown/foreign `403`. A
  malformed `scope` (bad kind, empty id) is a `422`.
- **Bearer credential required**, identical to the command routes: a missing,
  malformed, unknown, or revoked token is the same `401` with
  `WWW-Authenticate: Bearer` (section 5.0). Reads carry a credential like every
  command.
- **Utilization is reserved-inclusive:** `(reserved + committed + overage) /
  limit`, matching the `remaining` quantity the reserve guard gates on (section
  3). It may exceed 100 under audited overage and is reported, not clamped. A
  threshold `t` is `crossed` iff `spent * 100 >= t * limit` (exact integers).
- **Read-only, off the command path:** a `GET` with no `Idempotency-Key` and no
  transaction envelope; the handler opens a read-only connection and issues
  `SELECT`s only. It never seeds a `budget_balance` row — a node with no
  activity this period is reported as zero state against its `hard_limit_micro`.
- **Period:** the current UTC calendar month (ADR 0027). Historical periods and
  tag-grouped spend rollups (by model / provider / env / cost-center) are a
  separate follow-up with their own decision record; they require summing the
  append-only ledger and are out of scope here.

## Consequences

- The subtree listing doubles as the by-scope rollup, so no ledger read is
  introduced on this path; CLAUDE.md's "ledger summed only by the offline
  oracle" holds.
- Reusing `authorizes` (section 5.0) keeps one authorization definition for
  commands and reads; only the direction of application differs (one target vs.
  a subtree filter).
- As with ADR 0031, FastAPI request-validation `422`s use the default
  `{"detail": ...}` body, and the malformed-`scope` `422` follows suit rather
  than the `{"error": ...}` envelope; clients treat any non-2xx without an
  `error` key as a validation failure.
