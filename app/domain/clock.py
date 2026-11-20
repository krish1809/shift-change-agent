"""The only place in the codebase that reads the wall clock.

Everything time-dependent (cut-offs, expiry, created_at) asks a `Clock`, so tests can freeze time
and the demo can move it ("pretend it is 5:30 PM") without touching business code.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

DEFAULT_TZ = ZoneInfo("Asia/Kolkata")


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(DEFAULT_TZ)


class FrozenClock:
    """Test clock: stands still until told to move."""

    def __init__(self, at: datetime) -> None:
        _require_aware(at)
        self._now = at

    def now(self) -> datetime:
        return self._now

    def set(self, at: datetime) -> None:
        _require_aware(at)
        self._now = at

    def advance(self, **delta: float) -> None:
        self._now = self._now + timedelta(**delta)


class OverrideClock:
    """Demo clock: real time shifted by an offset, so time keeps flowing from the chosen moment."""

    def __init__(self, base: Clock | None = None) -> None:
        self._base = base or SystemClock()
        self._offset = timedelta(0)

    def now(self) -> datetime:
        return self._base.now() + self._offset

    def set(self, at: datetime | None) -> None:
        if at is None:
            self._offset = timedelta(0)
            return
        _require_aware(at)
        self._offset = at - self._base.now()

    @property
    def overridden(self) -> bool:
        return self._offset != timedelta(0)


def parse_aware(text: str, tz: ZoneInfo = DEFAULT_TZ) -> datetime:
    """Parse an ISO datetime; naive values are interpreted in the company timezone."""
    value = datetime.fromisoformat(text)
    return value if value.tzinfo else value.replace(tzinfo=tz)


def _require_aware(at: datetime) -> None:
    if at.tzinfo is None:
        raise ValueError("clock times must be timezone-aware")
