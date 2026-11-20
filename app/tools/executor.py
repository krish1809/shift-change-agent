"""Runs a validated tool call with a timeout, a retry policy and truthful failure reporting.

Reads: one retry. Writes: one retry (safe because writes are idempotent); and after a timeout or
failure we ask the database what really happened (reconcile) instead of guessing.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from pydantic import BaseModel

from app.db.repositories import audit
from app.db.repositories import requests as requests_repo
from app.logging import get_correlation_id
from app.tools.common import day_label, time_label, upcoming_entries
from app.tools.context import ToolContext
from app.tools.handlers import HANDLERS
from app.tools.registry import REGISTRY
from app.tools.results import ToolResult

log = logging.getLogger(__name__)
RETRY_BACKOFF_S = 0.1


async def execute_tool(ctx: ToolContext, name: str, args: BaseModel) -> ToolResult:
    spec = REGISTRY[name]
    ctx.scratch.clear()
    for attempt in (1, 2):
        try:
            return await asyncio.wait_for(_run(ctx, name, args), ctx.tool_timeout_s)
        except TimeoutError:
            reason = "timeout"
        except Exception as exc:  # a failing tool must never reach the user as an exception
            reason = type(exc).__name__
        log.warning("tool attempt failed", extra={"fields": {"tool": name, "attempt": attempt, "reason": reason}})
        if spec.mutating:
            reconciled = await _reconcile(ctx)
            if reconciled is not None:
                return reconciled
        if attempt == 1:
            await asyncio.sleep(RETRY_BACKOFF_S)

    await _audit_failure(ctx, name)
    return ToolResult(
        status="TOOL_UNAVAILABLE",
        template="tool_unavailable",
        params={"booking_line": await _booking_line(ctx)},
    )


async def _run(ctx: ToolContext, name: str, args: BaseModel) -> ToolResult:
    await ctx.faults.before(name)
    result = await HANDLERS[name](ctx, args)
    await ctx.faults.after(name)
    return result


async def _reconcile(ctx: ToolContext) -> ToolResult | None:
    """A write may have committed even though we didn't hear back. The database knows."""
    info: dict[str, Any] | None = ctx.scratch.get("reconcile")
    if not info:
        return None
    try:
        async with ctx.pool.connection() as conn:
            request = await requests_repo.find_by_idempotency_key(
                conn, ctx.auth.company_id, ctx.auth.employee_id, info["key"]
            )
    except Exception:
        return None
    if request is None or request.status != info["expect"]:
        return None
    status = "PENDING_APPROVAL" if info["expect"] == "PENDING_APPROVAL" else "OUTCOME_RECONCILED"
    updates = {"draft": None, "offered_options": [], "pending_offer": None}
    if status == "PENDING_APPROVAL":
        updates["pending_request_id"] = request.id
    return ToolResult(
        status=status,
        template=info["template"],
        request={"id": request.id, "status": request.status},
        params={**info["params"], "request_id": request.id, "reconciled": True},
        updates=updates,
    )


async def _booking_line(ctx: ToolContext) -> str:
    """'your 7:00 PM drop today is still booked', read fresh and never allowed to fail the reply."""
    try:
        async with ctx.pool.connection() as conn:
            entries = await upcoming_entries(ctx, conn)
        if entries:
            e = entries[0]
            return f"your {time_label(ctx, e.trip)} {e.trip_type} {day_label(ctx, e)} is still booked"  # type: ignore[arg-type]
    except Exception:
        pass
    return "your bookings are as they were"


async def _audit_failure(ctx: ToolContext, name: str) -> None:
    try:
        async with ctx.pool.connection() as conn:
            await audit.add_audit(
                conn,
                ctx.auth.company_id,
                at=ctx.now(),
                actor_type="system",
                actor_id="executor",
                action="TOOL_FAILED",
                thread_id=ctx.thread_id,
                correlation_id=get_correlation_id(),
                payload={"tool": name},
            )
    except Exception:
        log.exception("could not write TOOL_FAILED audit row")
