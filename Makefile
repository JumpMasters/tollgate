# Tollgate developer tasks.
#
# `make verify` mirrors the checks that gate every pull request in CI. The
# coverage step runs the integration tests against a real Postgres via
# testcontainers, so `make verify` (and `make cover`/`make test-integration`)
# require Docker once integration tests exist. `make test` stays unit-only and
# needs no Docker.

.PHONY: all verify sync locked fmt lint typecheck imports test test-integration cover audit clean

all: verify

## verify: run the full set of CI gates locally
verify: locked lint typecheck imports cover audit

## sync: install the project and dev dependencies into the uv environment
sync:
	uv sync --locked --dev

## locked: fail if uv.lock is stale relative to pyproject.toml (no relock)
locked:
	uv lock --check

## fmt: format and autofix the tree in place
fmt:
	uv run ruff format .
	uv run ruff check --fix .

## lint: ruff format check + lint (no changes)
lint:
	uv run ruff format --check .
	uv run ruff check .

## typecheck: mypy in strict mode
typecheck:
	uv run mypy

## imports: enforce the architecture import boundaries
imports:
	uv run lint-imports

## test: run the unit test suite (no Docker)
test:
	uv run pytest -m "not integration"

## test-integration: run the integration suite against real Postgres (Docker)
test-integration:
	uv run pytest -m integration

## cover: run the full suite (unit + integration) and enforce the coverage threshold
cover:
	uv run pytest --cov=tollgate --cov-report=term-missing --cov-fail-under=80

## audit: scan dependencies for known vulnerabilities
audit:
	uv run pip-audit

## clean: remove caches and coverage artifacts
clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache .hypothesis htmlcov .coverage coverage.xml
