"""Degraded-mode interpreter: regexes for the canonical phrasings, used when no LLM is reachable.

It produces exactly the same tool-call shape as a real model, so everything downstream (gate, rules,
templates) is unchanged. It never invents anything: unknown text gets no tool call.
"""

from __future__ import annotations

import re
from typing import Any

from app.llm.base import LLMResponse, Message

TIME = (
    r"(?:\d{1,2}(?:[:.]\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)?|noon|midnight|half past \d{1,2}|quarter (?:to|past) \d{1,2})"
)
DATE = r"(today|tonight|tomorrow|tmrw)"
TYPE = r"(pick[- ]?up|drop)"


def _call(name: str, **args: Any) -> LLMResponse:
    return LLMResponse(tool_calls=[{"name": name, "args": {k: v for k, v in args.items() if v is not None}}])


def _trip_type(text: str | None) -> str | None:
    if not text:
        return None
    return "drop" if "drop" in text else "pickup"


class RuleBasedLLM:
    async def invoke(self, messages: list[Message], tools: list[dict[str, Any]], *, timeout_s: float) -> LLMResponse:
        user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        return interpret(user)


def interpret(raw: str) -> LLMResponse:
    text = re.sub(r"\s+", " ", raw.lower()).strip(" .!?")
    if not text:
        return LLMResponse()

    if re.fullmatch(
        r"(yes|yeah|yep|yup|sure|ok|okay|please do|go ahead|send it|yes please|yes,? .*manager.*|ask my manager)", text
    ):
        return _call("respond_to_offer", accept=True)
    if re.fullmatch(r"(no|nope|nah|no thanks|don'?t|cancel that|never ?mind)", text):
        return _call("respond_to_offer", accept=False)

    if re.search(r"\b(thanks|thank you|thx)\b", text):
        return LLMResponse(text="You're welcome!")
    if re.fullmatch(r"(hi|hello|hey)( there)?", text):
        return LLMResponse(text="Hi! I can help with your cab pickups and drops. What would you like to do?")

    if re.search(r"\bwithdraw\b", text):
        return _call("withdraw_request")
    if re.search(r"\b(status|any (update|news)|update on|where is my request)\b", text):
        return _call("get_request_status")
    if re.search(r"\b(cut-?off|policy|rules?)\b", text):
        return _call("get_my_policy")

    cancel = re.search(rf"\bcancel\b.*?\b{TYPE}\b|\b{TYPE}\b.*?\bcancel\b", text)
    if cancel:
        trip = _trip_type(text.split("cancel")[0] + text.split("cancel")[-1]) or _trip_type(text)
        return _call("request_shift_change", change_type="cancel", trip_type=trip, date_text=_find(DATE, text))

    if re.search(r"\b(what|which|show|any|available)\b.*\b(drops?|pick-?ups?|cabs?|options|slots)\b", text):
        return _call(
            "check_cab_options",
            trip_type=_trip_type(text) if re.search(TYPE, text) else None,
            date_text=_find(DATE, text),
            time_text=_after_word(text),
        )
    if re.search(r"\b(my )?(schedule|roster|bookings?)\b|\bwhat time is my\b", text):
        return _call("get_my_roster", date_text=_find(DATE, text))

    if re.fullmatch(r"(option |number |no\.? |#)?\d", text) or re.fullmatch(
        r"(the )?(first|second|third|1st|2nd|3rd)( one)?|(the )?\d{1,2}([:.]\d{2})? ?(am|pm)? one", text
    ):
        return _call("request_shift_change", change_type="reschedule", option_ref=text)

    change = re.search(r"\b(change|move|shift|push|make|set|reschedule|switch)\b", text)
    if change and (re.search(TYPE, text) or re.search(rf"\b(to|at|for)\b {TIME}\b|make it {TIME}", text)):
        return _call(
            "request_shift_change",
            change_type="reschedule",
            trip_type=_trip_type(text) if re.search(TYPE, text) else None,
            date_text=_find(DATE, text),
            time_text=_time_span(text),
        )
    if re.fullmatch(rf"(make it |at |to )?{TIME}( instead| please)?", text):
        return _call("request_shift_change", change_type="reschedule", time_text=_time_span(text))
    return LLMResponse()


def _find(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text)
    return match.group(1) if match else None


def _time_span(text: str) -> str | None:
    match = re.search(
        rf"(?:\bto\b|\bat\b|\bfor\b|make it|^)\s*({TIME})(?:\s+(?:instead|please|today|tonight|tomorrow))?\s*$", text
    )
    if match:
        return match.group(1).strip()
    match = re.search(rf"\b(?:to|at|for)\s+({TIME})\b", text)
    return match.group(1).strip() if match else None


def _after_word(text: str) -> str | None:
    match = re.search(rf"\b(?:after|around|at|by)\s+({TIME})\b", text)
    return match.group(1).strip() if match else None
