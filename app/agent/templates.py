"""Every sentence that states a fact about a trip, a request, a rule or a failure comes from here.

Templates are pure functions of `params` that handlers built from committed results and the
CALLER's company/policy only. The LLM never writes these sentences, so it can't invent a
confirmation, and an Acme employee can't be quoted a Globex rule.

Failure templates always say (1) what happened, (2) whether anything changed, (3) what to do next.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

P = dict[str, Any]


# ---------------------------------------------------------------- helpers


def options_text(options: list[dict[str, Any]] | None) -> str:
    if not options:
        return ""
    parts = []
    for opt in options:
        seats = opt.get("seats_left")
        seat_txt = f" ({seats} seat{'s' if seats != 1 else ''})" if seats is not None else ""
        approval = " (needs approval)" if opt.get("needs_approval") else ""
        parts.append(f"{opt['ref']}) {opt['time']}{seat_txt}{approval}")
    return " ".join(parts)


def _still_booked(p: P) -> str:
    current = p.get("current_time")
    if current:
        return f"Your {current} {p.get('trip_type', 'trip')} {p.get('day', '')} is still booked.".replace("  ", " ")
    return "Nothing was changed."


def _with_options(p: P, intro: str, *, ask: str = "Want one of these?") -> str:
    text = options_text(p.get("options"))
    return f" {intro}: {text}. {ask}" if text else ""


def _desk(p: P) -> str:
    return f" If you need help urgently, call the transport desk at {p['desk_phone']}." if p.get("desk_phone") else ""


VIOLATION_REASONS: dict[str, Callable[[P, P], str]] = {
    "NO_SEAT": lambda v, p: f"the {v.get('time', p.get('target_time', ''))} {p.get('trip_type', 'cab')} is full",
    "CUTOFF_PASSED": lambda v, p: (
        f"{v['company_name']} needs {v['trip_type']} changes at least {v['cutoff_label']} before the trip, "
        f"and the deadline was {v['deadline']}"
    ),
    "NO_TRIP_AT_TIME": lambda v, p: f"there's no {p.get('trip_type', 'cab')} at {v['requested']} on your route",
    "TRIP_IN_PAST": lambda v, p: f"the {v['time']} cab has already left",
    "ROUTE_NOT_SERVED": lambda v, p: f"that cab's route doesn't cover {v['zone']}",
    "TRIP_TYPE_MISMATCH": lambda v, p: "that cab is for a different day or trip type",
    "TRIP_NOT_FOUND": lambda v, p: "I couldn't find that cab",
    "OUTSIDE_CHANGE_WINDOW": lambda v, p: (
        f"{v['company_name']} allows moving a trip by at most {v['window_label']} without approval"
    ),
    "DAILY_CHANGE_LIMIT": lambda v, p: (
        f"{v['company_name']} allows {v['max_changes']} changes a day and you've already made {v['changes_today']}"
    ),
}


def reasons(p: P) -> str:
    texts = [VIOLATION_REASONS[v["code"]](v["params"], p) for v in p.get("violations", [])]
    if not texts:
        return "that change isn't allowed"
    return texts[0] if len(texts) == 1 else "; ".join(texts[:-1]) + "; and " + texts[-1]


# ---------------------------------------------------------------- reads


def roster(p: P) -> str:
    lines = p.get("lines") or []
    if not lines:
        return f"You have no trips booked {p.get('scope', 'in the next two days')}."
    return "Here's your schedule: " + "; ".join(lines) + "."


def options(p: P) -> str:
    text = options_text(p.get("options"))
    if not text:
        return (
            f"I couldn't find any {p['trip_type']} {p['day']} with free seats that you could switch to. "
            f"{_still_booked(p)}{_desk(p)}"
        )
    return f"{p['trip_type'].capitalize()}s {p['day']} with seats: {text}. Want me to move you to one of these?"


def policy(p: P) -> str:
    escal = (
        "If you're past the cut-off I can ask your transport manager to approve it."
        if p["cutoff_escalatable"]
        else "After the cut-off, changes aren't possible; call the transport desk."
    )
    daily = f" You can make up to {p['max_changes']} changes a day." if p.get("max_changes") else ""
    return (
        f"{p['company_name']} rules: drop changes at least {p['cutoff_drop']} before the trip, pickup changes "
        f"at least {p['cutoff_pickup']} before. {escal} Moves of more than {p['window']} need approval.{daily} "
        f"Transport desk: {p['desk_phone']}."
    )


# ---------------------------------------------------------------- clarification


def clarify_time(p: P) -> str:
    current = f" It's currently {p['current_time']}." if p.get("current_time") else ""
    label = {"later": "Later", "earlier": "Earlier"}.get(p.get("direction") or "", "Nearest")
    extra = _with_options(p, f"{label} {p['trip_type']}s with seats", ask="Pick one or tell me a time.")
    return f"Sure — what time would you like your {p['trip_type']} {p['day']} instead?{current}{extra}"


def clarify_time_ambiguous(p: P) -> str:
    return f"Just to be sure — did you mean {p['candidates']} for your {p['trip_type']} {p['day']}?"


def clarify_trip_type(p: P) -> str:
    return f"Which one would you like to change {p['day_hint']}: {p['choices']}?"


def clarify_date(p: P) -> str:
    return f"Which day's {p['trip_type']} do you mean: {p['choices']}?"


def confirm_interpretation(p: P) -> str:
    return (
        f"Just to check — did you mean {p['suggested_time']} for your {p['trip_type']} {p['day']}? "
        "Reply yes and I'll check it, or tell me another time."
    )


def option_not_found(p: P) -> str:
    extra = _with_options(p, "The options I can offer you are", ask="Pick one by number or time.")
    return f"I couldn't match that to one of your options.{extra or ' What time would you like instead?'}"


# ---------------------------------------------------------------- outcomes


def applied_reschedule(p: P) -> str:
    note = " (It took a little longer than usual to confirm.)" if p.get("reconciled") else ""
    return (
        f"Done — your {p['trip_type']} {p['day']} is now {p['new_time']} (cab {p['cab']}). "
        f"Your {p['old_time']} seat has been released. Ref {p['request_id']}.{note}"
    )


def applied_cancel(p: P) -> str:
    note = " (It took a little longer than usual to confirm.)" if p.get("reconciled") else ""
    return (
        f"Done — your {p['old_time']} {p['trip_type']} {p['day']} is cancelled and the seat released. "
        f"Ref {p['request_id']}.{note}"
    )


def already_on_trip(p: P) -> str:
    if p.get("cancelled"):
        return f"Your {p['trip_type']} {p['day']} is already cancelled, so there's nothing to change."
    return f"You're already on the {p['current_time']} {p['trip_type']} {p['day']}, so nothing needed changing."


def rejected(p: P) -> str:
    alt = _with_options(p, f"Nearest {p.get('trip_type', 'trip')}s with seats")
    desk = "" if alt else _desk(p)
    return f"I can't make that change: {reasons(p)}. {_still_booked(p)}{alt}{desk}"


def offer_escalation(p: P) -> str:
    alt = _with_options(p, "Or, without approval, you could take", ask="")
    return (
        f"I can't apply this directly: {reasons(p)}. I can send it to your transport manager — decisions can "
        f"take a few hours, and your current {p['current_time']} {p['trip_type']} stays booked meanwhile. "
        f"Shall I send it?{alt}"
    ).rstrip()


def escalated(p: P) -> str:
    return (
        f"Sent to your transport manager (Ref {p['request_id']}): {p['trip_type']} {p['current_time']} → "
        f"{p['target_label']}. Your {p['current_time']} {p['trip_type']} stays booked until they decide. "
        "I'll post the decision here."
    )


def seat_taken(p: P) -> str:
    alt = _with_options(p, f"Nearest {p['trip_type']}s with seats")
    return f"Sorry — the {p['target_time']} cab filled up while we were talking. {_still_booked(p)}{alt or _desk(p)}"


def roster_changed(p: P) -> str:
    return (
        f"Your booking changed while we were talking — you're now on the {p['current_time']} "
        f"{p['trip_type']} {p['day']}. Nothing else was changed. Want to change it again?"
    )


def duplicate(p: P) -> str:
    return f"You already have request {p['request_id']} for this ({p['status_words']}), so I didn't create another one."


def offer_declined(p: P) -> str:
    return f"OK, I won't send it. {_still_booked(p)} Anything else?"


def interpretation_declined(p: P) -> str:
    return f"OK. What time would you like your {p['trip_type']} {p['day']} instead?"


def no_offer(_: P) -> str:
    return "There's nothing waiting for a yes or no from me right now. What would you like to change?"


def not_found(p: P) -> str:
    return f"I couldn't find {p.get('what', 'that')}. {p.get('hint', 'Ask me about your own pickups and drops.')}"


# ---------------------------------------------------------------- approval lifecycle


def status_pending(p: P) -> str:
    return (
        f"Your request {p['request_id']} ({p['summary']}) has been with your transport manager since "
        f"{p['since']}. {_still_booked(p)} I'll post the decision here. For anything urgent, call "
        f"{p['desk_phone']}."
    )


def status_final(p: P) -> str:
    return f"Your request {p['request_id']} ({p['summary']}) is {p['status_words']}."


def withdrawn(p: P) -> str:
    return f"Done — I've withdrawn request {p['request_id']}. {_still_booked(p)}"


def pending_blocked(p: P) -> str:
    return (
        f"This chat is waiting for your transport manager's decision on {p['request_id']}. I can tell you its "
        "status or withdraw it. To change a different trip, start a new chat."
    )


def approved_applied(p: P) -> str:
    return (
        f"Good news — your transport manager approved request {p['request_id']}. Your {p['trip_type']} "
        f"{p['day']} is now {p['new_time']}{' (cab ' + p['cab'] + ')' if p.get('cab') else ''}."
    )


def approved_applied_cancel(p: P) -> str:
    return (
        f"Good news — your transport manager approved request {p['request_id']}. Your {p['old_time']} "
        f"{p['trip_type']} {p['day']} is cancelled."
    )


def approval_rejected(p: P) -> str:
    note = f' Their note: "{p["note"]}".' if p.get("note") else ""
    return f"Your transport manager declined request {p['request_id']}.{note} {_still_booked(p)}"


def approved_but_unavailable(p: P) -> str:
    alt = _with_options(p, f"Nearest {p['trip_type']}s with seats")
    return (
        f"Your transport manager approved request {p['request_id']}, but {p['reason']}, so nothing was changed. "
        f"{_still_booked(p)}{alt or _desk(p)}"
    )


def expired(p: P) -> str:
    return (
        f"Your manager didn't respond in time, so request {p['request_id']} has expired and your "
        f"{p['current_time']} {p['trip_type']} {p['day']} stays as it was."
    ).replace("  ", " ")


# ---------------------------------------------------------------- failures


def tool_unavailable(p: P) -> str:
    booking = p.get("booking_line") or "your bookings are as they were"
    return (
        f"I couldn't reach the {p.get('system', 'cab schedule')} just now, so nothing was changed — {booking}. "
        "Please try again in a minute."
    )


def fallback_llm_down(p: P) -> str:
    return (
        "I'm having trouble understanding messages right now. Nothing was changed. Try again shortly, or type "
        "it like: change my drop today to 8:30 pm."
    )


def could_not_understand(_: P) -> str:
    return (
        "Sorry, I didn't catch that and nothing was changed. You can say things like \"move my drop today to "
        '8:30 pm", "cancel tomorrow\'s pickup" or "what\'s my schedule?".'
    )


def generic_help(_: P) -> str:
    return (
        "I can help with your cab pickups and drops: check your schedule, move a trip, cancel one, or check a "
        "request. What would you like to do?"
    )


def turn_timeout(p: P) -> str:
    return (
        "This is taking longer than usual. I'll post the result in this chat as soon as it's done; "
        "until you see a confirmation, assume nothing has changed."
    )


def internal_error(p: P) -> str:
    return f"Something went wrong on our side and nothing was changed. Please try again. (Ref: {p['correlation_id']})"


TEMPLATES: dict[str, Callable[[P], str]] = {
    name: fn
    for name, fn in dict(globals()).items()
    if callable(fn)
    and not name.startswith("_")
    and name not in {"options_text", "reasons", "render", "Callable", "Any"}
    and getattr(fn, "__module__", None) == __name__
}


def render(key: str, params: P | None = None) -> str:
    return TEMPLATES[key](params or {})
