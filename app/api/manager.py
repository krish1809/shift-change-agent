"""Transport-manager endpoints. A manager only ever sees requests of their own company."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.agent.runner import resume_request
from app.api.errors import AppError
from app.auth.deps import require_manager
from app.auth.jwt import AuthContext
from app.db.repositories import audit
from app.db.repositories import requests as requests_repo

router = APIRouter(prefix="/manager", tags=["manager"])


@router.get("/requests")
async def list_requests(
    request: Request,
    status: str | None = Query(default="PENDING_APPROVAL"),
    auth: AuthContext = Depends(require_manager),
) -> dict:
    async with request.app.state.pool.connection() as conn:
        rows = await requests_repo.list_requests(conn, auth.company_id, status=status or None)
    return {"requests": [r.model_dump(mode="json", exclude={"idempotency_key"}) for r in rows]}


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
        import logging

        logging.getLogger(__name__).exception("resume after decision failed; sweeper will retry")
    return JSONResponse(
        {
            "request": {"id": recorded.id, "decision": recorded.decision},
            "delivered": delivered,
            "reply": "Decision recorded." + ("" if delivered else " The employee will be notified shortly."),
        }
    )
