"""Pure builders: domain objects straight from data/seed.json (no database), for unit tests."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from functools import lru_cache

from app.db.seed import load_seed
from app.domain.clock import DEFAULT_TZ
from app.domain.models import Company, Employee, Policy, RosterEntry, Trip
from app.domain.rules.base import RuleContext
from tests.conftest import BASE_DATE


@dataclass(frozen=True)
class World:
    companies: dict[str, Company]
    policies: dict[str, Policy]
    employees: dict[tuple[str, str], Employee]
    trips: dict[str, Trip]
    entries: dict[str, RosterEntry]


@lru_cache
def world() -> World:
    data = load_seed()
    companies, policies = {}, {}
    for c in data["companies"]:
        companies[c["id"]] = Company(
            id=c["id"], name=c["name"], timezone=c["timezone"], desk_phone=c["desk_phone"]
        )
        policies[c["id"]] = Policy(company_id=c["id"], **c["policy"])
    zones = {(r["company_id"], r["id"]): r["zones"] for r in data["routes"]}
    employees = {(e["company_id"], e["id"]): Employee(**e) for e in data["employees"]}
    riders = Counter((e["company_id"], e["trip_id"]) for e in data["roster_entries"])
    trips = {}
    for t in data["trips"]:
        day = BASE_DATE + timedelta(days=t["day"])
        trips[t["id"]] = Trip(
            company_id=t["company_id"],
            id=t["id"],
            route_id=t["route_id"],
            trip_type=t["trip_type"],
            service_date=day,
            scheduled_at=datetime.combine(day, time.fromisoformat(t["time"]), DEFAULT_TZ),
            vehicle_seats=t["vehicle_seats"],
            effective_capacity=min(t["vehicle_seats"], policies[t["company_id"]].max_seats_per_cab),
            seats_taken=t["filler_riders"] + riders[(t["company_id"], t["id"])],
            cab_label=t["cab_label"],
            route_zones=zones[(t["company_id"], t["route_id"])],
        )
    entries = {
        e["id"]: RosterEntry(
            company_id=e["company_id"],
            id=e["id"],
            employee_id=e["employee_id"],
            service_date=BASE_DATE + timedelta(days=e["day"]),
            trip_type=e["trip_type"],
            trip_id=e["trip_id"],
            status="scheduled",
            version=1,
            trip=trips[e["trip_id"]],
        )
        for e in data["roster_entries"]
    }
    return World(companies, policies, employees, trips, entries)


def ctx(
    entry_id: str,
    target_id: str | None,
    now: datetime,
    *,
    change_type: str = "reschedule",
    changes_today: int = 0,
    requested_label: str | None = None,
    target: Trip | None = None,
) -> RuleContext:
    w = world()
    entry = w.entries[entry_id]
    cid = entry.company_id
    return RuleContext(
        company=w.companies[cid],
        policy=w.policies[cid],
        employee=w.employees[(cid, entry.employee_id)],
        now=now,
        change_type=change_type,
        entry=entry,
        current_trip=entry.trip,
        target_trip=target or (w.trips.get(target_id) if target_id else None),
        changes_today=changes_today,
        requested_label=requested_label,
    )
