"""The FastAPI application surface.

This module builds the HTTP app and its routes. It drives the application layer
and never imports a concrete adapter; the composition root injects dependencies
(handlers and the authenticate callable) via ``app.state``.
"""

from __future__ import annotations

from fastapi import FastAPI
from starlette.types import Lifespan

from tollgate import __version__
from tollgate.api.errors import register_error_handlers
from tollgate.api.routes import commands


def create_api(lifespan: Lifespan[FastAPI] | None = None) -> FastAPI:
    """Build the FastAPI app with its routes."""
    app = FastAPI(title="Tollgate", version=__version__, lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    register_error_handlers(app)
    app.include_router(commands.router)
    return app
