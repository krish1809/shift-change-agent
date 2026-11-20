"""POST /chat and GET /chat/{session_id}/messages.

Identity comes from the token only: the body has no company/employee fields (extra=forbid), and the
thread id is built server-side as company:employee:session so another user's session id is an empty thread.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.agent.runner import ThreadBusy, run_employee_turn
from app.api.errors import FRIENDLY, AppError, envelope
from app.auth.deps import require_employee
from app.auth.jwt import AuthContext
from app.logging import get_correlation_id

router = APIRouter(prefix="/chat", tags=["chat"])


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: uuid.UUID | None = None
    message: str = Field(min_length=1, max_length=1000)

    @field_validator("message")
    @classmethod
    def _clean(cls, value: str) -> str:
        # NUL and other control characters can't be stored in the JSONB checkpoint and are never meaningful.
        cleaned = "".join(ch for ch in value if ch in "\n\t" or ch.isprintable()).strip()
        if not cleaned:
            raise ValueError("blank message")
        return cleaned


@router.post("")
async def chat(body: ChatRequest, request: Request, auth: AuthContext = Depends(require_employee)) -> JSONResponse:
    session_id = str(body.session_id or uuid.uuid4())
    try:
        outcome = await run_employee_turn(request.app, auth, session_id, body.message)
    except ThreadBusy as exc:
        raise AppError(429, FRIENDLY[429], session_id=session_id) from exc
    payload: dict[str, Any] = {
        "reply": outcome.reply,
        "status": outcome.status,
        "session_id": session_id,
        "request": outcome.request,
        "options": outcome.options,
        "meta": {
            "correlation_id": get_correlation_id(),
            "llm_calls": outcome.llm_calls,
            "latency_ms": outcome.latency_ms,
            "outcome": outcome.last_outcome,  # machine-readable result status (used by the eval runner)
            "tools": outcome.tools_called or [],
        },
    }
    return JSONResponse(payload, status_code=outcome.http_status)


@router.get("/{session_id}/messages")
async def messages(
    session_id: uuid.UUID, request: Request, auth: AuthContext = Depends(require_employee)
) -> JSONResponse:
    config = {"configurable": {"thread_id": auth.thread_id(str(session_id))}}
    snapshot = await request.app.state.graph.aget_state(config)
    stored = (snapshot.values or {}).get("messages", []) if snapshot else []
    return envelope_list(str(session_id), stored)


def envelope_list(session_id: str, stored: list[dict[str, Any]]) -> JSONResponse:
    return JSONResponse(
        {
            "session_id": session_id,
            "messages": [{"role": m["role"], "content": m["content"], "at": m.get("at")} for m in stored],
            "meta": {"correlation_id": get_correlation_id()},
        }
    )


__all__ = ["envelope", "router"]
