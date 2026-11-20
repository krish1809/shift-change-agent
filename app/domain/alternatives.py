"""Nearest-slot finder: what to offer when the asked-for trip can't be had (or wasn't named)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal

from app.domain.models import Trip
from app.domain.rules.base import RuleContext
from app.domain.rules.engine import evaluate

Direction = Literal["later", "earlier"]


@dataclass(frozen=True)
class Alternative:
    trip: Trip
    needs_approval: tuple[str, ...] = ()  # escalatable violation codes; empty = can be applied directly

    @property
    def allowed(self) -> bool:
        return not self.needs_approval


def find_alternatives(
    candidates: list[Trip],
    base: RuleContext,
    *,
    desired_at: datetime | None = None,
    direction: Direction | None = None,
    exclude_trip_ids: frozenset[str] = frozenset(),
    only_allowed: bool = False,
    not_before: datetime | None = None,
    not_after: datetime | None = None,
) -> list[Alternative]:
    """Same company/type/date trips with a free seat, nearest to `desired_at` first (ties: earlier).

    `base` is the rule context of the request with no target; every candidate is checked by the real
    rules engine, hard-violating ones are dropped and the rest annotated allowed / needs approval.
    """
    current = base.current_trip
    anchor = desired_at or (current.scheduled_at if current else None)
    policy = base.policy
    found: list[tuple[float, datetime, Alternative]] = []

    for trip in candidates:
        if trip.company_id != base.company.id:  # defence in depth; the query is already scoped
            continue
        if trip.id in exclude_trip_ids or (current is not None and trip.id == current.id):
            continue
        if trip.seats_left <= 0 or trip.scheduled_at <= base.now:
            continue
        if base.employee.zone not in trip.route_zones:
            continue
        if (not_before and trip.scheduled_at < not_before) or (not_after and trip.scheduled_at > not_after):
            continue
        if direction and current is not None:
            if direction == "later" and trip.scheduled_at <= current.scheduled_at:
                continue
            if direction == "earlier" and trip.scheduled_at >= current.scheduled_at:
                continue
        distance = abs((trip.scheduled_at - anchor).total_seconds()) / 60 if anchor else 0.0
        if anchor is not None and distance > policy.alternatives_window_minutes:
            continue

        result = evaluate(replace(base, change_type="reschedule", target_trip=trip, requested_label=None))
        if result.already_on_trip or any(not v.escalatable for v in result.violations):
            continue
        alternative = Alternative(trip=trip, needs_approval=tuple(result.codes))
        if only_allowed and not alternative.allowed:
            continue
        found.append((distance, trip.scheduled_at, alternative))

    found.sort(key=lambda item: (item[0], item[1]))
    return [alt for _, _, alt in found[: policy.alternatives_count]]
