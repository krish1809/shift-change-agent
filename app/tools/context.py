"""Everything a tool handler may use. Identity is the verified AuthContext; there is no other source."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from psycopg_pool import AsyncConnectionPool

from app.auth.jwt import AuthContext
from app.domain.clock import Clock
from app.domain.models import Company, Employee, Policy
from app.tools.faults import FaultInjector


@dataclass
class ToolContext:
    auth: AuthContext
    pool: AsyncConnectionPool
    clock: Clock
    company: Company
    policy: Policy
    employee: Employee
    thread_id: str
    faults: FaultInjector
    draft: dict[str, Any] | None = None
    offered_options: list[dict[str, Any]] = field(default_factory=list)
    pending_offer: dict[str, Any] | None = None
    pending_request_id: str | None = None
    recent_user_texts: list[str] = field(default_factory=list)
    booking_line: str | None = None
    ungrounded: set[str] = field(default_factory=set)  # spans the gate could not find in the user's words
    tool_timeout_s: float = 1.0
    # Written by mutating handlers before they write, so a timed-out write can be reconciled.
    scratch: dict[str, Any] = field(default_factory=dict)

    @property
    def tz(self) -> ZoneInfo:
        return self.company.tz

    def now(self) -> datetime:
        return self.clock.now().astimezone(self.tz)

    def today(self) -> date:
        return self.now().date()

    @property
    def last_user_text(self) -> str:
        return self.recent_user_texts[-1] if self.recent_user_texts else ""
