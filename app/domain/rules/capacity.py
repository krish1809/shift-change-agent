from __future__ import annotations

from app.domain.models import Violation
from app.domain.rules.base import RuleContext


def check_seat_available(ctx: RuleContext) -> Violation | None:
    """Uses the policy-capped effective capacity, not the vehicle's seat count.

    A manager cannot create seats, so this is never escalatable. The atomic UPDATE at booking time
    re-checks it, because a seat can disappear between this check and the write.
    """
    target = ctx.target_trip
    if ctx.change_type != "reschedule" or target is None:
        return None
    if target.seats_taken < target.effective_capacity:
        return None
    return ctx.violation(
        "NO_SEAT", escalatable=False, time=ctx.label(target.scheduled_at), capacity=target.effective_capacity
    )
