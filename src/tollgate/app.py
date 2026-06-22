"""Composition root.

The only module that imports concrete adapters and wires them to the application.
Everything else depends on ports. The import-linter contracts in
``pyproject.toml`` keep that boundary honest.
"""

from __future__ import annotations

from fastapi import FastAPI

from tollgate.adapters.postgres.engine import build_engine
from tollgate.api.app import create_api
from tollgate.config.settings import Settings, load_settings


def build_app(settings: Settings | None = None) -> FastAPI:
    """Wire settings, the datastore engine, and the HTTP surface together."""
    settings = settings or load_settings()
    engine = build_engine(
        settings.database_url,
        statement_timeout_ms=settings.reserve_statement_timeout_ms,
    )
    app = create_api()
    app.state.engine = engine
    app.state.settings = settings
    return app
