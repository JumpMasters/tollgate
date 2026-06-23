# 0026 — Keyed deterministic token hash for credential lookup

- Status: Accepted
- Date: 2026-06-22

## Context

Authentication (ADR 0015, §5.0) hashes the presented bearer token and **looks up**
an active `api_credential` by that hash; the token itself is never stored. The
schema (§3) makes `api_credential.token_hash` `UNIQUE`, and the lookup is a single
equality match on it. That lookup shape requires the hash to be **deterministic**:
the same token must hash to the same value on every request, or it could never be
found.

§6 mentions `argon2`/`bcrypt` for credential tokens. Those are password hashes:
each embeds a fresh random salt, so the same input hashes differently every time
and the digest is **not** usable as a lookup key. They are also deliberately slow
(memory-hard) to resist brute-forcing **low-entropy** human passwords. Tollgate's
bearer tokens are not passwords — they are high-entropy random secrets minted by
the system — so the brute-force threat the slow KDF defends against does not apply,
while its non-determinism actively breaks the single-column lookup the schema and
§5.0 are built on.

## Decision

`token_hash = HMAC-SHA-256(token, key=secret)`, hex-encoded — a **keyed,
deterministic** hash.

- **Deterministic** so a presented token is found by one equality lookup on the
  `UNIQUE` `token_hash` column — no second "lookup id" column, no per-row salt.
- **Keyed** with a server-held secret (a pepper), injected from configuration at
  the composition root — never hardcoded and never in the `domain`/`application`
  import graph. A leak of the `token_hash` column alone cannot be reversed without
  the secret.
- The raw token is **never stored**; only its keyed hash is.
- `hash_token(token, *, secret)` is the single implementation, used both to
  authenticate a presented token and to mint a credential's stored hash, so the two
  can never diverge.

This refines §6's `argon2`/`bcrypt` wording for the token case; it **complements**
ADR 0015 (which fixed *that* tokens are stored only as a hash and looked up, but not
the hashing mechanism) and supersedes nothing.

## Consequences

- Authentication is one indexed equality lookup — fast, and outside the command
  transaction (§5.0).
- Rotating the server secret invalidates every existing token (they must be
  re-minted). This is an explicit, audited operation, acceptable for the V1
  per-deployment model.
- The "salt" of §3 is the shared server pepper, not a per-row salt; deterministic
  hashing is what a lookup-by-hash design requires.
- Should tokens ever become user-chosen / low-entropy, moving to per-credential
  `argon2` is a superseding record: it needs a split-token scheme (a public lookup
  id plus a secret verified against the slow hash) and a schema change, out of V1
  scope.
- High-entropy tokens make the keyed-hash choice safe: there is no low-entropy
  pre-image for an attacker to grind, with or without the secret.
