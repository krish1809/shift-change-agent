from __future__ import annotations

from datetime import datetime, timedelta

from app.domain.models import Violation
from app.domain.rules.base import RuleContext


def cutoff_deadline(ctx: RuleContext) -> datetime | None:
    """The last moment a change may be made without approval.

    Reschedule: measured from the EARLIER of the current and target trips, so moving a drop earlier
    can't sneak past the cut-off of the new, earlier cab. Cancel: measured from the current trip.
    """
    minutes = ctx.policy.cutoff_minutes(ctx.entry.trip_type)
    times = [t.scheduled_at for t in (ctx.current_trip, ctx.target_trip) if t is not None]
    if ctx.change_type == "cancel":
        times = [ctx.current_trip.scheduled_at] if ctx.current_trip else []
    if not times:
        return None
    return min(times) - timedelta(minutes=minutes)


def check_cutoff(ctx: RuleContext) -> Violation | None:
    deadline = cutoff_deadline(ctx)
    if deadline is None or ctx.now <= deadline:  # boundary inclusive: exactly at the deadline is fine
        return None
    minutes = ctx.policy.cutoff_minutes(ctx.entry.trip_type)
    return ctx.violation(
        "CUTOFF_PASSED",
        escalatable=ctx.policy.cutoff_escalatable,
        cutoff_minutes=minutes,
        cutoff_label=ctx.minutes_label(minutes),
        deadline=ctx.label(deadline),
        trip_type=ctx.entry.trip_type,
    )
