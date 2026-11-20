"""FastAPI dependencies that turn a bearer token into a verified AuthContext."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from fastapi import Depends, Request

from app.api.errors import AppError
from app.auth.jwt import AuthContext, InvalidToken, verify_token
from app.db.repositories import employees

log = logging.getLogger(__name__)

UNAUTHORIZED = "Please sign in again: your session token is missing, invalid or expired."


async def get_auth_context(request: Request) -> AuthContext:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise AppError(401, UNAUTHORIZED)
    state = request.app.state
    try:
        auth = verify_token(token.strip(), secret=state.settings.jwt_secret, now=state.clock.now())
    except InvalidToken as exc:
        log.info("token rejected", extra={"fields": {"reason": str(exc)}})
        raise AppError(401, UNAUTHORIZED) from exc

    # A validly signed token for someone who no longer exists (or changed role) is not accepted.
    async with state.pool.connection() as conn:
        employee = await employees.get_employee(conn, auth.company_id, auth.employee_id)
    if employee is None or employee.role != auth.role:
        raise AppError(401, UNAUTHORIZED)
    return auth


def require_role(role: str) -> Callable[..., Awaitable[AuthContext]]:
    async def dependency(auth: AuthContext = Depends(get_auth_context)) -> AuthContext:
        if auth.role != role:
            raise AppError(403, "This action is only available to a " + role + ".")
        return auth

    return dependency


require_employee = require_role("employee")
require_manager = require_role("manager")
