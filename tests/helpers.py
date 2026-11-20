"""Test helpers: run an app in-process, mint tokens, and refuse any leaked traceback."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import httpx
from fastapi import FastAPI

from app.auth.jwt import issue_token
from app.settings import Settings
from tests.conftest import TEST_DATABASE_URL

LEAK_PATTERNS = re.compile(r"Traceback|File \"|\b\w+(Error|Exception)\b(?!s)|psycopg|langgraph\.|pydantic_core")
SECRET = "test-secret-0123456789-0123456789-0123456789"


def make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "env": "dev",
        "database_url": TEST_DATABASE_URL,
        "seed_on_start": "never",
        "llm_provider": "fake",
        "llm_fallback_provider": "",
        "jwt_secret": SECRET,
        "log_level": "WARNING",
        "sweeper_interval_s": 3600,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


async def _no_leak(response: httpx.Response) -> None:
    await response.aread()
    if LEAK_PATTERNS.search(response.text):
        raise AssertionError(f"response leaks internals: {response.text[:300]}")


@asynccontextmanager
async def running(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Start the app's lifespan (pool, checkpointer, sweeper) and give an in-process client."""
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", event_hooks={"response": [_no_leak]}, timeout=30
        ) as client:
            yield client


def token(company_id: str, employee_id: str, role: str, now: datetime, **kw: Any) -> str:
    return issue_token(company_id, employee_id, role, secret=SECRET, now=now, **kw)


def bearer(company_id: str, employee_id: str, now: datetime, role: str = "employee") -> dict[str, str]:
    return {"Authorization": f"Bearer {token(company_id, employee_id, role, now)}"}


async def chat(
    client: httpx.AsyncClient, company: str, employee: str, text: str, now: datetime, session: str | None = None
):
    body: dict[str, Any] = {"message": text}
    if session:
        body["session_id"] = session
    return await client.post("/chat", json=body, headers=bearer(company, employee, now))


def tool(name: str, **args: Any) -> dict[str, Any]:
    """A scripted model turn: call `name(**args)`."""
    return {"tool": name, "args": args}
