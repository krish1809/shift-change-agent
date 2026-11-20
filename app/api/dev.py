"""Dev-only helpers for the demo: a mock JWT issuer, fault injection and a movable clock.

Every route here returns 404 unless ENV=dev.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from app.api.errors import AppError
from app.auth.jwt import issue_token
from app.db.repositories import employees
from app.db.seed import reseed
from app.domain.clock import DEFAULT_TZ, parse_aware

router = APIRouter(prefix="/dev", tags=["dev"])


def dev_only(request: Request) -> None:
    if request.app.state.settings.env != "dev":
        raise AppError(404, "I couldn't find that.")


class TokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company_id: str = Field(min_length=1, max_length=64)
    employee_id: str = Field(min_length=1, max_length=64)


@router.post("/token", dependencies=[Depends(dev_only)])
async def dev_token(body: TokenRequest, request: Request) -> dict:
    state = request.app.state
    async with state.pool.connection() as conn:
        employee = await employees.get_employee(conn, body.company_id, body.employee_id)
    if employee is None:
        raise AppError(404, "No such employee in that company.")
    # The role is looked up, never taken from the request.
    token = issue_token(
        employee.company_id,
        employee.id,
        employee.role,
        secret=state.settings.jwt_secret,
        now=state.clock.now(),
        ttl_minutes=state.settings.jwt_ttl_minutes,
    )
    return {"token": token, "role": employee.role, "name": employee.name}


@router.get("/people", dependencies=[Depends(dev_only)])
async def dev_people(request: Request) -> dict:
    """Seeded people for the demo UI's dropdowns (dev only, deliberately cross-company)."""
    async with request.app.state.pool.connection() as conn:
        rows = await (
            await conn.execute(
                """SELECT e.company_id, c.name AS company_name, e.id, e.name, e.role
                     FROM employees e JOIN companies c ON c.id = e.company_id
                    ORDER BY e.company_id, e.role, e.id"""
            )
        ).fetchall()
    return {"people": rows}


class ClockBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    now: str | None = Field(default=None, max_length=40, description="ISO datetime, or null to use real time")


@router.get("/clock", dependencies=[Depends(dev_only)])
async def dev_clock_now(request: Request) -> dict:
    clock = request.app.state.clock
    return {"now": clock.now().isoformat(), "overridden": bool(getattr(clock, "overridden", False))}


@router.post("/reseed", dependencies=[Depends(dev_only)])
async def dev_reseed(request: Request) -> dict:
    """Reset the demo: reload seed data with D0 = today (by the demo clock) and wipe all conversations."""
    state = request.app.state
    base_date = state.clock.now().astimezone(DEFAULT_TZ).date()
    async with state.pool.connection() as conn:
        await reseed(conn, base_date)
    state.faults.clear()
    return {"base_date": base_date.isoformat()}


@router.post("/clock", dependencies=[Depends(dev_only)])
async def dev_clock(body: ClockBody, request: Request) -> dict:
    """Pretend it is 5:30 PM so cut-off scenarios can be demonstrated at any time of day."""
    clock = request.app.state.clock
    try:
        moment = parse_aware(body.now) if body.now else None
    except ValueError as exc:
        raise AppError(422, "Use an ISO time such as 2026-03-02T17:30:00+05:30.") from exc
    clock.set(moment)
    return {"now": clock.now().isoformat()}


class FaultBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool: str = Field(min_length=1, max_length=40)
    mode: str | None = Field(
        default=None, max_length=20, description="timeout | error | fail_once | slow:<ms>; null clears"
    )


@router.post("/faults", dependencies=[Depends(dev_only)])
async def dev_faults(body: FaultBody, request: Request) -> dict:
    faults = request.app.state.faults
    try:
        if body.mode is None:
            faults.clear(body.tool)
        else:
            faults.arm(body.tool, body.mode)
    except ValueError as exc:
        raise AppError(422, "Unknown fault mode. Use timeout, error, fail_once or slow:<ms>.") from exc
    return {"faults": faults.active()}
