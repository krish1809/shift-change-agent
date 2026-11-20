"""Status and withdrawal of the caller's own requests."""

from __future__ import annotations

from app.db.repositories import audit, trips
from app.db.repositories import requests as requests_repo
from app.domain.formatting import fmt_time
from app.domain.models import ShiftRequest
from app.tools.context import ToolContext
from app.tools.handlers.status_words import STATUS_WORDS
from app.tools.results import ToolResult
from app.tools.schemas import GetRequestStatusArgs, WithdrawRequestArgs


async def _summary(ctx: ToolContext, conn, request: ShiftRequest) -> str:
    from_trip = await trips.get_trip(conn, request.company_id, request.from_trip_id) if request.from_trip_id else None
    to_trip = await trips.get_trip(conn, request.company_id, request.to_trip_id) if request.to_trip_id else None
    kind = from_trip.trip_type if from_trip else "trip"
    if request.change_type == "cancel":
        return f"cancel {kind} {fmt_time(from_trip.scheduled_at, ctx.tz) if from_trip else ''}".strip()
    old = fmt_time(from_trip.scheduled_at, ctx.tz) if from_trip else "?"
    new = fmt_time(to_trip.scheduled_at, ctx.tz) if to_trip else "?"
    return f"{kind} {old} → {new}"


async def _find(ctx: ToolContext, conn, request_id: str | None) -> ShiftRequest | None:
    # Always scoped to the caller: someone else's request id simply isn't found.
    # In a paused chat, "status" / "withdraw it" mean THIS chat's request, not the latest one anywhere.
    request_id = request_id or ctx.pending_request_id
    if request_id:
        return await requests_repo.get_request(
            conn, ctx.auth.company_id, request_id.upper(), employee_id=ctx.auth.employee_id
        )
    mine = await requests_repo.list_requests(conn, ctx.auth.company_id, employee_id=ctx.auth.employee_id, limit=1)
    open_ = await requests_repo.latest_open_request(conn, ctx.auth.company_id, ctx.auth.employee_id)
    return open_ or (mine[0] if mine else None)


async def get_request_status(ctx: ToolContext, args: GetRequestStatusArgs) -> ToolResult:
    async with ctx.pool.connection() as conn:
        request = await _find(ctx, conn, args.request_id)
        if request is None:
            return ToolResult(status="NOT_FOUND", template="not_found", params={"what": "that request"})
        summary = await _summary(ctx, conn, request)
        from_trip = (
            await trips.get_trip(conn, request.company_id, request.from_trip_id) if request.from_trip_id else None
        )
    info = {"id": request.id, "status": request.status}
    if request.status == "PENDING_APPROVAL":
        params = {
            "request_id": request.id,
            "summary": summary,
            "since": fmt_time(request.created_at, ctx.tz),
            "desk_phone": ctx.company.desk_phone,
            "trip_type": from_trip.trip_type if from_trip else "trip",
            "day": "",
            "current_time": fmt_time(from_trip.scheduled_at, ctx.tz) if from_trip else "",
        }
        return ToolResult(status="STATUS", template="status_pending", params=params, request=info)
    params = {
        "request_id": request.id,
        "summary": summary,
        "status_words": STATUS_WORDS.get(request.status, request.status.lower()),
    }
    return ToolResult(status="STATUS", template="status_final", params=params, request=info)


async def withdraw_request(ctx: ToolContext, args: WithdrawRequestArgs) -> ToolResult:
    async with ctx.pool.connection() as conn:
        request = await _find(ctx, conn, args.request_id)
        if request is None or request.status != "PENDING_APPROVAL":
            return ToolResult(
                status="NOT_FOUND",
                template="not_found",
                params={
                    "what": "a request waiting for approval",
                    "hint": "Only requests that are still pending can be withdrawn.",
                },
            )
        now = ctx.now()
        moved = await (
            await conn.execute(
                """UPDATE shift_requests
                      SET status = 'WITHDRAWN', decision = 'withdrawn', decided_by = %s, decided_at = %s,
                          updated_at = %s
                    WHERE company_id = %s AND employee_id = %s AND id = %s
                      AND status = 'PENDING_APPROVAL' AND decision IS NULL
                RETURNING id""",
                (ctx.auth.employee_id, now, now, ctx.auth.company_id, ctx.auth.employee_id, request.id),
            )
        ).fetchone()
        if moved is None:  # a manager decided in the meantime: report the truth, not the intent
            return await get_request_status(ctx, GetRequestStatusArgs(request_id=request.id))
        await audit.add_audit(
            conn,
            ctx.auth.company_id,
            at=now,
            actor_type="employee",
            actor_id=ctx.auth.employee_id,
            action="WITHDRAWN",
            request_id=request.id,
            thread_id=ctx.thread_id,
        )
        from_trip = (
            await trips.get_trip(conn, request.company_id, request.from_trip_id) if request.from_trip_id else None
        )
    params = {
        "request_id": request.id,
        "trip_type": from_trip.trip_type if from_trip else "trip",
        "day": "",
        "current_time": fmt_time(from_trip.scheduled_at, ctx.tz) if from_trip else "",
    }
    return ToolResult(
        status="WITHDRAWN",
        template="withdrawn",
        params=params,
        request={"id": request.id, "status": "WITHDRAWN"},
        updates={"pending_request_id": None},
    )
