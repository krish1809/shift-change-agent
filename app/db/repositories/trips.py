"""Trip queries. A trip id that belongs to another company is simply not found."""

from __future__ import annotations

from datetime import date

from psycopg import AsyncConnection

from app.domain.models import Trip, TripType

_SELECT = """
SELECT t.*, r.zones AS route_zones
  FROM trips t
  JOIN routes r ON r.company_id = t.company_id AND r.id = t.route_id
"""


async def get_trip(conn: AsyncConnection, company_id: str, trip_id: str) -> Trip | None:
    row = await (
        await conn.execute(_SELECT + " WHERE t.company_id = %s AND t.id = %s", (company_id, trip_id))
    ).fetchone()
    return Trip(**row) if row else None


async def list_trips(conn: AsyncConnection, company_id: str, *, trip_type: TripType, service_date: date) -> list[Trip]:
    rows = await (
        await conn.execute(
            _SELECT
            + " WHERE t.company_id = %s AND t.trip_type = %s AND t.service_date = %s"
            + " ORDER BY t.scheduled_at, t.id",
            (company_id, trip_type, service_date),
        )
    ).fetchall()
    return [Trip(**r) for r in rows]
