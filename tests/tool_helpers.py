"""Build a ToolContext for an employee and run a tool call through gate + executor + template."""

from __future__ import annotations

from typing import Any

from app.agent.templates import render
from app.auth.jwt import AuthContext
from app.db.repositories import companies, employees
from app.tools.context import ToolContext
from app.tools.executor import execute_tool
from app.tools.faults import FaultInjector
from app.tools.gate import GateError, validate_call
from app.tools.results import ToolResult


async def make_ctx(
    pool, clock, company: str, employee: str, *, faults: FaultInjector | None = None, **state: Any
) -> ToolContext:
    async with pool.connection() as conn:
        cc = await companies.get_company_context(conn, company)
        emp = await employees.get_employee(conn, company, employee)
    return ToolContext(
        auth=AuthContext(company, employee, "employee"),
        pool=pool,
        clock=clock,
        company=cc.company,
        policy=cc.policy,
        employee=emp,
        thread_id=f"{company}:{employee}:t1",
        faults=faults or FaultInjector(),
        **state,
    )


async def call(ctx: ToolContext, user_text: str, name: str, **args: Any) -> tuple[ToolResult, str]:
    """One model turn: the user says `user_text`, the model calls `name(**args)`."""
    ctx.recent_user_texts.append(user_text)
    valid = validate_call({"name": name, "args": args}, mode="normal", user_texts=ctx.recent_user_texts)
    assert not isinstance(valid, GateError), valid
    ctx.ungrounded = set(valid.ungrounded)
    result = await execute_tool(ctx, valid.name, valid.args)
    apply_updates(ctx, result)
    return result, render(result.template, result.params)


def apply_updates(ctx: ToolContext, result: ToolResult) -> None:
    """What the graph does with a result: persist draft / options / offer for the next turn."""
    for key in ("draft", "offered_options", "pending_offer", "pending_request_id"):
        if key in result.updates:
            setattr(
                ctx, key, result.updates[key] if result.updates[key] is not None or key != "offered_options" else []
            )
