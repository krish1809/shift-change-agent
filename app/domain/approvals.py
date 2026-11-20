"""System-level approval queries for the sweeper.

These are the ONLY queries that look across companies, because the sweeper is a system actor that
must find every stuck request. Each returned row is then handled with its own company_id: the resume
path rebuilds identity from the stored row and every action after that is tenant-scoped.
"""

from __future__ import annotations

from datetime import datetime

from psycopg import AsyncConnection

from app.domain.models import ShiftRequest


async def decided_not_resumed(conn: AsyncConnection) -> list[ShiftRequest]:
    """A manager's decision is recorded, but the paused conversation was never resumed (crash window)."""
    rows = await (
        await conn.execute(
            """SELECT * FROM shift_requests
                WHERE status = 'PENDING_APPROVAL' AND decision IS NOT NULL AND resumed_at IS NULL
                ORDER BY decided_at"""
        )
    ).fetchall()
    return [ShiftRequest(**r) for r in rows]


async def overdue_pending(conn: AsyncConnection, now: datetime) -> list[ShiftRequest]:
    """Open requests whose approval window has closed: earlier trip time minus the company's timeout."""
    rows = await (
        await conn.execute(
            """SELECT r.* FROM shift_requests r
                 JOIN company_policies p ON p.company_id = r.company_id
                 JOIN trips f ON f.company_id = r.company_id AND f.id = r.from_trip_id
                 LEFT JOIN trips t ON t.company_id = r.company_id AND t.id = r.to_trip_id
                WHERE r.status = 'PENDING_APPROVAL' AND r.decision IS NULL
                  AND %s > LEAST(f.scheduled_at, COALESCE(t.scheduled_at, f.scheduled_at))
                           - make_interval(mins => p.approval_timeout_minutes)""",
            (now,),
        )
    ).fetchall()
    return [ShiftRequest(**r) for r in rows]


async def open_pending(conn: AsyncConnection) -> list[ShiftRequest]:
    rows = await (
        await conn.execute(
            "SELECT * FROM shift_requests WHERE status = 'PENDING_APPROVAL' AND decision IS NULL ORDER BY created_at"
        )
    ).fetchall()
    return [ShiftRequest(**r) for r in rows]
