"""Shift-request queries. Every lookup is scoped by company (and by employee for employee callers)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from psycopg import AsyncConnection

from app.domain.models import ShiftRequest

FINAL_APPLIED = ("APPLIED", "APPROVED_APPLIED")


async def get_request(
    conn: AsyncConnection, company_id: str, request_id: str, *, employee_id: str | None = None
) -> ShiftRequest | None:
    sql = "SELECT * FROM shift_requests WHERE company_id = %s AND id = %s"
    params: list[Any] = [company_id, request_id]
    if employee_id is not None:
        sql += " AND employee_id = %s"
        params.append(employee_id)
    row = await (await conn.execute(sql, params)).fetchone()
    return ShiftRequest(**row) if row else None


async def list_requests(
    conn: AsyncConnection,
    company_id: str,
    *,
    employee_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[ShiftRequest]:
    sql = "SELECT * FROM shift_requests WHERE company_id = %s"
    params: list[Any] = [company_id]
    if employee_id is not None:
        sql += " AND employee_id = %s"
        params.append(employee_id)
    if status is not None:
        sql += " AND status = %s"
        params.append(status)
    sql += " ORDER BY created_at DESC, id LIMIT %s"
    params.append(limit)
    rows = await (await conn.execute(sql, params)).fetchall()
    return [ShiftRequest(**r) for r in rows]


async def latest_open_request(conn: AsyncConnection, company_id: str, employee_id: str) -> ShiftRequest | None:
    open_ = await list_requests(conn, company_id, employee_id=employee_id, status="PENDING_APPROVAL", limit=1)
    return open_[0] if open_ else None


async def find_by_idempotency_key(
    conn: AsyncConnection, company_id: str, employee_id: str, key: str
) -> ShiftRequest | None:
    row = await (
        await conn.execute(
            "SELECT * FROM shift_requests WHERE company_id = %s AND employee_id = %s AND idempotency_key = %s",
            (company_id, employee_id, key),
        )
    ).fetchone()
    return ShiftRequest(**row) if row else None


async def count_applied_changes(
    conn: AsyncConnection, company_id: str, employee_id: str, *, since: datetime, until: datetime
) -> int:
    """Changes that actually took effect in [since, until): used by the daily-limit rule."""
    row = await (
        await conn.execute(
            """SELECT count(*) AS n FROM shift_requests
                WHERE company_id = %s AND employee_id = %s AND status = ANY(%s)
                  AND updated_at >= %s AND updated_at < %s""",
            (company_id, employee_id, list(FINAL_APPLIED), since, until),
        )
    ).fetchone()
    return int(row["n"]) if row else 0


async def record_decision(
    conn: AsyncConnection,
    company_id: str,
    request_id: str,
    *,
    decision: str,
    decided_by: str,
    note: str | None,
    at: datetime,
) -> ShiftRequest | None:
    """The decision outbox: written FIRST, exactly once. None = not pending / already decided / not visible."""
    row = await (
        await conn.execute(
            """UPDATE shift_requests
                  SET decision = %s, decided_by = %s, decision_note = %s, decided_at = %s, updated_at = %s
                WHERE company_id = %s AND id = %s AND status = 'PENDING_APPROVAL' AND decision IS NULL
            RETURNING *""",
            (decision, decided_by, note, at, at, company_id, request_id),
        )
    ).fetchone()
    return ShiftRequest(**row) if row else None


async def finalize_status(
    conn: AsyncConnection, company_id: str, request_id: str, *, status: str, at: datetime
) -> bool:
    """PENDING_APPROVAL -> final status, once. Re-running after a crash is a harmless no-op (False)."""
    row = await (
        await conn.execute(
            """UPDATE shift_requests SET status = %s, updated_at = %s, resumed_at = %s
                WHERE company_id = %s AND id = %s AND status = 'PENDING_APPROVAL'
            RETURNING id""",
            (status, at, at, company_id, request_id),
        )
    ).fetchone()
    return row is not None
