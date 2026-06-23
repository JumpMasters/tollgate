"""Smoke test: every module in the package imports cleanly."""

from __future__ import annotations

import importlib

import pytest

MODULES = [
    "tollgate",
    "tollgate.app",
    "tollgate.domain",
    "tollgate.domain.money",
    "tollgate.domain.pricing",
    "tollgate.domain.ids",
    "tollgate.domain.errors",
    "tollgate.domain.records",
    "tollgate.domain.reservations",
    "tollgate.domain.scopes",
    "tollgate.domain.commands",
    "tollgate.domain.invariants",
    "tollgate.application",
    "tollgate.application.ports",
    "tollgate.application.handlers",
    "tollgate.adapters",
    "tollgate.adapters.postgres",
    "tollgate.adapters.postgres.engine",
    "tollgate.adapters.postgres.identifiers",
    "tollgate.adapters.postgres.schema",
    "tollgate.adapters.postgres.idempotency_repo",
    "tollgate.adapters.integrations",
    "tollgate.api",
    "tollgate.api.app",
    "tollgate.workers",
    "tollgate.config",
    "tollgate.config.settings",
]


@pytest.mark.parametrize("name", MODULES)
def test_module_imports(name: str) -> None:
    assert importlib.import_module(name) is not None
