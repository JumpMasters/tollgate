# 0014 — UUIDv7 ledger identifiers

- Status: Accepted
- Date: 2026-06-22

## Context

The ledger is append-only and high-volume. Random (v4) UUID primary keys
fragment the index and hurt insert locality, because each new row lands at a
random position in the key space.

## Decision

Use UUIDv7 for ledger-entry and reservation identifiers. v7 ids are time-ordered
in their high bits, so append-only inserts stay index-friendly while the
identifier remains globally unique and needs no central coordination to generate.

## Consequences

- Better insert locality than random UUIDs, which matters for a hot append-only
  table.
- No sequence or coordinator is needed to mint ids.
- Ids encode their creation-time order, which is convenient for audit and
  pagination.
