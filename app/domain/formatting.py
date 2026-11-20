"""Human formatting shared by rules (violation params) and templates."""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo


def fmt_time(value: datetime | time, tz: ZoneInfo | None = None) -> str:
    """'8:30 PM' (company timezone when given an aware datetime)."""
    if isinstance(value, datetime):
        value = value.astimezone(tz).time() if tz else value.time()
    hour = value.hour % 12 or 12
    return f"{hour}:{value.minute:02d} {'AM' if value.hour < 12 else 'PM'}"


def fmt_minutes(minutes: int) -> str:
    """120 -> '2 hours', 60 -> '60 minutes', 90 -> '90 minutes'."""
    if minutes >= 120 and minutes % 60 == 0:
        return f"{minutes // 60} hours"
    return f"{minutes} minutes"


def fmt_day(day: date, today: date) -> str:
    delta = (day - today).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    return day.strftime("%a %d %b").replace(" 0", " ")
