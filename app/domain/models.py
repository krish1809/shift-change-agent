"""Typed domain objects. Repositories return these; rules and templates consume them."""
from __future__ import annotations

from datetime import date, datetime, time
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

TripType = Literal["pickup", "drop"]
ChangeType = Literal["reschedule", "cancel"]
Role = Literal["employee", "manager"]


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Company(Frozen):
    id: str
    name: str
    timezone: str
    desk_phone: str

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


class Policy(Frozen):
    company_id: str
    cutoff_minutes_pickup: int
    cutoff_minutes_drop: int
    cutoff_escalatable: bool
    max_seats_per_cab: int
    change_window_minutes: int
    window_escalatable: bool
    max_changes_per_day: int | None
    daily_limit_escalatable: bool
    approval_timeout_minutes: int
    alternatives_count: int
    alternatives_window_minutes: int
    version: int = 1

    def cutoff_minutes(self, trip_type: TripType) -> int:
        return self.cutoff_minutes_pickup if trip_type == "pickup" else self.cutoff_minutes_drop


class CompanyContext(Frozen):
    """A company together with its policy: everything templates may quote to that company's employees."""

    company: Company
    policy: Policy


class Route(Frozen):
    company_id: str
    id: str
    name: str
    zones: list[str]


class Employee(Frozen):
    company_id: str
    id: str
    name: str
    zone: str
    role: Role

    @property
    def first_name(self) -> str:
        return self.name.split()[0]


class Trip(Frozen):
    company_id: str
    id: str
    route_id: str
    trip_type: TripType
    service_date: date
    scheduled_at: datetime
    vehicle_seats: int
    effective_capacity: int
    seats_taken: int
    cab_label: str
    route_zones: list[str] = Field(default_factory=list)

    @property
    def seats_left(self) -> int:
        return max(self.effective_capacity - self.seats_taken, 0)

    def local_time(self, tz: ZoneInfo) -> time:
        return self.scheduled_at.astimezone(tz).time().replace(tzinfo=None)


class RosterEntry(Frozen):
    company_id: str
    id: str
    employee_id: str
    service_date: date
    trip_type: TripType
    trip_id: str | None
    status: Literal["scheduled", "cancelled"]
    version: int
    trip: Trip | None = None


class Violation(Frozen):
    code: str
    escalatable: bool
    params: dict[str, Any] = Field(default_factory=dict)


class ShiftRequest(Frozen):
    id: str
    company_id: str
    employee_id: str
    roster_entry_id: str
    change_type: ChangeType
    from_trip_id: str | None
    to_trip_id: str | None
    status: str
    violations: list[dict[str, Any]]
    idempotency_key: str
    thread_id: str
    roster_version: int
    decision: str | None
    decision_note: str | None
    decided_by: str | None
    decided_at: datetime | None
    resumed_at: datetime | None
    created_at: datetime
    updated_at: datetime
