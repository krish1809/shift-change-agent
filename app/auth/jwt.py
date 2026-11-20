"""JWT issue/verify. The token is the ONLY source of company and employee identity."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

import jwt

ISSUER = "shift-agent-dev"
AUDIENCE = "shift-agent"
ALGORITHM = "HS256"
REQUIRED_CLAIMS = ["iss", "aud", "exp", "iat", "sub", "company_id", "role"]


@dataclass(frozen=True)
class AuthContext:
    company_id: str
    employee_id: str
    role: Literal["employee", "manager"]

    @property
    def thread_prefix(self) -> str:
        return f"{self.company_id}:{self.employee_id}:"

    def thread_id(self, session_id: str) -> str:
        return f"{self.thread_prefix}{session_id}"


class InvalidToken(Exception):
    """Raised for any token problem. The reason is logged, never shown to the client."""


def issue_token(
    company_id: str,
    employee_id: str,
    role: str,
    *,
    secret: str,
    now: datetime,
    ttl_minutes: int = 480,
) -> str:
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": employee_id,
        "company_id": company_id,
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=ttl_minutes)).timestamp()),
    }
    return jwt.encode(claims, secret, algorithm=ALGORITHM)


def verify_token(token: str, *, secret: str, now: datetime) -> AuthContext:
    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=[ALGORITHM],  # explicit allowlist: rejects alg=none and algorithm confusion
            audience=AUDIENCE,
            issuer=ISSUER,
            options={"require": REQUIRED_CLAIMS, "verify_exp": False},
        )
    except jwt.PyJWTError as exc:
        raise InvalidToken(type(exc).__name__) from exc

    # Expiry is checked against the injected clock so tests and the demo clock stay consistent.
    if int(claims["exp"]) <= int(now.timestamp()):
        raise InvalidToken("expired")
    role = claims["role"]
    if role not in ("employee", "manager"):
        raise InvalidToken("bad role")
    if not isinstance(claims["company_id"], str) or not isinstance(claims["sub"], str):
        raise InvalidToken("bad identity claims")
    return AuthContext(company_id=claims["company_id"], employee_id=claims["sub"], role=role)
