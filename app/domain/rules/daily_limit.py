from __future__ import annotations

from app.domain.models import Violation
from app.domain.rules.base import RuleContext


def check_daily_limit(ctx: RuleContext) -> Violation | None:
    limit = ctx.policy.max_changes_per_day
    if limit is None or ctx.changes_today < limit:
        return None
    return ctx.violation(
        "DAILY_CHANGE_LIMIT",
        escalatable=ctx.policy.daily_limit_escalatable,
        max_changes=limit,
        changes_today=ctx.changes_today,
    )
