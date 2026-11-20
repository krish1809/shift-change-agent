"""Conversation state persisted in the Postgres checkpoint.

Identity is NOT here: company/employee come from the verified JWT and travel as LangGraph runtime
context (RunContext), so they are never in state, messages, tool args or the checkpoint.
Everything stored is plain JSON-able data.
"""

from __future__ import annotations

from typing import Annotated, Any, TypedDict

MAX_STORED_MESSAGES = 60


def append_capped(left: list[dict[str, Any]] | None, right: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Message reducer: append, but keep the stored thread bounded."""
    return ((left or []) + (right or []))[-MAX_STORED_MESSAGES:]


class ConversationState(TypedDict, total=False):
    # ---- survives across turns
    messages: Annotated[list[dict[str, Any]], append_capped]  # {"role": "user"|"assistant", "content", "at"}
    draft: dict[str, Any] | None  # partially-specified change carried across follow-ups
    offered_options: list[dict[str, Any]]  # numbered alternatives from the last reply
    pending_offer: dict[str, Any] | None  # escalate? / did you mean?
    pending_request_id: str | None  # set while paused for manager approval
    last_outcome: str | None

    # ---- per turn (reset by `ingest`)
    user_text: str
    mode: str  # "normal" | "pending"
    roster_lines: list[str]
    resume: dict[str, Any] | None  # what resumed the paused graph: manager decision / expiry / employee message
    llm_response: dict[str, Any] | None
    degraded: bool
    repair_note: str | None
    repairs: int
    call: dict[str, Any] | None
    result: dict[str, Any] | None
    fail_reason: str | None
    tools_called: list[str]
    reply: str | None
    status: str | None
    request: dict[str, Any] | None
    options: list[dict[str, Any]] | None
