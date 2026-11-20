"""Runtime context passed to every graph node. Built server-side per request from the verified token."""

from __future__ import annotations

from dataclasses import dataclass, field

from psycopg_pool import AsyncConnectionPool

from app.auth.jwt import AuthContext
from app.domain.clock import Clock
from app.domain.models import Company, Employee, Policy
from app.llm.base import BudgetedLLM
from app.settings import Settings
from app.tools.faults import FaultInjector


@dataclass
class RunContext:
    auth: AuthContext
    thread_id: str
    clock: Clock
    pool: AsyncConnectionPool
    llm: BudgetedLLM  # carries this turn's call budget
    settings: Settings
    faults: FaultInjector
    company: Company
    policy: Policy
    employee: Employee
    correlation_id: str = "-"
    deadline: float | None = None  # loop-time by which the turn must finish
    tool_calls: list[str] = field(default_factory=list)

    @property
    def llm_calls(self) -> int:
        return self.llm.budget.calls
