"""Turns the user's own words ("8", "half past 8", "30 min later", "tomorrow") into times and dates.

The LLM only copies spans; this module decides what they mean. When a span has two sensible
readings (a bare "9" could be 9 AM or 9 PM) it returns `ambiguous` so the agent asks a question
instead of guessing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, time, timedelta
from typing import Literal

Kind = Literal["exact", "ambiguous", "unparseable"]

# When the current trip time is unknown, a typical time for the trip type anchors AM/PM.
ANCHORS = {"pickup": time(9, 0), "drop": time(19, 0)}


@dataclass(frozen=True)
class TimeResolution:
    kind: Kind
    value: time | None = None
    candidates: tuple[time, ...] = ()


@dataclass(frozen=True)
class DateResolution:
    kind: Kind
    value: date | None = None
    candidates: tuple[date, ...] = ()


UNPARSEABLE_TIME = TimeResolution("unparseable")
UNPARSEABLE_DATE = DateResolution("unparseable")


def _normalise(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"\bp\.?\s*m\.?(?=\s|$)", "pm", text)
    text = re.sub(r"\ba\.?\s*m\.?(?=\s|$)", "am", text)
    text = text.replace("o'clock", "").replace("oclock", "").replace("hrs", "hours")
    return re.sub(r"\s+", " ", text).strip()


def _minutes(t: time) -> int:
    return t.hour * 60 + t.minute


def _from_minutes(total: int) -> time | None:
    if not 0 <= total < 24 * 60:
        return None  # moving across midnight is not a same-day trip change
    return time(total // 60, total % 60)


# ---------------------------------------------------------------- time


def resolve_time(
    text: str | None,
    *,
    trip_type: str | None = None,
    current_time: time | None = None,
    window_minutes: int = 180,
) -> TimeResolution:
    if not text or not text.strip():
        return UNPARSEABLE_TIME
    t = _normalise(text)

    if re.search(r"\b(noon|midday)\b", t):
        return TimeResolution("exact", time(12, 0))
    if re.search(r"\bmidnight\b", t):
        return TimeResolution("exact", time(0, 0))

    relative = _relative(t, current_time)
    if relative is not None:
        return relative

    anchor = current_time or (ANCHORS.get(trip_type) if trip_type else None)
    meridiem = _meridiem_hint(t)

    phrase = _phrase_time(t)
    if phrase is not None:
        hour, minute = phrase
        return _pick_reading(hour, minute, meridiem, anchor, window_minutes)

    return _numeric(t, meridiem, anchor, window_minutes)


_REL = re.compile(
    r"(?P<plus>\+|plus\s)?\s*(?P<amount>half an|an?|\d+(?:\.\d+)?)\s*"
    r"(?P<unit>hours?|hr|h|minutes?|mins?|m)\b\s*(?P<dir>later|earlier|before|after|late|early)?"
)


def _relative(t: str, current: time | None) -> TimeResolution | None:
    match = _REL.search(t)
    if not match or not (match["plus"] or match["dir"]):
        return None
    if current is None:
        return UNPARSEABLE_TIME
    amount_text = match["amount"]
    amount = 0.5 if amount_text == "half an" else 1.0 if amount_text in ("a", "an") else float(amount_text)
    minutes = int(round(amount * 60)) if match["unit"].startswith("h") else int(round(amount))
    sign = -1 if match["dir"] in ("earlier", "before", "early") else 1
    value = _from_minutes(_minutes(current) + sign * minutes)
    return TimeResolution("exact", value) if value else UNPARSEABLE_TIME


def _meridiem_hint(t: str) -> str | None:
    if re.search(r"\b(tonight|evening|night|raat|shaam)\b", t):
        return "pm"
    if re.search(r"\bafternoon\b", t):
        return "pm"
    if re.search(r"\b(morning|subah)\b", t):
        return "am"
    return None


def _phrase_time(t: str) -> tuple[int, int] | None:
    """Spoken forms: 'half past 8', 'quarter to 9', and Hinglish 'saade 8', 'paune 9', '9 baje'."""
    patterns: list[tuple[str, int, int]] = [
        (r"\bhalf past (\d{1,2})\b", 0, 30),
        (r"\bquarter past (\d{1,2})\b", 0, 15),
        (r"\bquarter to (\d{1,2})\b", -1, 45),
        (r"\bsaa?de (\d{1,2})\b", 0, 30),
        (r"\bsawa (\d{1,2})\b|\bsava (\d{1,2})\b", 0, 15),
        (r"\bpaune (\d{1,2})\b", -1, 45),
        (r"\b(\d{1,2}) baje\b", 0, 0),
    ]
    for pattern, hour_shift, minute in patterns:
        match = re.search(pattern, t)
        if match:
            hour = int(next(g for g in match.groups() if g)) + hour_shift
            if 0 <= hour <= 23:
                return hour, minute
    return None


_NUMERIC = re.compile(r"(?<![\d:.])(\d{1,2})(?:[:.](\d{2}))?(?!\d)\s*(am|pm)?|(?<!\d)(\d{1,2})(\d{2})\s*(am|pm)")


def _numeric(t: str, meridiem: str | None, anchor: time | None, window: int) -> TimeResolution:
    readings: list[TimeResolution] = []
    for match in _NUMERIC.finditer(t):
        if match.group(4):  # compact '830pm'
            hour_s, minute_s, suffix = match.group(4), match.group(5), match.group(6)
        else:
            hour_s, minute_s, suffix = match.group(1), match.group(2), match.group(3)
        hour, minute = int(hour_s), int(minute_s or 0)
        if hour > 23 or minute > 59:
            continue
        explicit_24h = hour >= 13 or hour == 0 or (hour_s.startswith("0") and len(hour_s) == 2)
        if suffix:
            if hour == 0 or hour > 12:
                continue
            readings.append(TimeResolution("exact", _apply_meridiem(hour, minute, suffix)))
        elif explicit_24h:
            readings.append(TimeResolution("exact", time(hour, minute)))
        else:
            readings.append(_pick_reading(hour, minute, meridiem, anchor, window))

    if not readings:
        return UNPARSEABLE_TIME
    if len(readings) == 1:
        return readings[0]
    # "between 8 and 9" etc.: several times in one span is a question, not an answer.
    candidates = tuple(dict.fromkeys(c for r in readings for c in ((r.value,) if r.value else r.candidates)))
    return TimeResolution("ambiguous", None, candidates)


def _apply_meridiem(hour: int, minute: int, suffix: str) -> time:
    hour = hour % 12 + (12 if suffix == "pm" else 0)
    return time(hour, minute)


def _pick_reading(hour: int, minute: int, meridiem: str | None, anchor: time | None, window: int) -> TimeResolution:
    if hour > 12:
        return TimeResolution("exact", time(hour, minute))
    if meridiem:
        return TimeResolution("exact", _apply_meridiem(hour, minute, meridiem))
    am, pm = _apply_meridiem(hour, minute, "am"), _apply_meridiem(hour, minute, "pm")
    if anchor is None:
        return TimeResolution("ambiguous", None, (am, pm))
    inside = [c for c in (am, pm) if abs(_minutes(c) - _minutes(anchor)) <= window]
    if len(inside) == 1:
        return TimeResolution("exact", inside[0])
    return TimeResolution("ambiguous", None, (am, pm))


# ---------------------------------------------------------------- date

_WEEKDAYS = {
    name: i
    for i, names in enumerate(
        [
            ("monday", "mon"),
            ("tuesday", "tue", "tues"),
            ("wednesday", "wed"),
            ("thursday", "thu", "thurs"),
            ("friday", "fri"),
            ("saturday", "sat"),
            ("sunday", "sun"),
        ]
    )
    for name in names
}
_MONTHS = {
    name: i + 1
    for i, names in enumerate(
        [
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sep", "sept"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        ]
    )
    for name in names
}


def resolve_date(text: str | None, *, today: date) -> DateResolution:
    """Explicit date words only. A missing date is decided by the caller, using the roster."""
    if not text or not text.strip():
        return UNPARSEABLE_DATE
    t = _normalise(text)

    if re.search(r"\b(day after tomorrow|parso|parson)\b", t):
        return DateResolution("exact", today + timedelta(days=2))
    if re.search(r"\b(tomorrow|tmrw|tmr|tomorow|tommorow|tommorrow|kal)\b", t):
        return DateResolution("exact", today + timedelta(days=1))
    if re.search(r"\b(today|tonight|aaj|this evening|this morning|now)\b", t):
        return DateResolution("exact", today)

    iso = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", t)
    if iso:
        try:
            return DateResolution("exact", date(int(iso[1]), int(iso[2]), int(iso[3])))
        except ValueError:
            return UNPARSEABLE_DATE

    for word, weekday in _WEEKDAYS.items():
        match = re.search(rf"\b(next )?{word}\b", t)
        if match:
            ahead = (weekday - today.weekday()) % 7
            if match.group(1) and ahead == 0:
                ahead = 7
            return DateResolution("exact", today + timedelta(days=ahead))

    return _day_of_month(t, today)


def _day_of_month(t: str, today: date) -> DateResolution:
    month = None
    for word, number in _MONTHS.items():
        if re.search(rf"\b{word}\b", t):
            month = number
            break
    match = re.search(r"\b(\d{1,2})(st|nd|rd|th)?\b", t)
    if not match or not (match.group(2) or month or re.search(r"\bon the\b", t)):
        return UNPARSEABLE_DATE
    day = int(match.group(1))
    explicit_month = month is not None
    year, month = today.year, month or today.month
    try:
        candidate = date(year, month, day)
    except ValueError:
        return UNPARSEABLE_DATE
    if candidate < today and explicit_month:
        try:
            return DateResolution("exact", date(year + 1, month, day))
        except ValueError:
            return UNPARSEABLE_DATE
    if candidate < today:
        # "the 5th" when it is already the 20th means next month.
        next_month = month % 12 + 1
        next_year = year + (1 if next_month == 1 else 0)
        try:
            candidate = date(next_year, next_month, day)
        except ValueError:
            return UNPARSEABLE_DATE
    return DateResolution("exact", candidate)
