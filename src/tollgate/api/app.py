"""The FastAPI application surface.

This module builds the HTTP app and its routes. It drives the application layer
and never imports a concrete adapter; the composition root injects dependencies.
"""

from __future__ import annotations

from fastapi import FastAPI

from tollgate import __version__


def create_api() -> FastAPI:
    """Build the FastAPI app with its routes."""
    app = FastAPI(title="Tollgate", version=__version__)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
