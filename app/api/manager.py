"""Transport-manager endpoints. A manager only ever sees requests of their own company."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from app.auth.deps import require_manager
from app.auth.jwt import AuthContext
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
