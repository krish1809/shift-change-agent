"""Runs one employee turn: lock the thread, build the runtime context, invoke the graph, shape the reply."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI

from app.agent import templates
from app.agent.context import RunContext
from app.agent.thread_lock import ThreadBusy, thread_lock
from app.auth.jwt import AuthContext
from app.db.repositories import companies, employees, metrics
from app.llm.base import BudgetedLLM, TurnBudget
from app.logging import get_correlation_id

log = logging.getLogger(__name__)


@dataclass
class TurnOutcome:
    reply: str
    status: str
    request: dict[str, Any] | None = None
    options: list[dict[str, Any]] | None = None
    llm_calls: int = 0
    tool_calls: int = 0
    latency_ms: int = 0
    last_outcome: str | None = None
    tools_called: list[str] | None = None
    http_status: int = 200


async def build_run_context(app: FastAPI, auth: AuthContext, thread_id: str) -> RunContext:
    state = app.state
    async with state.pool.connection() as conn:
        company_ctx = await companies.get_company_context(conn, auth.company_id)
        employee = await employees.get_employee(conn, auth.company_id, auth.employee_id)
    if company_ctx is None or employee is None:
        raise LookupError("identity no longer exists")
    budget = TurnBudget(state.settings.max_llm_calls_per_turn)
    return RunContext(
        auth=auth, thread_id=thread_id, clock=state.clock, pool=state.pool,
        llm=BudgetedLLM(state.llm, budget), settings=state.settings, faults=state.faults,
        company=company_ctx.company, policy=company_ctx.policy, employee=employee,
        correlation_id=get_correlation_id(),
    )  # fmt: skip


async def run_employee_turn(app: FastAPI, auth: AuthContext, session_id: str, text: str) -> TurnOutcome:
    """Raises ThreadBusy if another turn on this thread doesn't finish within THREAD_LOCK_WAIT_MS."""
    settings = app.state.settings
    thread_id = auth.thread_id(session_id)
    started = time.perf_counter()
    loop = asyncio.get_running_loop()

    async with thread_lock(app.state.pool, thread_id, settings.thread_lock_wait_ms):
        ctx = await build_run_context(app, auth, thread_id)
        ctx.deadline = loop.time() + settings.turn_deadline_ms / 1000
        config = {"configurable": {"thread_id": thread_id}}
        try:
            final = await asyncio.wait_for(_invoke(app, ctx, config, text), timeout=settings.turn_deadline_ms / 1000)
            outcome = _outcome_from_state(final, ctx)
        except TimeoutError:
            log.warning("turn deadline exceeded")
            outcome = TurnOutcome(reply=templates.render("turn_timeout"), status="error", last_outcome="TIMEOUT")
        except Exception:
            log.exception("turn failed")
            outcome = TurnOutcome(
                reply=templates.render("internal_error", {"correlation_id": get_correlation_id()}),
                status="error", last_outcome="INTERNAL_ERROR", http_status=500,
            )  # fmt: skip
        outcome.llm_calls = ctx.llm_calls
        outcome.tool_calls = len(ctx.tool_calls)
        outcome.latency_ms = int((time.perf_counter() - started) * 1000)
        await _record_metrics(app, ctx, outcome)
        return outcome


async def _invoke(app: FastAPI, ctx: RunContext, config: dict[str, Any], text: str) -> dict[str, Any]:
    return await app.state.graph.ainvoke({"user_text": text}, config, context=ctx)


def _outcome_from_state(final: dict[str, Any], ctx: RunContext) -> TurnOutcome:
    return TurnOutcome(
        reply=final.get("reply") or templates.render("generic_help"),
        status=final.get("status") or "ok",
        request=final.get("request"),
        options=final.get("options"),
        last_outcome=final.get("last_outcome"),
        tools_called=list(final.get("tools_called") or []),
    )


async def _record_metrics(app: FastAPI, ctx: RunContext, outcome: TurnOutcome) -> None:
    try:
        async with app.state.pool.connection() as conn:
            await metrics.record_turn(
                conn, ctx.auth.company_id, at=ctx.clock.now(), thread_id=ctx.thread_id,
                correlation_id=ctx.correlation_id, latency_ms=outcome.latency_ms, llm_calls=outcome.llm_calls,
                tool_calls=outcome.tool_calls, outcome=outcome.last_outcome,
            )  # fmt: skip
    except Exception:
        log.exception("could not record turn metrics")


__all__ = ["ThreadBusy", "TurnOutcome", "build_run_context", "run_employee_turn"]
