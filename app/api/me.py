"""Employee self-service reads. Identity comes from the token; there is no id in the path or body."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query, Request

from app.auth.deps import require_employee
from app.auth.jwt import AuthContext
from app.db.repositories import requests as requests_repo
from app.db.repositories import roster

router = APIRouter(prefix="/me", tags=["employee"])


@router.get("/roster")
async def my_roster(
    request: Request,
    on: date | None = Query(default=None, alias="date"),
    auth: AuthContext = Depends(require_employee),
) -> dict:
    async with request.app.state.pool.connection() as conn:
        entries = await roster.get_entries_for_employee(conn, auth.company_id, auth.employee_id, [on] if on else None)
    return {
        "entries": [
            {
                "id": e.id,
                "date": e.service_date.isoformat(),
                "trip_type": e.trip_type,
                "status": e.status,
                "trip_id": e.trip_id,
                "scheduled_at": e.trip.scheduled_at.isoformat() if e.trip else None,
                "cab": e.trip.cab_label if e.trip else None,
            }
            for e in entries
        ]
    }


@router.get("/requests")
async def my_requests(request: Request, auth: AuthContext = Depends(require_employee)) -> dict:
    async with request.app.state.pool.connection() as conn:
        rows = await requests_repo.list_requests(conn, auth.company_id, employee_id=auth.employee_id)
    return {"requests": [r.model_dump(mode="json", exclude={"idempotency_key", "thread_id"}) for r in rows]}
