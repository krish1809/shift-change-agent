"""Runs one employee turn: lock the thread, build the runtime context, invoke the graph, shape the reply."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI
from langgraph.types import Command

from app.agent import templates
from app.agent.approval_flow import finalize_request
from app.agent.context import RunContext
from app.agent.thread_lock import ThreadBusy, thread_lock
from app.auth.jwt import AuthContext
from app.db.repositories import companies, employees, metrics
from app.db.repositories import requests as requests_repo
from app.domain.models import ShiftRequest
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


async def is_paused(app: FastAPI, config: dict[str, Any]) -> bool:
    """True when the thread is waiting at the manager-approval interrupt (state lives in Postgres)."""
    snapshot = await app.state.graph.aget_state(config)
    return "await_manager" in (snapshot.next or ())


async def _invoke(app: FastAPI, ctx: RunContext, config: dict[str, Any], text: str) -> dict[str, Any]:
    if await is_paused(app, config):
        # The conversation is waiting on a manager: this message resumes it in "pending" mode.
        command = Command(resume={"kind": "employee_message", "text": text})
        return await app.state.graph.ainvoke(command, config, context=ctx)
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


DECISION_KIND = {"approved": "manager_decision", "rejected": "manager_decision", "expired": "expired"}


async def resume_request(app: FastAPI, request: ShiftRequest) -> bool:
    """Deliver a recorded decision to the paused conversation that raised `request`.

    Identity is rebuilt from the stored request row (created from the employee's JWT), never from the
    manager's token. Returns True if this call delivered it, False if it was already delivered or busy.
    """
    settings = app.state.settings
    auth = AuthContext(request.company_id, request.employee_id, "employee")
    if not request.thread_id.startswith(auth.thread_prefix):
        raise ValueError("request thread does not belong to its employee")  # corrupted row: refuse
    config = {"configurable": {"thread_id": request.thread_id}}
    try:
        async with thread_lock(app.state.pool, request.thread_id, settings.thread_lock_wait_ms):
            async with app.state.pool.connection() as conn:
                current = await requests_repo.get_request(conn, request.company_id, request.id)
            if current is None or current.status != "PENDING_APPROVAL" or current.decision is None:
                return False  # someone else already finished it
            ctx = await build_run_context(app, auth, request.thread_id)
            if await is_paused(app, config):
                command = Command(resume={"kind": DECISION_KIND[current.decision], "request_id": current.id})
                await app.state.graph.ainvoke(command, config, context=ctx)
                return True
            # The pause was never persisted (crash window): finish the request directly and post the
            # message into the thread so the employee still sees it.
            result = await finalize_request(ctx, current.id)
            if result is None:
                return False
            reply = templates.render(result.template, result.params)
            await app.state.graph.aupdate_state(
                config,
                {"messages": [{"role": "assistant", "content": reply, "at": ctx.clock.now().isoformat()}],
                 "last_outcome": result.status, **result.updates},
            )  # fmt: skip
            return True
    except ThreadBusy:
        return False  # the sweeper will try again


async def redrive_unpaused(app: FastAPI, request: ShiftRequest) -> bool:
    """A PENDING request whose conversation never reached the pause: put the thread into the paused state."""
    auth = AuthContext(request.company_id, request.employee_id, "employee")
    config = {"configurable": {"thread_id": request.thread_id}}
    try:
        async with thread_lock(app.state.pool, request.thread_id, app.state.settings.thread_lock_wait_ms):
            if await is_paused(app, config):
                return False
            ctx = await build_run_context(app, auth, request.thread_id)
            await app.state.graph.aupdate_state(config, {"pending_request_id": request.id}, as_node="respond_outcome")
            await app.state.graph.ainvoke(None, config, context=ctx)
            return True
    except ThreadBusy:
        return False


__all__ = [
    "ThreadBusy",
    "TurnOutcome",
    "build_run_context",
    "redrive_unpaused",
    "resume_request",
    "run_employee_turn",
]
