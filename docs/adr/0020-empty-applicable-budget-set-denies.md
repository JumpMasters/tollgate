# 0020 — Empty applicable-budget set is a denial

- Status: Accepted
- Date: 2026-06-22

## Context

Budget resolution can yield no node — no budget on the user, team, or org, and no
authorized project budget. An all-or-nothing guard evaluated over zero rows would
vacuously succeed, admitting a request that no budget governs.

## Decision

Default-deny the empty set. A request governed by no budget is not, by the
project's thesis, safely admissible, and a guard over zero rows must not pass by
default. A deployment may configure an explicit allow-list of ungoverned paths;
absent that, the request is denied.

## Consequences

- No request slips through merely because nothing governs it.
- Ungoverned paths become an explicit, audited configuration choice rather than
  an accident.
- A missing budget surfaces loudly as a denial instead of silently as a
  fail-open.
