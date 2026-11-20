"""FastAPI application factory. `build_app()` is also what tests use to simulate a restart."""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from app.api import health
from app.logging import configure_logging, correlation_id_var, new_correlation_id
from app.settings import Settings, get_settings

log = logging.getLogger(__name__)


def build_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        log.info("startup", extra={"fields": {"env": settings.env}})
        yield
        log.info("shutdown")

    app = FastAPI(title="Shift-Change Request Agent", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings

    @app.middleware("http")
    async def correlation_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        cid = request.headers.get("x-correlation-id") or new_correlation_id()
        token = correlation_id_var.set(cid)
        try:
            response = await call_next(request)
        finally:
            correlation_id_var.reset(token)
        response.headers["x-correlation-id"] = cid
        return response

    app.include_router(health.router)
    return app


def create_app() -> FastAPI:
    """Entry point for uvicorn (`uvicorn app.main:create_app --factory`)."""
    return build_app()


