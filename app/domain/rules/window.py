from __future__ import annotations

from app.domain.models import Violation
from app.domain.rules.base import RuleContext


def check_change_window(ctx: RuleContext) -> Violation | None:
    """How far a trip may move without approval. Boundary inclusive."""
    current, target = ctx.current_trip, ctx.target_trip
    if ctx.change_type != "reschedule" or current is None or target is None:
        return None
    shift = abs((target.scheduled_at - current.scheduled_at).total_seconds()) / 60
    limit = ctx.policy.change_window_minutes
    if shift <= limit:
        return None
    return ctx.violation(
        "OUTSIDE_CHANGE_WINDOW",
        escalatable=ctx.policy.window_escalatable,
        window_minutes=limit,
        window_label=ctx.minutes_label(limit),
        shift_minutes=int(shift),
    )
