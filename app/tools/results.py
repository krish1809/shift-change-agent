"""ToolResult: the typed outcome every tool returns. Replies are rendered from it, never written by the LLM."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# Statuses that mean the database was changed (or a request row was created) in this turn.
MUTATED_STATUSES = frozenset({"APPLIED", "OUTCOME_RECONCILED", "PENDING_APPROVAL", "WITHDRAWN"})

# Envelope status for each ToolResult status (SPEC §8).
ENVELOPE_STATUS = {
    "NEEDS_CLARIFICATION": "needs_clarification",
    "NEEDS_APPROVAL": "needs_clarification",
    "PENDING_APPROVAL": "pending_approval",
    "REJECTED": "rejected",
    "SEAT_TAKEN": "rejected",
    "APPROVAL_REJECTED": "rejected",
    "APPROVED_BUT_UNAVAILABLE": "rejected",
    "EXPIRED": "rejected",
    "TOOL_UNAVAILABLE": "error",
    "ERROR": "error",
}


class ToolResult(BaseModel):
    status: str
    template: str
    params: dict[str, Any] = Field(default_factory=dict)
    options: list[dict[str, Any]] | None = None
    request: dict[str, Any] | None = None
    # Only the keys present are written to graph state (draft, offered_options, pending_offer,
    # pending_request_id). Absent = leave unchanged.
    updates: dict[str, Any] = Field(default_factory=dict)

    @property
    def mutated(self) -> bool:
        return self.status in MUTATED_STATUSES

    @property
    def envelope_status(self) -> str:
        return ENVELOPE_STATUS.get(self.status, "ok")
