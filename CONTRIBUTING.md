# Contributing

Thanks for your interest in Tollgate. This document describes the development
workflow and the checks every change must pass.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) and Python 3.13.
- A local Postgres is needed only for the integration and load tests; the unit
  suite and all static checks run without one.

Install the project and its dev dependencies:

```sh
make sync   # uv sync --locked --dev
```

## Workflow

1. Create a branch from `main`.
2. Make your change, with tests.
3. Run `make verify` and make sure it passes.
4. Open a pull request. CI must be green before a change can merge; `main` is
   protected and does not accept direct pushes or force-pushes.

```sh
make verify   # ruff, mypy --strict, import-linter, pytest + coverage, pip-audit
```

## Standards

- **Tests.** New code comes with tests. The suite must hold at least 80%
  coverage; CI enforces this.
- **Formatting and linting.** Code must be clean under `ruff format` and
  `ruff check`. Run `make fmt` to format and autofix.
- **Types.** The tree must pass `mypy` in strict mode.
- **Architecture.** Shared types and the port interfaces belong in
  `tollgate.domain` (a pure leaf); `application` depends on `domain` and declares
  the `CounterStore` and repository ports; `adapters` implement them; `api` and
  `workers` drive `application`; and only `app.py` wires concrete implementations
  together. The import-linter contracts in `pyproject.toml` enforce these edges.
- **Commits.** Write clear, imperative commit messages that explain the change
  and its motivation.

## Architecture decisions

Significant or hard-to-reverse decisions are recorded as Architecture Decision
Records under [`docs/adr`](docs/adr). If your change makes such a decision, add
an ADR for it.

## Reporting security issues

Please follow [SECURITY.md](SECURITY.md) for vulnerabilities rather than opening
a public issue.
