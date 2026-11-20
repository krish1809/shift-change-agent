"""request_shift_change and respond_to_offer: resolve what the user means, run the rules, act.

The model only supplied verbatim words. Everything below is deterministic code over the caller's own
company data, and every answer is a typed ToolResult that templates turn into text.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime, time, timedelta
from typing import Any

from psycopg import AsyncConnection

from app.db.repositories import requests as requests_repo
from app.db.repositories import roster, trips
from app.domain import booking
from app.domain.alternatives import Alternative, find_alternatives
from app.domain.booking import BookingStatus, ChangeSpec
from app.domain.formatting import fmt_day, fmt_time
from app.domain.models import RosterEntry, Trip, Violation
from app.domain.rules.base import RuleContext
from app.domain.rules.engine import evaluate
from app.domain.time_resolver import resolve_date, resolve_time
from app.tools.common import (
    booking_params,
    day_label,
    match_option,
    option_dicts,
    time_label,
    upcoming_entries,
)
from app.tools.context import ToolContext
from app.tools.results import ToolResult
from app.tools.schemas import RequestShiftChangeArgs, RespondToOfferArgs

LATER = re.compile(r"\b(late|later|after|delay|push|postpone)\b", re.I)
EARLIER = re.compile(r"\b(early|earlier|before|advance|prepone)\b", re.I)


def entry_draft(entry: RosterEntry, change_type: str = "reschedule") -> dict[str, Any]:
    """What a follow-up like 'the 8:30 one' needs to know: which trip we were talking about."""
    return {"trip_type": entry.trip_type, "date": entry.service_date.isoformat(), "change_type": change_type}


def _violation_dicts(violations: list[Violation]) -> list[dict[str, Any]]:
    return [v.model_dump() for v in violations]


def _request_info(request: Any) -> dict[str, Any] | None:
    return {"id": request.id, "status": request.status} if request else None


# ------------------------------------------------------------------ resolving WHICH trip


def _entry_phrase(ctx: ToolContext, entry: RosterEntry) -> str:
    return f"your {entry.trip_type} {day_label(ctx, entry)} ({time_label(ctx, entry.trip)})"  # type: ignore[arg-type]


async def _pick_entry(
    ctx: ToolContext,
    conn: AsyncConnection,
    *,
    trip_type: str | None,
    day: Any,
    draft: dict[str, Any],
    assumed_day: bool = False,
) -> RosterEntry | ToolResult:
    entries = await upcoming_entries(ctx, conn)
    if day is not None and assumed_day:
        same_type = [e.service_date for e in entries if trip_type is None or e.trip_type == trip_type]
        if not same_type or day != min(same_type):
            day = None  # the model assumed a day the user never said, and it isn't the obvious next trip
    candidates = [
        e for e in entries if (trip_type is None or e.trip_type == trip_type) and (day is None or e.service_date == day)
    ]
    keep = {
        "draft": {
            **draft,
            **({"trip_type": trip_type} if trip_type else {}),
            **({"date": day.isoformat()} if day else {}),
        }
    }
    if not candidates:
        when = fmt_day(day, ctx.today()) if day else "in the next two days"
        what = f"a {trip_type or 'trip'} {when} that can still be changed"
        return ToolResult(
            status="NOT_FOUND",
            template="not_found",
            params={"what": what, "hint": "Ask me for your schedule to see your upcoming trips."},
        )
    if len(candidates) == 1:
        return candidates[0]
    if len({e.trip_type for e in candidates}) > 1 and trip_type is None:
        choices = " or ".join(_entry_phrase(ctx, e) for e in candidates)
        return ToolResult(
            status="NEEDS_CLARIFICATION",
            template="clarify_trip_type",
            params={
                "day_hint": "(today or tomorrow)" if day is None else fmt_day(day, ctx.today()),
                "choices": choices,
            },
            updates=keep,
        )
    choices = " or ".join(_entry_phrase(ctx, e) for e in candidates)
    return ToolResult(
        status="NEEDS_CLARIFICATION",
        template="clarify_date",
        params={"trip_type": candidates[0].trip_type, "choices": choices},
        updates=keep,
    )


# ------------------------------------------------------------------ resolving WHERE to


def _find_trip_at(trips_on_day: list[Trip], zone: str, at: time, ctx: ToolContext) -> Trip | None:
    matching = [t for t in trips_on_day if t.local_time(ctx.tz) == at]
    serving = [t for t in matching if zone in t.route_zones]
    return (serving or matching or [None])[0]


def _base_context(ctx: ToolContext, entry: RosterEntry, change_type: str, changes_today: int = 0) -> RuleContext:
    return RuleContext(
        company=ctx.company,
        policy=ctx.policy,
        employee=ctx.employee,
        now=ctx.now(),
        change_type=change_type,
        entry=entry,
        current_trip=entry.trip,
        target_trip=None,  # type: ignore[arg-type]
        changes_today=changes_today,
    )


async def _changes_today(ctx: ToolContext, conn: AsyncConnection) -> int:
    start = datetime.combine(ctx.today(), time.min, ctx.tz)
    return await requests_repo.count_applied_changes(
        conn, ctx.auth.company_id, ctx.auth.employee_id, since=start, until=start + timedelta(days=1)
    )


async def _alternatives(
    ctx: ToolContext, conn: AsyncConnection, entry: RosterEntry, base: RuleContext, **kw: Any
) -> list[Alternative]:
    candidates = await trips.list_trips(
        conn, ctx.auth.company_id, trip_type=entry.trip_type, service_date=entry.service_date
    )
    return find_alternatives(candidates, base, **kw)


def _with_options(result: ToolResult, ctx: ToolContext, alts: list[Alternative]) -> ToolResult:
    options = option_dicts(ctx, alts)
    result.params["options"] = options
    result.options = options
    result.updates["offered_options"] = options
    return result


# ------------------------------------------------------------------ request_shift_change


async def request_shift_change(ctx: ToolContext, args: RequestShiftChangeArgs) -> ToolResult:
    draft = dict(ctx.draft or {})
    trip_type = args.trip_type or draft.get("trip_type")
    day = None
    assumed = False
    if args.date_text:
        resolved = resolve_date(args.date_text, today=ctx.today())
        day = resolved.value if resolved.kind == "exact" else None
        assumed = "date_text" in ctx.ungrounded
    if day is None and draft.get("date"):
        day = datetime.fromisoformat(draft["date"]).date()

    async with ctx.pool.connection() as conn:
        picked = await _pick_entry(ctx, conn, trip_type=trip_type, day=day, draft=draft, assumed_day=assumed)
        if isinstance(picked, ToolResult):
            return picked
        entry = picked
        draft = {
            **draft,
            "trip_type": entry.trip_type,
            "date": entry.service_date.isoformat(),
            "change_type": args.change_type,
        }

        if args.change_type == "cancel":
            return await process_change(ctx, conn, entry, "cancel", None, escalate=False)
        return await _reschedule(ctx, conn, entry, args, draft)


async def _reschedule(
    ctx: ToolContext, conn: AsyncConnection, entry: RosterEntry, args: RequestShiftChangeArgs, draft: dict[str, Any]
) -> ToolResult:
    base = _base_context(ctx, entry, "reschedule")
    params = {**booking_params(ctx, entry)}
    current_local = entry.trip.local_time(ctx.tz)  # type: ignore[union-attr]
    day_trips = await trips.list_trips(
        conn, ctx.auth.company_id, trip_type=entry.trip_type, service_date=entry.service_date
    )

    # 1) the user pointed at an option from the last reply ("the 8:30 one", "2")
    if args.option_ref:
        option = match_option(ctx, args.option_ref, ctx.offered_options)
        if option is not None:
            target = next((t for t in day_trips if t.id == option["trip_id"]), None)
            if target is not None:
                return await process_change(ctx, conn, entry, "reschedule", target, escalate=False, from_offer=True)
        if not args.time_text:
            as_time = resolve_time(
                args.option_ref,
                trip_type=entry.trip_type,
                current_time=current_local,
                window_minutes=ctx.policy.change_window_minutes,
            )
            if as_time.kind == "unparseable":
                alts = await _alternatives(ctx, conn, entry, base)
                result = ToolResult(
                    status="NEEDS_CLARIFICATION", template="option_not_found", params=params, updates={"draft": draft}
                )
                return _with_options(result, ctx, alts)
            args = args.model_copy(update={"time_text": args.option_ref})

    # 2) the user gave a time
    if args.time_text:
        resolved = resolve_time(
            args.time_text,
            trip_type=entry.trip_type,
            current_time=current_local,
            window_minutes=ctx.policy.change_window_minutes,
        )
        if resolved.kind == "ambiguous":
            candidates = " or ".join(fmt_time(c) for c in resolved.candidates)
            return ToolResult(
                status="NEEDS_CLARIFICATION",
                template="clarify_time_ambiguous",
                params={**params, "candidates": candidates},
                updates={"draft": {**draft, "time_options": [c.strftime("%H:%M") for c in resolved.candidates]}},
            )
        if resolved.kind == "exact" and resolved.value is not None:
            target = _find_trip_at(day_trips, ctx.employee.zone, resolved.value, ctx)
            if target is not None:
                return await process_change(ctx, conn, entry, "reschedule", target, escalate=False)
            desired = datetime.combine(entry.service_date, resolved.value, ctx.tz)
            return await _no_trip_at_time(ctx, conn, entry, base, desired)

    # 3) nothing usable: a suggestion (never applied) or an open question with real options
    if args.suggested_time:
        suggestion = resolve_time(
            args.suggested_time,
            trip_type=entry.trip_type,
            current_time=current_local,
            window_minutes=ctx.policy.change_window_minutes,
        )
        if suggestion.kind == "exact" and suggestion.value is not None:
            label = fmt_time(suggestion.value)
            return ToolResult(
                status="NEEDS_CLARIFICATION",
                template="confirm_interpretation",
                params={**params, "suggested_time": label},
                updates={
                    "draft": draft,
                    "pending_offer": {
                        "kind": "confirm_interpretation",
                        "time": suggestion.value.strftime("%H:%M"),
                        "draft": draft,
                    },
                },
            )
    direction = (
        "later" if LATER.search(ctx.last_user_text) else "earlier" if EARLIER.search(ctx.last_user_text) else None
    )
    alts = await _alternatives(ctx, conn, entry, base, direction=direction)
    result = ToolResult(
        status="NEEDS_CLARIFICATION",
        template="clarify_time",
        params={**params, "direction": direction},
        updates={"draft": draft},
    )
    return _with_options(result, ctx, alts)


async def _no_trip_at_time(
    ctx: ToolContext, conn: AsyncConnection, entry: RosterEntry, base: RuleContext, desired: datetime
) -> ToolResult:
    label = fmt_time(desired)
    result_rules = evaluate(replace(base, requested_label=label))
    alts = await _alternatives(ctx, conn, entry, base, desired_at=desired)
    params = {**booking_params(ctx, entry), "violations": _violation_dicts(result_rules.violations)}
    await _record_rejection(ctx, conn, entry, "reschedule", None, result_rules.violations)
    result = ToolResult(status="REJECTED", template="rejected", params=params, updates={"draft": entry_draft(entry)})
    return _with_options(result, ctx, alts)


# ------------------------------------------------------------------ rules + action


async def _record_rejection(
    ctx: ToolContext,
    conn: AsyncConnection,
    entry: RosterEntry,
    change_type: str,
    target: Trip | None,
    violations: list[Violation],
) -> None:
    spec = _spec(ctx, entry, change_type, target)
    await booking.record_request(
        conn,
        spec,
        status="REJECTED",
        from_trip_id=entry.trip_id,
        violations=_violation_dicts(violations),
        now=ctx.now(),
    )


def _spec(ctx: ToolContext, entry: RosterEntry, change_type: str, target: Trip | None) -> ChangeSpec:
    return ChangeSpec(
        company_id=ctx.auth.company_id,
        employee_id=ctx.auth.employee_id,
        roster_entry_id=entry.id,
        change_type=change_type,
        to_trip_id=target.id if target else None,  # type: ignore[arg-type]
        expected_version=entry.version,
        thread_id=ctx.thread_id,
    )


async def process_change(
    ctx: ToolContext,
    conn: AsyncConnection,
    entry: RosterEntry,
    change_type: str,
    target: Trip | None,
    *,
    escalate: bool,
    from_offer: bool = False,
) -> ToolResult:
    """Check every rule, then apply / offer escalation / reject. `escalate=True` = the user already said yes."""
    base = _base_context(ctx, entry, change_type, await _changes_today(ctx, conn))
    verdict = evaluate(replace(base, target_trip=target))
    params = booking_params(ctx, entry)
    desired = target.scheduled_at if target else None
    clear = {"draft": None, "offered_options": [], "pending_offer": None}

    if verdict.already_on_trip:
        return ToolResult(
            status="ALREADY_ON_TRIP",
            template="already_on_trip",
            params={**params, "cancelled": change_type == "cancel"},
            updates=clear,
        )

    if not verdict.violations:
        return await _apply(ctx, conn, entry, change_type, target, base)

    if from_offer and "NO_SEAT" in verdict.codes and target is not None:
        return await _seat_taken(ctx, conn, entry, base, target)  # it was offered a moment ago; now it's gone

    params = {
        **params,
        "violations": _violation_dicts(verdict.violations),
        "target_time": time_label(ctx, target) if target else None,
    }

    if verdict.escalatable and escalate:
        return await _escalate(ctx, conn, entry, change_type, target, verdict.violations)

    if verdict.escalatable:
        alts = await _alternatives(ctx, conn, entry, base, desired_at=desired, only_allowed=True) if target else []
        offer = {
            "kind": "escalate",
            "entry_id": entry.id,
            "change_type": change_type,
            "target_trip_id": target.id if target else None,
        }
        result = ToolResult(
            status="NEEDS_APPROVAL",
            template="offer_escalation",
            params=params,
            updates={"pending_offer": offer, "draft": entry_draft(entry, change_type)},
        )
        return _with_options(result, ctx, alts)

    await _record_rejection(ctx, conn, entry, change_type, target, verdict.violations)
    alts = await _alternatives(ctx, conn, entry, base, desired_at=desired) if change_type == "reschedule" else []
    params["desk_phone"] = ctx.company.desk_phone
    result = ToolResult(
        status="REJECTED",
        template="rejected",
        params=params,
        updates={"draft": entry_draft(entry, change_type), "pending_offer": None},
    )
    return _with_options(result, ctx, alts)


async def _seat_taken(
    ctx: ToolContext, conn: AsyncConnection, entry: RosterEntry, base: RuleContext, target: Trip | None
) -> ToolResult:
    """The cab the user chose filled up (lost the atomic race, or was taken while they were deciding)."""
    alts = await _alternatives(ctx, conn, entry, base, desired_at=target.scheduled_at if target else None)
    out = ToolResult(
        status="SEAT_TAKEN",
        template="seat_taken",
        params={**booking_params(ctx, entry), "target_time": time_label(ctx, target)},  # type: ignore[arg-type]
        updates={"draft": entry_draft(entry), "pending_offer": None},
    )
    return _with_options(out, ctx, alts)


async def _apply(
    ctx: ToolContext,
    conn: AsyncConnection,
    entry: RosterEntry,
    change_type: str,
    target: Trip | None,
    base: RuleContext,
) -> ToolResult:
    spec = _spec(ctx, entry, change_type, target)
    params = {
        **booking_params(ctx, entry),
        "old_time": time_label(ctx, entry.trip),  # type: ignore[arg-type]
        "new_time": time_label(ctx, target) if target else None,
        "cab": target.cab_label if target else None,
    }
    template = "applied_reschedule" if change_type == "reschedule" else "applied_cancel"
    # Remember how to describe the outcome if this write times out and has to be reconciled.
    ctx.scratch["reconcile"] = {
        "key": booking.idempotency_key(spec),
        "expect": "APPLIED",
        "template": template,
        "params": params,
    }
    result = await booking.apply_change(conn, spec, now=ctx.now())
    await ctx.faults.after("request_shift_change")
    clear = {"draft": None, "offered_options": [], "pending_offer": None}

    if result.status == BookingStatus.APPLIED:
        return ToolResult(
            status="APPLIED",
            template=template,
            params={**params, "request_id": result.request.id},  # type: ignore[union-attr]
            request=_request_info(result.request),
            updates=clear,
        )
    if result.status == BookingStatus.ALREADY_ON_TRIP:
        return ToolResult(
            status="ALREADY_ON_TRIP",
            template="already_on_trip",
            params={**params, "cancelled": change_type == "cancel"},
            updates=clear,
        )
    if result.status == BookingStatus.DUPLICATE:
        existing = result.request
        words = "already done" if existing and existing.status == "APPLIED" else "waiting for approval"
        return ToolResult(
            status="DUPLICATE",
            template="duplicate",
            request=_request_info(existing),
            updates=clear,
            params={"request_id": existing.id if existing else "", "status_words": words},
        )
    if result.status == BookingStatus.SEAT_TAKEN:
        return await _seat_taken(ctx, conn, entry, base, target)
    if result.status == BookingStatus.ROSTER_CHANGED:
        fresh = await roster.get_entry(conn, ctx.auth.company_id, ctx.auth.employee_id, entry.id)
        if fresh is not None and fresh.version != entry.version and not ctx.scratch.get("retried"):
            ctx.scratch["retried"] = True
            return await process_change(ctx, conn, fresh, change_type, target, escalate=False)
        current = fresh or entry
        return ToolResult(
            status="REJECTED", template="roster_changed", params=booking_params(ctx, current), updates=clear
        )
    return ToolResult(status="NOT_FOUND", template="not_found", params={}, updates=clear)


async def _escalate(
    ctx: ToolContext,
    conn: AsyncConnection,
    entry: RosterEntry,
    change_type: str,
    target: Trip | None,
    violations: list[Violation],
) -> ToolResult:
    spec = _spec(ctx, entry, change_type, target)
    key = booking.idempotency_key(spec, salt="escalate")
    params = {
        **booking_params(ctx, entry),
        "target_label": time_label(ctx, target) if target else "cancelled",
    }
    ctx.scratch["reconcile"] = {"key": key, "expect": "PENDING_APPROVAL", "template": "escalated", "params": params}
    result = await booking.record_request(
        conn,
        spec,
        status="PENDING_APPROVAL",
        from_trip_id=entry.trip_id,
        violations=_violation_dicts(violations),
        now=ctx.now(),
        key=key,
    )
    await ctx.faults.after("respond_to_offer")
    request = result.request
    if request is None:
        return ToolResult(status="NOT_FOUND", template="not_found", params={})
    if result.status == BookingStatus.DUPLICATE:
        return ToolResult(
            status="DUPLICATE",
            template="duplicate",
            request=_request_info(request),
            params={"request_id": request.id, "status_words": "waiting for approval"},
            updates={"pending_offer": None},
        )
    return ToolResult(
        status="PENDING_APPROVAL",
        template="escalated",
        params={**params, "request_id": request.id},
        request=_request_info(request),
        updates={"pending_offer": None, "draft": None, "offered_options": [], "pending_request_id": request.id},
    )


# ------------------------------------------------------------------ respond_to_offer


async def respond_to_offer(ctx: ToolContext, args: RespondToOfferArgs) -> ToolResult:
    offer = ctx.pending_offer
    if not offer:
        return ToolResult(status="NOT_FOUND", template="no_offer", params={})

    if offer["kind"] == "confirm_interpretation":
        draft = offer.get("draft") or {}
        if not args.accept:
            return ToolResult(
                status="NEEDS_CLARIFICATION",
                template="interpretation_declined",
                params={"trip_type": draft.get("trip_type", "trip"), "day": "today"},
                updates={"pending_offer": None, "draft": draft},
            )
        ctx.draft = draft
        # The user confirmed this exact time, so it is now their own word: ground it.
        ctx.recent_user_texts.append(offer["time"])
        follow_up = RequestShiftChangeArgs(change_type="reschedule", time_text=offer["time"])
        return await request_shift_change(ctx, follow_up)

    # kind == "escalate"
    async with ctx.pool.connection() as conn:
        entry = await roster.get_entry(conn, ctx.auth.company_id, ctx.auth.employee_id, offer["entry_id"])
        if entry is None or entry.trip is None:
            return ToolResult(status="NOT_FOUND", template="not_found", params={})
        params = booking_params(ctx, entry)
        if not args.accept:
            return ToolResult(
                status="NEEDS_CLARIFICATION", template="offer_declined", params=params, updates={"pending_offer": None}
            )
        target = None
        if offer.get("target_trip_id"):
            target = await trips.get_trip(conn, ctx.auth.company_id, offer["target_trip_id"])
        # Re-run the rules: seats and time may have moved since the offer. Never escalate stale facts.
        return await process_change(ctx, conn, entry, offer["change_type"], target, escalate=True)
