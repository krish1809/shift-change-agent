"""What happens when a pending request is decided (or expires): re-validate, apply, tell the employee.

One code path serves the graph's `apply_decision` node AND the sweeper's direct re-drive. Every step is
idempotent: the final status change is a conditional UPDATE, so running this twice changes nothing twice.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime

from app.agent.context import RunContext
from app.db.repositories import audit, roster, trips
from app.db.repositories import requests as requests_repo
from app.domain import booking
from app.domain.booking import BookingStatus, ChangeSpec
from app.domain.formatting import fmt_day, fmt_time
from app.domain.models import ShiftRequest
from app.domain.rules.base import RuleContext
from app.domain.rules.engine import evaluate
from app.logging import get_correlation_id
from app.tools.common import option_dicts
from app.tools.context import ToolContext
from app.tools.handlers.shift import _alternatives, _base_context, entry_draft
from app.tools.results import ToolResult

log = logging.getLogger(__name__)

UNAVAILABLE_REASONS = {
    "NO_SEAT": "that cab is full now",
    "TRIP_IN_PAST": "that cab has already left",
    "ROUTE_NOT_SERVED": "that cab no longer serves your area",
    "TRIP_NOT_FOUND": "that cab is no longer available",
    "TRIP_TYPE_MISMATCH": "that cab is no longer available",
}
CLEAR = {"pending_request_id": None, "pending_offer": None, "draft": None}


def _tool_context(ctx: RunContext) -> ToolContext:
    return ToolContext(
        auth=ctx.auth, pool=ctx.pool, clock=ctx.clock, company=ctx.company, policy=ctx.policy,
        employee=ctx.employee, thread_id=ctx.thread_id, faults=ctx.faults,
    )  # fmt: skip


async def finalize_request(ctx: RunContext, request_id: str) -> ToolResult | None:
    """Apply the recorded decision for `request_id`. None = nothing to do (already finalised / not found)."""
    now = ctx.clock.now()
    async with ctx.pool.connection() as conn:
        request = await requests_repo.get_request(
            conn, ctx.auth.company_id, request_id, employee_id=ctx.auth.employee_id
        )
        if request is None or request.status != "PENDING_APPROVAL" or request.decision is None:
            return None
        entry = await roster.get_entry(conn, ctx.auth.company_id, ctx.auth.employee_id, request.roster_entry_id)
        if entry is None:
            return None
        base_params = _base_params(ctx, request, entry)
        await audit.add_audit(
            conn, ctx.auth.company_id, at=now, actor_type="system", actor_id="resume", action="RESUMED",
            request_id=request.id, thread_id=ctx.thread_id, correlation_id=get_correlation_id(),
            payload={"decision": request.decision},
        )  # fmt: skip

        if request.decision == "approved":
            return await _apply_approved(ctx, conn, request, entry, base_params, now)

        status, action, template = {
            "rejected": ("APPROVAL_REJECTED", "CHANGE_REJECTED", "approval_rejected"),
            "expired": ("EXPIRED", "EXPIRED", "expired"),
        }[request.decision]
        if not await requests_repo.finalize_status(conn, ctx.auth.company_id, request.id, status=status, at=now):
            return None
        await audit.add_audit(
            conn, ctx.auth.company_id, at=now, actor_type="system", actor_id="resume", action=action,
            request_id=request.id, thread_id=ctx.thread_id, correlation_id=get_correlation_id(),
            payload={"note": request.decision_note},
        )  # fmt: skip
        params = {**base_params, "note": request.decision_note}
        return ToolResult(
            status=status, template=template, params=params, request={"id": request.id, "status": status},
            updates=CLEAR,
        )  # fmt: skip


def _base_params(ctx: RunContext, request: ShiftRequest, entry) -> dict:  # type: ignore[no-untyped-def]
    today = ctx.clock.now().astimezone(ctx.company.tz).date()
    params = {"request_id": request.id, "trip_type": entry.trip_type, "day": fmt_day(entry.service_date, today)}
    if entry.trip:
        params["current_time"] = fmt_time(entry.trip.scheduled_at, ctx.company.tz)
    return params


async def _apply_approved(ctx, conn, request, entry, params, now: datetime) -> ToolResult | None:  # type: ignore[no-untyped-def]
    company_id = ctx.auth.company_id
    target = await trips.get_trip(conn, company_id, request.to_trip_id) if request.to_trip_id else None
    reason = await _unavailable_reason(ctx, request, entry, target, now)

    if reason is None:
        spec = ChangeSpec(
            company_id=company_id, employee_id=ctx.auth.employee_id, roster_entry_id=entry.id,
            change_type=request.change_type, to_trip_id=request.to_trip_id, expected_version=entry.version,
            thread_id=request.thread_id,
        )  # fmt: skip
        result = await booking.apply_change(
            conn,
            spec,
            now=now,
            actor_type="manager",
            actor_id=request.decided_by or "manager",
            approved_request_id=request.id,
        )
        if result.status == BookingStatus.APPLIED:
            if request.change_type == "cancel":
                template = "approved_applied_cancel"
                applied = {**params, "old_time": params.get("current_time")}
            else:
                template = "approved_applied"
                applied = {
                    **params, "new_time": fmt_time(target.scheduled_at, ctx.company.tz),  # type: ignore[union-attr]
                    "cab": target.cab_label if target else None,
                }  # fmt: skip
            return ToolResult(
                status="APPROVED_APPLIED", template=template, params=applied,
                request={"id": request.id, "status": "APPROVED_APPLIED"}, updates=CLEAR,
            )  # fmt: skip
        if result.status == BookingStatus.DUPLICATE:
            return None  # another worker finalised it first: exactly-once
        reason = "that cab filled up" if result.status == BookingStatus.SEAT_TAKEN else "your booking changed"

    # Approved, but the world moved on: say so honestly, change nothing, offer real options.
    if not await requests_repo.finalize_status(conn, company_id, request.id, status="APPROVED_BUT_UNAVAILABLE", at=now):
        return None
    await audit.add_audit(
        conn, company_id, at=now, actor_type="system", actor_id="resume", action="APPROVED_BUT_UNAVAILABLE",
        request_id=request.id, thread_id=ctx.thread_id, correlation_id=get_correlation_id(), payload={"reason": reason},
    )  # fmt: skip
    tool_ctx = _tool_context(ctx)
    fresh = await roster.get_entry(conn, company_id, ctx.auth.employee_id, entry.id)
    out_params = {**params, "reason": reason}
    updates = dict(CLEAR)
    if request.change_type == "reschedule" and fresh is not None and fresh.trip is not None:
        base = _base_context(tool_ctx, fresh, "reschedule")
        alts = await _alternatives(tool_ctx, conn, fresh, base, desired_at=target.scheduled_at if target else None)
        options = option_dicts(tool_ctx, alts)
        out_params["options"] = options
        updates.update({"offered_options": options, "draft": entry_draft(fresh)})
    return ToolResult(
        status="APPROVED_BUT_UNAVAILABLE", template="approved_but_unavailable", params=out_params,
        options=out_params.get("options"), request={"id": request.id, "status": "APPROVED_BUT_UNAVAILABLE"},
        updates=updates,
    )  # fmt: skip


async def _unavailable_reason(ctx, request, entry, target, now: datetime) -> str | None:  # type: ignore[no-untyped-def]
    """Re-check what the manager's approval does NOT cover: seats, trip in the past, roster changes."""
    if entry.status != "scheduled" and request.change_type == "reschedule":
        return "your booking changed"
    if entry.trip_id != request.from_trip_id:
        return "your booking changed in the meantime"
    if request.change_type == "cancel":
        return None
    if target is None:
        return "that cab is no longer available"
    tool_ctx = _tool_context(ctx)
    base: RuleContext = replace(_base_context(tool_ctx, entry, "reschedule"), now=now, target_trip=target)
    verdict = evaluate(base)
    if verdict.already_on_trip:
        return "your booking changed in the meantime"
    for violation in verdict.violations:
        if not violation.escalatable:  # a manager can override rules, but cannot create seats or undo departures
            return UNAVAILABLE_REASONS.get(violation.code, "that change is no longer possible")
    return None
