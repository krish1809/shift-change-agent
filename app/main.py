"""FastAPI application factory. `build_app()` is also what tests use to simulate a restart."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from app.agent.graph import build_graph
from app.api import chat, dev, health, manager, me
from app.api.errors import install_error_handlers
from app.db.pool import apply_schema, open_pool
from app.db.seed import reseed, resolve_base_date, seed_if_empty
from app.domain.clock import Clock, OverrideClock, parse_aware
from app.llm.base import ChatLLM
from app.llm.openai_compat import build_llm
from app.llm.rule_based import RuleBasedLLM
from app.logging import configure_logging, correlation_id_var, new_correlation_id
from app.settings import Settings, get_settings
from app.tools.faults import FaultInjector

log = logging.getLogger(__name__)


def _default_clock(settings: Settings) -> OverrideClock:
    clock = OverrideClock()
    if settings.clock_override:
        clock.set(parse_aware(settings.clock_override))
    return clock


def build_app(settings: Settings | None = None, *, clock: Clock | None = None, llm: ChatLLM | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    clock = clock or _default_clock(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        pool = await open_pool(settings.database_url)
        try:
            await apply_schema(pool)
            saver = AsyncPostgresSaver(pool)
            await saver.setup()
            await _seed(pool, settings, clock)
            app.state.pool = pool
            app.state.graph = build_graph(saver)
            log.info("startup complete", extra={"fields": {"env": settings.env}})
            yield
        finally:
            app.state.pool = None
            await pool.close()
            log.info("shutdown complete")

    app = FastAPI(title="Shift-Change Request Agent", version="1.0.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.clock = clock
    app.state.pool = None
    app.state.graph = None
    app.state.faults = FaultInjector(settings.faults)
    app.state.llm = llm or build_llm(settings, RuleBasedLLM())

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

    install_error_handlers(app)
    for router in (health.router, dev.router, me.router, manager.router, chat.router):
        app.include_router(router)
    return app


async def _seed(pool, settings: Settings, clock: Clock) -> None:  # type: ignore[no-untyped-def]
    base_date = resolve_base_date(settings.seed_base_date, clock)
    if settings.seed_mode == "always":
        async with pool.connection() as conn:
            await reseed(conn, base_date)
        log.info("seeded (always)", extra={"fields": {"base_date": str(base_date)}})
    elif settings.seed_mode == "if_empty" and await seed_if_empty(pool, base_date):
        log.info("seeded empty database", extra={"fields": {"base_date": str(base_date)}})


def create_app() -> FastAPI:
    """Entry point for uvicorn (`uvicorn app.main:create_app --factory`)."""
    return build_app()
