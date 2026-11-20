"""name -> handler. Kept separate from the registry so schemas/gate don't import database code."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from app.tools.context import ToolContext
from app.tools.handlers import approvals, reads, shift
from app.tools.results import ToolResult

Handler = Callable[[ToolContext, Any], Awaitable[ToolResult]]

HANDLERS: dict[str, Handler] = {
    "get_my_roster": reads.get_my_roster,
    "check_cab_options": reads.check_cab_options,
    "request_shift_change": shift.request_shift_change,
    "respond_to_offer": shift.respond_to_offer,
    "get_request_status": approvals.get_request_status,
    "withdraw_request": approvals.withdraw_request,
    "get_my_policy": reads.get_my_policy,
}
