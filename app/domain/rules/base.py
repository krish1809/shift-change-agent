"""Shared types for the rules engine. Rules are pure functions of a RuleContext."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.domain.formatting import fmt_minutes, fmt_time
from app.domain.models import ChangeType, Company, Employee, Policy, RosterEntry, Trip, Violation


@dataclass(frozen=True)
class RuleContext:
    company: Company
    policy: Policy
    employee: Employee
    now: datetime
    change_type: ChangeType
    entry: RosterEntry
    current_trip: Trip | None
    target_trip: Trip | None
    changes_today: int = 0
    requested_label: str | None = None  # e.g. "7:30 PM" when the asked-for time had no trip

    def violation(self, code: str, *, escalatable: bool, **params: object) -> Violation:
        """Violation params only ever carry THIS company's values (name, numbers, phone)."""
        base = {"company_name": self.company.name, "desk_phone": self.company.desk_phone}
        return Violation(code=code, escalatable=escalatable, params={**base, **params})

    def label(self, at: datetime) -> str:
        return fmt_time(at, self.company.tz)

    def minutes_label(self, minutes: int) -> str:
        return fmt_minutes(minutes)


@dataclass(frozen=True)
class RuleResult:
    violations: list[Violation] = field(default_factory=list)
    already_on_trip: bool = False

    @property
    def allowed(self) -> bool:
        return not self.violations and not self.already_on_trip

    @property
    def escalatable(self) -> bool:
        """Escalate only if there is something to escalate and the manager may override all of it."""
        return bool(self.violations) and all(v.escalatable for v in self.violations)

    @property
    def codes(self) -> list[str]:
        return [v.code for v in self.violations]
