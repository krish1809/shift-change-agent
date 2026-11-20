"""Loads data/seed.json into Postgres.

Day offsets in the file ("day": 0 / 1) are resolved against a base date (D0), so the same data
works for "today" in the demo and for the frozen test date 2026-03-02.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from app.domain.clock import DEFAULT_TZ, Clock

SEED_PATH = Path(__file__).resolve().parents[2] / "data" / "seed.json"

DOMAIN_TABLES = (
    "audit_log",
    "turn_metrics",
    "shift_requests",
    "roster_entries",
    "trips",
    "employees",
    "routes",
    "company_policies",
    "companies",
)
CHECKPOINT_TABLES = ("checkpoint_writes", "checkpoint_blobs", "checkpoints")


def load_seed(path: Path = SEED_PATH) -> dict[str, Any]:
    return json.loads(path.read_text())


def resolve_base_date(setting: str, clock: Clock) -> date:
    if setting:
        return date.fromisoformat(setting)
    return clock.now().astimezone(DEFAULT_TZ).date()


async def reseed(conn: AsyncConnection, base_date: date, data: dict[str, Any] | None = None) -> None:
    """Wipe and reload everything (including conversation checkpoints) in one transaction."""
    data = data or load_seed()
    async with conn.transaction():
        await _truncate_all(conn)
        await _insert_all(conn, data, base_date)


async def seed_if_empty(pool: AsyncConnectionPool, base_date: date) -> bool:
    async with pool.connection() as conn:
        row = await (await conn.execute("SELECT count(*) AS n FROM companies")).fetchone()
        if row and row["n"] > 0:
            return False
        await reseed(conn, base_date)
        return True


async def _truncate_all(conn: AsyncConnection) -> None:
    tables = list(DOMAIN_TABLES)
    for table in CHECKPOINT_TABLES:
        exists = await (await conn.execute("SELECT to_regclass(%s) AS t", (table,))).fetchone()
        if exists and exists["t"]:
            tables.append(table)
    await conn.execute(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE")


async def _insert_all(conn: AsyncConnection, data: dict[str, Any], base_date: date) -> None:
    policies: dict[str, dict[str, Any]] = {}
    timezones: dict[str, ZoneInfo] = {}
    for company in data["companies"]:
        policy = company["policy"]
        policies[company["id"]] = policy
        timezones[company["id"]] = ZoneInfo(company["timezone"])
        await conn.execute(
            "INSERT INTO companies (id, name, timezone, desk_phone) VALUES (%s, %s, %s, %s)",
            (company["id"], company["name"], company["timezone"], company["desk_phone"]),
        )
        await conn.execute(
            """INSERT INTO company_policies (company_id, cutoff_minutes_pickup, cutoff_minutes_drop,
                 cutoff_escalatable, max_seats_per_cab, change_window_minutes, window_escalatable,
                 max_changes_per_day, daily_limit_escalatable, approval_timeout_minutes,
                 alternatives_count, alternatives_window_minutes)
               VALUES (%(company_id)s, %(cutoff_minutes_pickup)s, %(cutoff_minutes_drop)s,
                 %(cutoff_escalatable)s, %(max_seats_per_cab)s, %(change_window_minutes)s,
                 %(window_escalatable)s, %(max_changes_per_day)s, %(daily_limit_escalatable)s,
                 %(approval_timeout_minutes)s, %(alternatives_count)s, %(alternatives_window_minutes)s)""",
            {"company_id": company["id"], **policy},
        )

    for route in data["routes"]:
        await conn.execute(
            "INSERT INTO routes (company_id, id, name, zones) VALUES (%s, %s, %s, %s)",
            (route["company_id"], route["id"], route["name"], route["zones"]),
        )

    for emp in data["employees"]:
        await conn.execute(
            "INSERT INTO employees (company_id, id, name, zone, role) VALUES (%s, %s, %s, %s, %s)",
            (emp["company_id"], emp["id"], emp["name"], emp["zone"], emp["role"]),
        )

    riders = Counter((e["company_id"], e["trip_id"]) for e in data["roster_entries"] if e.get("trip_id"))
    for trip in data["trips"]:
        cid = trip["company_id"]
        service_date = base_date + timedelta(days=trip["day"])
        scheduled_at = datetime.combine(service_date, time.fromisoformat(trip["time"]), timezones[cid])
        capacity = min(trip["vehicle_seats"], policies[cid]["max_seats_per_cab"])
        taken = trip["filler_riders"] + riders[(cid, trip["id"])]
        await conn.execute(
            """INSERT INTO trips (company_id, id, route_id, trip_type, service_date, scheduled_at,
                 vehicle_seats, effective_capacity, seats_taken, cab_label)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                cid,
                trip["id"],
                trip["route_id"],
                trip["trip_type"],
                service_date,
                scheduled_at,
                trip["vehicle_seats"],
                capacity,
                taken,
                trip["cab_label"],
            ),
        )

    for entry in data["roster_entries"]:
        await conn.execute(
            """INSERT INTO roster_entries (company_id, id, employee_id, service_date, trip_type, trip_id, status)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (
                entry["company_id"],
                entry["id"],
                entry["employee_id"],
                base_date + timedelta(days=entry["day"]),
                entry["trip_type"],
                entry.get("trip_id"),
                "scheduled" if entry.get("trip_id") else "cancelled",
            ),
        )
