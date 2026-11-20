"""Transport-manager endpoints. A manager only ever sees requests of their own company."""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from psycopg import AsyncConnection
from pydantic import BaseModel, ConfigDict, Field

from app.agent.runner import resume_request
from app.api.errors import AppError
from app.auth.deps import require_manager
from app.auth.jwt import AuthContext
from app.db.repositories import audit, companies, employees, trips
from app.db.repositories import requests as requests_repo
from app.domain.formatting import fmt_day, fmt_time
from app.domain.models import ShiftRequest

log = logging.getLogger(__name__)
router = APIRouter(prefix="/manager", tags=["manager"])

REASONS = {
    "CUTOFF_PASSED": "after the cut-off",
    "OUTSIDE_CHANGE_WINDOW": "moves the trip a lot",
    "DAILY_CHANGE_LIMIT": "over the daily change limit",
    "NO_SEAT": "cab full",
    "NO_TRIP_AT_TIME": "no cab at that time",
    "TRIP_IN_PAST": "cab already left",
}


async def _describe(conn: AsyncConnection, company_id: str, req: ShiftRequest, tz, today) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    """A request as a manager reads it: who, which trip, from -> to, and why it needed approval."""
    employee = await employees.get_employee(conn, company_id, req.employee_id)
    old = await trips.get_trip(conn, company_id, req.from_trip_id) if req.from_trip_id else None
    new = await trips.get_trip(conn, company_id, req.to_trip_id) if req.to_trip_id else None
    trip_type = (old or new).trip_type if (old or new) else "trip"
    day = fmt_day((old or new).service_date, today) if (old or new) else ""
    return {
        **req.model_dump(mode="json", exclude={"idempotency_key", "thread_id"}),
        "employee_name": employee.name if employee else req.employee_id,
        "summary": (
            f"{trip_type} {day}: "
            + (fmt_time(old.scheduled_at, tz) if old else "?")
            + " → "
            + ("cancel" if req.change_type == "cancel" else fmt_time(new.scheduled_at, tz) if new else "?")
        ),
        "reasons": [REASONS.get(v.get("code", ""), v.get("code", "")) for v in req.violations],
        "created_label": fmt_time(req.created_at, tz),
    }


@router.get("/requests")
async def list_requests(
    request: Request,
    status: str | None = Query(default="PENDING_APPROVAL"),
    auth: AuthContext = Depends(require_manager),
) -> dict:
    """Own company only. `status=` (empty) lists every status, newest first."""
    state = request.app.state
    async with state.pool.connection() as conn:
        company = await companies.get_company(conn, auth.company_id)
        rows = await requests_repo.list_requests(conn, auth.company_id, status=status or None)
        tz = company.tz  # type: ignore[union-attr]
        today = state.clock.now().astimezone(tz).date()
        return {"requests": [await _describe(conn, auth.company_id, r, tz, today) for r in rows]}


@router.get("/requests/{request_id}/audit")
async def request_audit(request_id: str, request: Request, auth: AuthContext = Depends(require_manager)) -> dict:
    """Who asked for what and who decided: the append-only trail of one request (own company only)."""
    async with request.app.state.pool.connection() as conn:
        found = await requests_repo.get_request(conn, auth.company_id, request_id.upper())
        if found is None:
            raise AppError(404, "I couldn't find that request.")
        rows = await audit.list_audit(conn, auth.company_id, request_id=found.id)
    return {
        "request_id": found.id,
        "events": [
            {"at": r["at"].isoformat(), "actor": f"{r['actor_type']}:{r['actor_id']}", "action": r["action"],
             "details": r["payload"]}
            for r in rows
        ],
    }  # fmt: skip


class DecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve", "reject"]
    note: str | None = Field(default=None, max_length=300)


@router.post("/requests/{request_id}/decision")
async def decide(
    request_id: str, body: DecisionBody, request: Request, auth: AuthContext = Depends(require_manager)
) -> JSONResponse:
    state = request.app.state
    now = state.clock.now()
    async with state.pool.connection() as conn:
        # 1) Record the decision FIRST (outbox). Scoped to the manager's own company: another company's
        #    request is simply not found (404, never 403), so its existence isn't leaked.
        recorded = await requests_repo.record_decision(
            conn, auth.company_id, request_id.upper(),
            decision="approved" if body.decision == "approve" else "rejected",
            decided_by=auth.employee_id, note=body.note, at=now,
        )  # fmt: skip
        if recorded is None:
            existing = await requests_repo.get_request(conn, auth.company_id, request_id.upper())
            if existing is None:
                raise AppError(404, "I couldn't find that request.")
            raise AppError(409, f"Request {existing.id} has already been decided or is no longer pending.")
        await audit.add_audit(
            conn, auth.company_id, at=now, actor_type="manager", actor_id=auth.employee_id,
            action="DECISION_RECORDED", request_id=recorded.id, thread_id=recorded.thread_id,
            payload={"decision": recorded.decision, "note": body.note},
        )  # fmt: skip
    # 2) Then resume the paused conversation. If this fails, the decision is safe in the DB and the
    #    sweeper delivers it.
    delivered = False
    try:
        delivered = await resume_request(request.app, recorded)
    except Exception:
        log.exception("resume after decision failed; sweeper will retry")
    return JSONResponse(
        {
            "request": {"id": recorded.id, "decision": recorded.decision},
            "delivered": delivered,
            "reply": "Decision recorded." + ("" if delivered else " The employee will be notified shortly."),
        }
    )
