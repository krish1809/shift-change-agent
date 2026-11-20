"""Append-only audit trail: who asked for what, and who decided."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

ActorType = Literal["employee", "manager", "agent", "system"]


async def add_audit(
    conn: AsyncConnection,
    company_id: str,
    *,
    at: datetime,
    actor_type: ActorType,
    actor_id: str,
    action: str,
    request_id: str | None = None,
    thread_id: str | None = None,
    correlation_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    await conn.execute(
        """INSERT INTO audit_log (at, company_id, actor_type, actor_id, action, request_id, thread_id,
                                  correlation_id, payload)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (
            at,
            company_id,
            actor_type,
            actor_id,
            action,
            request_id,
            thread_id,
            correlation_id,
            Jsonb(payload or {}),
        ),
    )


async def list_audit(conn: AsyncConnection, company_id: str, *, request_id: str | None = None) -> list[dict]:
    sql = "SELECT * FROM audit_log WHERE company_id = %s"
    params: list[Any] = [company_id]
    if request_id is not None:
        sql += " AND request_id = %s"
        params.append(request_id)
    rows = await (await conn.execute(sql + " ORDER BY id", params)).fetchall()
    return list(rows)
