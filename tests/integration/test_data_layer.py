"""Phase 1: seed correctness, tenant-scoped repositories and DB-level isolation."""
from __future__ import annotations

import importlib
import inspect
import pkgutil

import psycopg
import pytest

import app.db.repositories as repos_pkg
from app.db.repositories import companies, roster, trips
from tests.conftest import BASE_DATE

EXPECTED_SEATS = {
    # trip id: (seats_taken, effective_capacity)
    "ACME-R1-DROP-D0-1800": (1, 4),
    "ACME-R1-DROP-D0-1900": (3, 4),  # 1 filler + A001 + A004
    "ACME-R1-DROP-D0-2000": (4, 4),  # full: Acme caps a 6-seat vehicle at 4
    "ACME-R1-DROP-D0-2030": (3, 4),  # exactly one seat left (last-seat race)
    "ACME-R1-DROP-D0-2100": (2, 4),
    "ACME-R1-DROP-D0-2200": (0, 4),
    "ACME-R1-PICK-D1-0900": (3, 4),
    "GLX-R1-DROP-D0-1830": (6, 6),
    "GLX-R1-DROP-D0-1930": (4, 6),
    "GLX-R1-DROP-D0-2000": (5, 6),  # one seat left; Globex uses the full vehicle
    "GLX-R1-DROP-D0-2100": (1, 6),
}


@pytest.mark.parametrize(("trip_id", "expected"), EXPECTED_SEATS.items())
async def test_seed_seats_match_scenario_map(pool, trip_id, expected) -> None:
    company_id = "acme" if trip_id.startswith("ACME") else "globex"
    async with pool.connection() as conn:
        trip = await trips.get_trip(conn, company_id, trip_id)
    assert trip is not None
    assert (trip.seats_taken, trip.effective_capacity) == expected


async def test_seed_resolves_day_offsets_to_base_date(pool) -> None:
    async with pool.connection() as conn:
        today = await trips.get_trip(conn, "acme", "ACME-R1-DROP-D0-1900")
        tomorrow = await trips.get_trip(conn, "acme", "ACME-R1-PICK-D1-0900")
    assert today.service_date == BASE_DATE
    assert tomorrow.service_date.toordinal() == BASE_DATE.toordinal() + 1
    assert today.scheduled_at.isoformat() == "2026-03-02T13:30:00+00:00"  # 19:00 IST


async def test_other_company_trip_is_not_found(pool) -> None:
    async with pool.connection() as conn:
        assert await trips.get_trip(conn, "acme", "GLX-R1-DROP-D0-2000") is None
        assert await trips.get_trip(conn, "globex", "GLX-R1-DROP-D0-2000") is not None


async def test_other_company_roster_is_not_visible(pool) -> None:
    async with pool.connection() as conn:
        assert await roster.get_entries_for_employee(conn, "acme", "G001") == []
        assert await roster.get_entry(conn, "acme", "G001", "RE-G001-D0-DROP") is None
        own = await roster.get_entries_for_employee(conn, "globex", "G001")
    assert {e.id for e in own} == {"RE-G001-D0-DROP", "RE-G001-D1-PICK", "RE-G001-D1-DROP"}


async def test_roster_entry_carries_its_trip(pool) -> None:
    async with pool.connection() as conn:
        entry = await roster.get_entry(conn, "acme", "A001", "RE-A001-D0-DROP")
    assert entry.trip is not None and entry.trip.id == "ACME-R1-DROP-D0-1900"
    assert entry.trip.route_zones == ["Whitefield", "Marathahalli"]


async def test_policies_differ_per_company(pool) -> None:
    async with pool.connection() as conn:
        acme = await companies.get_policy(conn, "acme")
        globex = await companies.get_policy(conn, "globex")
    assert (acme.cutoff_minutes_drop, acme.cutoff_escalatable, acme.max_seats_per_cab) == (120, True, 4)
    assert (globex.cutoff_minutes_drop, globex.cutoff_escalatable, globex.max_seats_per_cab) == (60, False, 6)


async def test_cross_tenant_fk(pool) -> None:
    """The database itself refuses an Acme roster entry that points at a Globex trip."""
    async with pool.connection() as conn:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            await conn.execute(
                """UPDATE roster_entries SET trip_id = 'GLX-R1-DROP-D0-2000'
                    WHERE company_id = 'acme' AND id = 'RE-A001-D0-DROP'"""
            )


async def test_seat_check_constraint_blocks_overbooking(pool) -> None:
    async with pool.connection() as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            await conn.execute(
                "UPDATE trips SET seats_taken = seats_taken + 1 WHERE company_id='acme' AND id='ACME-R1-DROP-D0-2000'"
            )


def _public_repo_functions():
    for info in pkgutil.iter_modules(repos_pkg.__path__):
        module = importlib.import_module(f"{repos_pkg.__name__}.{info.name}")
        for name, fn in inspect.getmembers(module, inspect.iscoroutinefunction):
            if not name.startswith("_") and fn.__module__ == module.__name__:
                yield f"{info.name}.{name}", fn


@pytest.mark.no_db
@pytest.mark.parametrize(("name", "fn"), list(_public_repo_functions()))
def test_every_repository_function_requires_company_id(name, fn) -> None:
    params = list(inspect.signature(fn).parameters.values())
    assert params[0].name == "conn", name
    assert params[1].name == "company_id", f"{name}: second parameter must be company_id"
    assert params[1].default is inspect.Parameter.empty, f"{name}: company_id must be required"
