# 0023 — Async Alembic on asyncpg, no separate sync migration driver

- Status: Accepted
- Date: 2026-06-22

## Context

The application talks to Postgres through async SQLAlchemy and asyncpg; that is
the only Postgres driver in the dependency set. Alembic conventionally runs
migrations through a *synchronous* driver (psycopg), which would mean adding a
second Postgres driver solely for the migration path — another dependency to
install, audit, and keep version-aligned, on a path that otherwise needs none.

## Decision

Run Alembic through the application's async engine. `migrations/env.py` builds an
async engine from `Settings.database_url` and applies migrations with
`connection.run_sync(...)`; the online entrypoint wraps that in `asyncio.run`. No
psycopg or other sync driver is added. The integration harness invokes
`alembic upgrade head` from a synchronous, session-scoped pytest fixture, where no
event loop is active, so the env's `asyncio.run` is safe. testcontainers'
readiness probe runs `psql` *inside* the container, so it needs no Python driver
either.

## Consequences

- One Postgres driver (asyncpg) spans runtime, tests, and migrations — nothing
  extra to track or audit, and migrations exercise the same connection settings
  the application uses.
- Migrations must be invoked from a synchronous context (the Alembic CLI, or the
  sync session fixture). Calling them from inside an already-running event loop
  would conflict with the env's `asyncio.run`; this constraint is documented in
  `env.py`.
- The choice is reversible: should an offline or sync-only migration path later be
  required, a sync driver and an offline branch can be added without disturbing
  the schema or the runtime.
