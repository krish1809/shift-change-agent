from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
async def ready(request: Request) -> JSONResponse:
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        return JSONResponse({"status": "starting"}, status_code=503)
    try:
        async with pool.connection() as conn:
            await conn.execute("SELECT 1 FROM checkpoints LIMIT 1")
    except Exception:  # readiness is a yes/no question; details go to the log
        return JSONResponse({"status": "not_ready"}, status_code=503)
    return {"status": "ready"}
