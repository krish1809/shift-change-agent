"""Runs every rule in a fixed order and collects ALL violations, so one reply can explain everything."""

from __future__ import annotations

from collections.abc import Callable

from app.domain.models import Violation
from app.domain.rules.base import RuleContext, RuleResult
from app.domain.rules.capacity import check_seat_available
from app.domain.rules.cutoff import check_cutoff
from app.domain.rules.daily_limit import check_daily_limit
from app.domain.rules.trip_checks import (
    check_not_in_past,
    check_route_serves_zone,
    check_target_exists,
    check_trip_type,
)
from app.domain.rules.window import check_change_window

Rule = Callable[[RuleContext], Violation | None]

RULES: tuple[Rule, ...] = (
    check_target_exists,
    check_trip_type,
    check_route_serves_zone,
    check_not_in_past,
    check_seat_available,
    check_cutoff,
    check_change_window,
    check_daily_limit,
)


def evaluate(ctx: RuleContext) -> RuleResult:
    if (
        ctx.change_type == "reschedule"
        and ctx.target_trip is not None
        and ctx.current_trip is not None
        and ctx.target_trip.id == ctx.current_trip.id
    ):
        return RuleResult(already_on_trip=True)
    if ctx.change_type == "cancel" and ctx.entry.status == "cancelled":
        return RuleResult(already_on_trip=True)

    violations = [v for rule in RULES if (v := rule(ctx)) is not None]
    return RuleResult(violations=violations)
