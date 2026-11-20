"""One response envelope for everything, including errors. No exception text ever reaches a client."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.logging import get_correlation_id

log = logging.getLogger(__name__)


class Envelope(BaseModel):
    reply: str
    status: str
    session_id: str | None = None
    request: dict[str, Any] | None = None
    options: list[dict[str, Any]] | None = None
    meta: dict[str, Any]


class AppError(Exception):
    """An expected HTTP-level failure with a friendly reply."""

    def __init__(self, http_status: int, reply: str, *, session_id: str | None = None) -> None:
        super().__init__(reply)
        self.http_status = http_status
        self.reply = reply
        self.session_id = session_id


FRIENDLY = {
    400: "I couldn't read that request. Please send a valid message and try again.",
    401: "Please sign in again: your session token is missing, invalid or expired.",
    403: "You don't have access to this.",
    404: "I couldn't find that.",
    405: "That action isn't supported here.",
    409: "This has already been decided.",
    422: "I couldn't read that request. Please check it and try again.",
    429: "I'm still working on your previous message. Please wait a moment and try again.",
}


def internal_error_reply(correlation_id: str) -> str:
    return f"Something went wrong on our side and nothing was changed. Please try again. (Ref: {correlation_id})"


def envelope(
    reply: str,
    *,
    status: str = "error",
    http_status: int = 200,
    session_id: str | None = None,
    meta: dict[str, Any] | None = None,
) -> JSONResponse:
    body = Envelope(
        reply=reply,
        status=status,
        session_id=session_id,
        meta={"correlation_id": get_correlation_id(), **(meta or {})},
    )
    return JSONResponse(body.model_dump(), status_code=http_status)


def _validation_reply(exc: RequestValidationError) -> str:
    for err in exc.errors():
        loc = [str(p) for p in err.get("loc", ())]
        if err.get("type") == "extra_forbidden":
            return "This request contains fields that aren't allowed. Identity always comes from your sign-in."
        if "message" in loc:
            kind = err.get("type", "")
            if kind in ("string_too_long",):
                return "That message is too long. Please keep it under 1,000 characters."
            if kind in ("string_too_short", "missing", "value_error"):
                return "Please type a message, for example: change my drop today to 8:30 pm."
    return FRIENDLY[422]


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return envelope(exc.reply, http_status=exc.http_status, session_id=exc.session_id)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return envelope(FRIENDLY.get(exc.status_code, "Request failed."), http_status=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return envelope(_validation_reply(exc), http_status=422)

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error")
        return envelope(internal_error_reply(get_correlation_id()), http_status=500)
