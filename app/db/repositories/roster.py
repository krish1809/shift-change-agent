"""Roster queries. Always scoped to (company_id, employee_id): an employee only ever sees their own rows."""

from __future__ import annotations

from datetime import date
from typing import Any

from psycopg import AsyncConnection

from app.domain.models import RosterEntry, Trip, TripType

_SELECT = """
SELECT e.*, t.id AS t_id, t.route_id AS t_route_id, t.trip_type AS t_trip_type,
       t.service_date AS t_service_date, t.scheduled_at AS t_scheduled_at,
       t.vehicle_seats AS t_vehicle_seats, t.effective_capacity AS t_effective_capacity,
       t.seats_taken AS t_seats_taken, t.cab_label AS t_cab_label, r.zones AS t_route_zones
  FROM roster_entries e
  LEFT JOIN trips t ON t.company_id = e.company_id AND t.id = e.trip_id
  LEFT JOIN routes r ON r.company_id = t.company_id AND r.id = t.route_id
"""


def _to_entry(row: dict[str, Any]) -> RosterEntry:
    trip = None
    if row["t_id"]:
        trip = Trip(
            company_id=row["company_id"],
            **{k[2:]: v for k, v in row.items() if k.startswith("t_")},
        )
    fields = {k: v for k, v in row.items() if not k.startswith("t_")}
    return RosterEntry(**fields, trip=trip)


async def get_entries_for_employee(
    conn: AsyncConnection, company_id: str, employee_id: str, dates: list[date] | None = None
) -> list[RosterEntry]:
    sql = _SELECT + " WHERE e.company_id = %s AND e.employee_id = %s"
    params: list[Any] = [company_id, employee_id]
    if dates is not None:
        sql += " AND e.service_date = ANY(%s)"
        params.append(dates)
    sql += " ORDER BY e.service_date, e.trip_type DESC"
    rows = await (await conn.execute(sql, params)).fetchall()
    return [_to_entry(r) for r in rows]


async def get_entry(conn: AsyncConnection, company_id: str, employee_id: str, entry_id: str) -> RosterEntry | None:
    row = await (
        await conn.execute(
            _SELECT + " WHERE e.company_id = %s AND e.employee_id = %s AND e.id = %s",
            (company_id, employee_id, entry_id),
        )
    ).fetchone()
    return _to_entry(row) if row else None


async def get_entry_for(
    conn: AsyncConnection, company_id: str, employee_id: str, service_date: date, trip_type: TripType
) -> RosterEntry | None:
    row = await (
        await conn.execute(
            _SELECT + " WHERE e.company_id = %s AND e.employee_id = %s AND e.service_date = %s AND e.trip_type = %s",
            (company_id, employee_id, service_date, trip_type),
        )
    ).fetchone()
    return _to_entry(row) if row else None
