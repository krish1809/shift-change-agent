"""Read-only tools: roster, cab options, company policy."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.db.repositories import roster
from app.domain.formatting import fmt_day, fmt_minutes
from app.domain.time_resolver import resolve_date, resolve_time
from app.tools.common import (
    booking_params,
    day_label,
    time_label,
)
from app.tools.context import ToolContext
from app.tools.handlers.shift import _alternatives, _base_context, _pick_entry, _with_options, entry_draft
from app.tools.results import ToolResult
from app.tools.schemas import CheckCabOptionsArgs, GetMyPolicyArgs, GetMyRosterArgs


async def get_my_roster(ctx: ToolContext, args: GetMyRosterArgs) -> ToolResult:
    today = ctx.today()
    day = None
    if args.date_text:
        resolved = resolve_date(args.date_text, today=today)
        day = resolved.value if resolved.kind == "exact" else None
    days = [day] if day else [today, today + timedelta(days=1)]
    async with ctx.pool.connection() as conn:
        entries = await roster.get_entries_for_employee(conn, ctx.auth.company_id, ctx.auth.employee_id, days)
    lines = []
    for e in entries:
        label = f"{e.trip_type} {fmt_day(e.service_date, today)}"
        lines.append(f"{label} at {time_label(ctx, e.trip)}" if e.trip else f"{label} is cancelled")  # type: ignore[arg-type]
    scope = fmt_day(day, today) if day else "today or tomorrow"
    return ToolResult(status="ROSTER", template="roster", params={"lines": lines, "scope": scope})


async def check_cab_options(ctx: ToolContext, args: CheckCabOptionsArgs) -> ToolResult:
    draft = dict(ctx.draft or {})
    trip_type = args.trip_type or draft.get("trip_type")
    day = None
    if args.date_text:
        resolved = resolve_date(args.date_text, today=ctx.today())
        day = resolved.value if resolved.kind == "exact" else None
    async with ctx.pool.connection() as conn:
        picked = await _pick_entry(ctx, conn, trip_type=trip_type, day=day, draft=draft)
        if not hasattr(picked, "id"):
            return picked  # type: ignore[return-value]
        entry = picked
        desired: datetime | None = None
        if args.time_text:
            resolved_time = resolve_time(
                args.time_text,
                trip_type=entry.trip_type,
                current_time=entry.trip.local_time(ctx.tz),  # type: ignore[union-attr]
                window_minutes=ctx.policy.change_window_minutes,
            )
            if resolved_time.kind == "exact" and resolved_time.value:
                desired = datetime.combine(entry.service_date, resolved_time.value, ctx.tz)
        base = _base_context(ctx, entry, "reschedule")
        alts = await _alternatives(ctx, conn, entry, base, desired_at=desired)
    params: dict[str, Any] = {"trip_type": entry.trip_type, "day": day_label(ctx, entry), **booking_params(ctx, entry)}
    result = ToolResult(status="OPTIONS", template="options", params=params, updates={"draft": entry_draft(entry)})
    return _with_options(result, ctx, alts)


async def get_my_policy(ctx: ToolContext, args: GetMyPolicyArgs) -> ToolResult:
    p = ctx.policy
    return ToolResult(
        status="POLICY",
        template="policy",
        params={
            "company_name": ctx.company.name,
            "desk_phone": ctx.company.desk_phone,
            "cutoff_drop": fmt_minutes(p.cutoff_minutes_drop),
            "cutoff_pickup": fmt_minutes(p.cutoff_minutes_pickup),
            "cutoff_escalatable": p.cutoff_escalatable,
            "window": fmt_minutes(p.change_window_minutes),
            "max_changes": p.max_changes_per_day,
        },
    )
