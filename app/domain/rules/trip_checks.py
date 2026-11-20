"""Hard checks on the target trip itself. None of these can be overridden by a manager."""

from __future__ import annotations

from app.domain.models import Violation
from app.domain.rules.base import RuleContext


def check_target_exists(ctx: RuleContext) -> Violation | None:
    if ctx.change_type != "reschedule" or ctx.target_trip is not None:
        return None
    if ctx.requested_label:
        # Never snap to a nearby trip: that would be a guess. The caller offers nearest options instead.
        return ctx.violation("NO_TRIP_AT_TIME", escalatable=False, requested=ctx.requested_label)
    return ctx.violation("TRIP_NOT_FOUND", escalatable=False)


def check_trip_type(ctx: RuleContext) -> Violation | None:
    target = ctx.target_trip
    if target is None:
        return None
    if target.trip_type != ctx.entry.trip_type or target.service_date != ctx.entry.service_date:
        return ctx.violation("TRIP_TYPE_MISMATCH", escalatable=False)
    return None


def check_route_serves_zone(ctx: RuleContext) -> Violation | None:
    target = ctx.target_trip
    if target is None or ctx.employee.zone in target.route_zones:
        return None
    return ctx.violation("ROUTE_NOT_SERVED", escalatable=False, zone=ctx.employee.zone)


def check_not_in_past(ctx: RuleContext) -> Violation | None:
    trip = ctx.target_trip if ctx.change_type == "reschedule" else ctx.current_trip
    if trip is None or trip.scheduled_at > ctx.now:
        return None
    return ctx.violation("TRIP_IN_PAST", escalatable=False, time=ctx.label(trip.scheduled_at))
