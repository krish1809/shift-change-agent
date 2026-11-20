"""Helpers shared by tool handlers: roster loading, labels, option building and option matching."""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from psycopg import AsyncConnection

from app.db.repositories import roster
from app.domain.alternatives import Alternative
from app.domain.formatting import fmt_day, fmt_time
from app.domain.models import RosterEntry, Trip
from app.domain.time_resolver import resolve_time
from app.tools.context import ToolContext


def time_label(ctx: ToolContext, trip: Trip) -> str:
    return fmt_time(trip.scheduled_at, ctx.tz)


def day_label(ctx: ToolContext, entry: RosterEntry) -> str:
    return fmt_day(entry.service_date, ctx.today())


def booking_params(ctx: ToolContext, entry: RosterEntry) -> dict[str, Any]:
    """The facts every 'your booking is unchanged' sentence needs."""
    params: dict[str, Any] = {"trip_type": entry.trip_type, "day": day_label(ctx, entry)}
    if entry.trip:
        params["current_time"] = time_label(ctx, entry.trip)
    return params


async def upcoming_entries(ctx: ToolContext, conn: AsyncConnection) -> list[RosterEntry]:
    """The caller's own scheduled trips for today and tomorrow that haven't left yet."""
    today = ctx.today()
    entries = await roster.get_entries_for_employee(
        conn, ctx.auth.company_id, ctx.auth.employee_id, [today, today + timedelta(days=1)]
    )
    now = ctx.now()
    return [e for e in entries if e.status == "scheduled" and e.trip and e.trip.scheduled_at > now]


def option_dicts(ctx: ToolContext, alternatives: list[Alternative]) -> list[dict[str, Any]]:
    return [
        {
            "ref": i,
            "trip_id": alt.trip.id,
            "time": time_label(ctx, alt.trip),
            "seats_left": alt.trip.seats_left,
            "needs_approval": list(alt.needs_approval),
        }
        for i, alt in enumerate(alternatives, start=1)
    ]


_ORDINALS = {
    "first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4, "last": -1,
}  # fmt: skip
_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4}  # only when that is the whole reply


def match_option(ctx: ToolContext, ref_text: str, options: list[dict[str, Any]]) -> dict[str, Any] | None:
    """'2', 'second', 'option 2', 'the 8:30 one', '9 pm one' -> one of the offered options, else None.

    Only the options shown in the last reply are candidates, so an id of another company's trip can
    never resolve here.
    """
    if not options:
        return None
    text = ref_text.lower().strip()
    refs = {o["ref"]: o for o in options}

    pure = re.fullmatch(r"(?:option|number|no\.?|#)?\s*(\d)", text)
    if pure and int(pure.group(1)) in refs:
        return refs[int(pure.group(1))]
    word = re.fullmatch(r"(?:option|number)?\s*(one|two|three|four)", text)
    if word:
        return refs.get(_NUMBER_WORDS[word.group(1)])
    for word, number in _ORDINALS.items():
        if re.search(rf"\b{word}\b", text) and not re.search(r"\d\s*(:|\.|am|pm)", text):
            return options[-1] if number == -1 else refs.get(number)

    # "the 8:30 one": a bare 8:30 has an AM and a PM reading; the option list decides which exists.
    resolved = resolve_time(re.sub(r"\b(the|one|option)\b", " ", text))
    wanted = {resolved.value} if resolved.value else set(resolved.candidates)
    hits = [o for o in options if _parse_label(o["time"]) in {(t.hour, t.minute) for t in wanted}]
    return hits[0] if len(hits) == 1 else None


def _parse_label(label: str) -> tuple[int, int]:
    clock, suffix = label.split()
    hour, minute = (int(x) for x in clock.split(":"))
    return hour % 12 + (12 if suffix == "PM" else 0), minute
