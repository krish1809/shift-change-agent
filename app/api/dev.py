"""Dev-only helpers for the demo: a mock JWT issuer, fault injection and a movable clock.

Every route here returns 404 unless ENV=dev.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from app.api.errors import AppError
from app.auth.jwt import issue_token
from app.db.repositories import employees

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
