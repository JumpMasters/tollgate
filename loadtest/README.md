# Load harness

A standalone asyncio tool that drives a high-concurrency reserve workload at a **hot shared parent
budget** and checks the result with the offline conservation oracle (`loadtest/oracle.py`). It
exists to *prove* — not merely assert — that the invariant-guarded conditional write holds the
spend invariant under real concurrent traffic.

## The comparison

The same workload runs against three admission-control strategies that differ only in the reserve
guard:

- **naive** — read the remaining headroom, then add unconditionally. The check-then-act gap lets
  concurrent workers over-admit the shared parent.
- **occ** — read the balance, then value-compare-and-swap; retry on a lost race. Correct, but the
  retries thrash the hot row.
- **guarded** — a single atomic `UPDATE … WHERE remaining >= amount`; the `WHERE` is the guard.
  Correct *and* it never retries.

To make an over-admission *visible and countable*, the shootout runs against a dedicated
`harness_balance` table without the storage `CHECK` the product's `budget_balance` carries. In
production that `CHECK` is a second-line backstop — it turns a would-be over-admission into a hard
error; the application-tier guard is what turns it into a clean *denial* instead. A separate
product-path check drives the real handlers + reaper on the real schema and confirms conservation
and exactly-once under contention.

## Running it

```bash
# against any migrated Postgres (set the asyncpg URL):
TOLLGATE_DATABASE_URL='postgresql+asyncpg://user:pass@host:5432/db' \
  uv run python -m loadtest.harness --concurrency 8 --concurrency 32 --concurrency 64 --ops 8

# a scaled-down deterministic run also gates every PR:
uv run pytest tests/integration/test_load_harness.py
```

## Representative numbers

One run on an Apple M3 (macOS, Postgres 17 in Docker). Throughput and latency are hardware- and
contention-dependent; `overspend` is the micro-USD admitted past a node's limit (0 = the invariant
held).

```
strategy  concurrency  throughput/s  p99_ms  overspend  retries  violations
naive     8            2034          15.76   700        0        storage_guard
occ       8            2048          12.11   0          16       -
guarded   8            3639          7.94    0          0        -
naive     32           3162          35.74   750        0        storage_guard
occ       32           3024          37.41   0          50       -
guarded   32           4033          27.18   0          0        -
naive     64           4222          43.04   1000       0        storage_guard
occ       64           2920          57.94   0          82       -
guarded   64           5125          36.29   0          0        -
```

Reading it: **naive** admits real overspend that climbs with concurrency; **occ** stays at zero
overspend but its retry count (and p99) climbs as the hot row thrashes; **guarded** stays correct
with no retries. All three are bounded by a single-hot-row ceiling — the same contention limit that
motivates the deferred per-node fast-path (a future `CounterStore` backed by Redis); the guarded
write wins this comparison but does not remove that ceiling.
