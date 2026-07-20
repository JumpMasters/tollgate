# 0037 — Metering command and LiteLLM callback

- Status: Accepted
- Date: 2026-07-19

## Context

ADR 0022 drew the enforcement/metering line: only a hook that runs before dispatch and can deny
is honestly an enforcement path, so the SDK guard (ADR 0035) carries enforcement and the LiteLLM
callback was reserved for metering. Two things were still open. First, the command routes fixed
by ADR 0031 had no metering ingress — a caller with provider-reported usage for a call that
already happened (a completed litellm call, a batch import, any post-hoc chargeback source) had
nothing to record it against. Second, the callback itself needed a concrete usage mapping, a
label-carrying contract, and a failure story, none of which ADR 0022 pinned.

Metering is structurally different from `reserve`: there is no pre-charge estimate to hold and
nothing left to deny — the tokens were already spent. Treating it as a reservation-shaped command
would force an artificial reserve/commit pair around spend that already happened, or would leave
metered calls outside the ledger and chargeback entirely.

## Decision

- **`POST /v1/meter` is a reservation-less command that never denies.** It runs the same
  server-side resolution as the grace backfill (ADR 0030) — the applicable budget set from
  credential ancestry plus an authorized project, the current price-book version (ADR 0028), the
  current UTC calendar-month period (ADR 0027) — then applies the actual cost against each node's
  live remaining. Committed takes what fits in `remaining = limit - reserved - committed -
  overage`; the excess is recorded as audited overage (the same split ADR 0029 already applies to
  self-healing late commits). An empty applicable-budget set is still rejected — metering to
  nothing is a request error, not spend with nowhere to land. `/v1/meter` is the general post-hoc
  metering ingress: any caller with provider-reported usage for an already-completed call can use
  it, not only the LiteLLM callback.
- **Distinct `meter` ledger provenance.** Metered spend is appended as its own `LedgerKind.METER`
  rows, not folded into `grace_backfill` or `commit_adjust`. A ledger row's `kind` is what makes
  the append-only ledger auditable by source (section 3): a reviewer can tell a normal
  reserve-then-commit call apart from a self-healing late commit, a connectivity-outage grace
  backfill, and a metered call, without inferring it from absence of a reservation id alone.
- **The ledger is self-describing: `model` and `labels` columns carry provenance on the row
  itself.** Every other ledger kind gets its model and labels by joining to the reservation it
  came from; a metered row has no reservation, so metering stamps `model` and `labels` directly on
  the ledger row. The chargeback rollup (ADR 0033) reads `COALESCE(reservation.model,
  ledger.model)` and `COALESCE(reservation.labels ->> key, ledger.labels ->> key)`, so metered
  spend rolls up on every existing axis — provider, model, and label — without a parallel query
  path. Reservation-backed rows are unaffected (their ledger `model`/`labels` stay null and the
  reservation join still wins); a grace backfill also sets no ledger `model`, so it keeps landing
  in the unattributed bucket exactly as before — only a `meter` row uses the ledger-side fallback.
- **Idempotency via the provider's response id.** `/v1/meter` uses the same `Idempotency-Key` +
  fingerprint-checked replay contract as every other command (ADR 0031, section 5.1). The natural
  key for a metered call is the provider's own response id — stable across a caller's retries of
  the *same* completed call, distinct across different calls — so the LiteLLM callback derives its
  idempotency key from the response id, falling back to litellm's per-call id and finally a random
  key only when neither is available.
- **The LiteLLM callback reads chargeback labels only from `metadata["tollgate_labels"]`.**
  litellm's call `metadata` is shared with litellm-internal bookkeeping (proxy/router keys like
  `user_api_key_alias` or `model_group`), so forwarding it wholesale into chargeback labels would
  leak those keys onto every metered row. The callback reads exactly one nested key,
  `metadata["tollgate_labels"]` (checked on both the top-level and `litellm_params["metadata"]`
  locations), coerces its keys and values to strings, and ignores everything else in `metadata`.
  A caller opts a value into chargeback labels explicitly; nothing is forwarded by default.
- **Post-call, metering-only — composes with ADR 0022.** The callback hooks
  `async_log_success_event` and `async_log_failure_event`, both of which fire after the provider
  call returns or fails; neither can prevent dispatch. It never claims to enforce. The SDK guard
  remains the only path that can deny (ADR 0035); the callback's job is exclusively to make sure
  every completed call — successful or not — lands in the ledger.
- **The failure path meters partial or last-seen usage flagged `truncated`.** A call that errors
  mid-stream or dies before completion may still have incurred billable tokens; the failure hook
  extracts whatever usage the provider reported (last-seen, not necessarily final) and meters it
  with `truncated=True`. A failure that carries no positive usage at all records nothing — an
  empty failure is not spend. `truncated` is stamped on the `ref` of every ledger row the call
  produces, so a reviewer can distinguish confirmed usage from a best-effort recording of a call
  that did not finish cleanly.
- **The litellm usage mapping targets Tollgate's disjoint token convention (ADR 0036).** litellm's
  `prompt_tokens` already folds in both cache-read and cache-creation tokens; Tollgate wants
  `input_tokens` to include the cache-read subset but exclude cache creation (a disjoint, additive
  class), so the callback computes `input_tokens = prompt_tokens - cache_creation`. `cached` reads
  the cache-read count (`cache_read`), and `cache_creation` is read separately and kept disjoint.
  Every field is read defensively with `.get(...)` because litellm's usage shape is
  version-sensitive; unknown or absent counts default to zero rather than raising.
- **Deployment note: use one path per call, never both.** A single call routed through both the
  SDK guard and the LiteLLM callback double-counts — the guard's commit and the callback's meter
  would both record spend for the same tokens. A deployment picks the SDK guard (enforcing,
  pre-dispatch) or the LiteLLM callback (metering-only, post-call), never both on the same call
  path.

## Consequences

- Any post-hoc usage source — the LiteLLM callback today, a future batch importer or another
  gateway's completion log tomorrow — has one wire-stable ingress (`POST /v1/meter`) that resolves
  budgets, price, and period the same way the grace backfill already does, rather than each source
  reimplementing that resolution.
- Chargeback rollups (ADR 0033) need no metering-specific query path: the `COALESCE` over
  reservation and ledger columns already generalizes to a third ledger kind with no reservation at
  all, so metered spend is visible on `GET /v1/spend` immediately.
- The metering path never denies, so it carries no availability risk for the calls it observes;
  the trade is that an over-budget metered call is recorded as audited overage rather than caught
  before the fact — by construction, since the call already happened before metering runs.
- Per-class token *provenance* on the ledger (which class each committed/overage micro-USD amount
  came from) remains deferred; a metered row records total input/output token counts the same way
  a grace backfill does, not a class-by-class breakdown.
- Deployments that need enforcement guarantees must route through the SDK guard; the LiteLLM
  callback is a chargeback-completeness tool, not a substitute for it. Operators mixing both
  integrations on the same call path will silently double-count until this note is followed.
