"""Bonus: the last seat goes to exactly one employee; a double submit creates exactly one request."""

from __future__ import annotations

import asyncio
from collections import Counter

import pytest

from app.db.repositories import audit, roster, trips
from app.db.seed import reseed
from app.domain.booking import BookingStatus, ChangeSpec, apply_change, idempotency_key, reconcile
from tests.conftest import BASE_DATE, d0

NOW = d0("15:00")
REPEATS = 20


def spec(company: str, employee: str, entry: str, to_trip: str | None, *, version: int = 1, session: str = "s1"):
    return ChangeSpec(
        company_id=company,
        employee_id=employee,
        roster_entry_id=entry,
        change_type="reschedule" if to_trip else "cancel",
        to_trip_id=to_trip,
        expected_version=version,
        thread_id=f"{company}:{employee}:{session}",
    )


async def _apply_on_own_connection(pool, change: ChangeSpec):
    async with pool.connection() as conn:
        return await apply_change(conn, change, now=NOW)


async def _seats(pool, company: str, trip_id: str) -> int:
    async with pool.connection() as conn:
        return (await trips.get_trip(conn, company, trip_id)).seats_taken


async def _request_count(pool, company: str) -> int:
    async with pool.connection() as conn:
        row = await (
            await conn.execute("SELECT count(*) AS n FROM shift_requests WHERE company_id=%s", (company,))
        ).fetchone()
    return row["n"]


@pytest.mark.parametrize(
    ("company", "a", "b", "target"),
    [
        ("acme", ("A001", "RE-A001-D0-DROP"), ("A004", "RE-A004-D0-DROP"), "ACME-R1-DROP-D0-2030"),
        ("globex", ("G001", "RE-G001-D0-DROP"), ("G003", "RE-G003-D0-DROP"), "GLX-R1-DROP-D0-2000"),
    ],
)
async def test_last_seat_race_exactly_one_winner(pool, company, a, b, target) -> None:
    for _ in range(REPEATS):
        async with pool.connection() as conn:
            await reseed(conn, BASE_DATE)
        capacity_before = await _seats(pool, company, target)
        results = await asyncio.gather(
            _apply_on_own_connection(pool, spec(company, a[0], a[1], target)),
            _apply_on_own_connection(pool, spec(company, b[0], b[1], target)),
        )
        assert Counter(r.status for r in results) == {BookingStatus.APPLIED: 1, BookingStatus.SEAT_TAKEN: 1}
        assert await _seats(pool, company, target) == capacity_before + 1  # full, never over
        assert await _request_count(pool, company) == 1


async def test_double_submit_creates_one_request_and_one_seat(pool) -> None:
    for _ in range(REPEATS):
        async with pool.connection() as conn:
            await reseed(conn, BASE_DATE)
        change = spec("acme", "A001", "RE-A001-D0-DROP", "ACME-R1-DROP-D0-2100")
        results = await asyncio.gather(
            _apply_on_own_connection(pool, change),
            _apply_on_own_connection(
                pool, spec("acme", "A001", "RE-A001-D0-DROP", "ACME-R1-DROP-D0-2100", session="s2")
            ),
        )
        statuses = Counter(r.status for r in results)
        assert statuses[BookingStatus.APPLIED] == 1
        assert statuses[BookingStatus.ALREADY_ON_TRIP] + statuses[BookingStatus.DUPLICATE] == 1
        assert await _request_count(pool, "acme") == 1
        assert await _seats(pool, "acme", "ACME-R1-DROP-D0-2100") == 3  # 2 + A001 once
        assert await _seats(pool, "acme", "ACME-R1-DROP-D0-1900") == 2  # A001's old seat released once


async def test_reschedule_moves_seat_and_bumps_version(pool) -> None:
    async with pool.connection() as conn:
        result = await apply_change(conn, spec("acme", "A001", "RE-A001-D0-DROP", "ACME-R1-DROP-D0-2030"), now=NOW)
        entry = await roster.get_entry(conn, "acme", "A001", "RE-A001-D0-DROP")
        log = await audit.list_audit(conn, "acme", request_id=result.request.id)
    assert result.status == BookingStatus.APPLIED
    assert result.request.id.startswith("REQ-") and len(result.request.id) == 12
    assert result.request.status == "APPLIED" and result.request.from_trip_id == "ACME-R1-DROP-D0-1900"
    assert entry.trip_id == "ACME-R1-DROP-D0-2030" and entry.version == 2
    assert [row["action"] for row in log] == ["CHANGE_APPLIED"]
    assert await _seats(pool, "acme", "ACME-R1-DROP-D0-2030") == 4
    assert await _seats(pool, "acme", "ACME-R1-DROP-D0-1900") == 2


async def test_cancel_frees_a_seat(pool) -> None:
    async with pool.connection() as conn:
        result = await apply_change(conn, spec("acme", "A002", "RE-A002-D1-PICK", None), now=NOW)
        entry = await roster.get_entry(conn, "acme", "A002", "RE-A002-D1-PICK")
    assert result.status == BookingStatus.APPLIED
    assert entry.status == "cancelled" and entry.trip_id is None
    assert await _seats(pool, "acme", "ACME-R1-PICK-D1-0900") == 2


async def test_cancel_twice_is_a_no_op(pool) -> None:
    async with pool.connection() as conn:
        await apply_change(conn, spec("acme", "A002", "RE-A002-D1-PICK", None), now=NOW)
        again = await apply_change(conn, spec("acme", "A002", "RE-A002-D1-PICK", None, version=2), now=NOW)
    assert again.status == BookingStatus.ALREADY_ON_TRIP
    assert await _seats(pool, "acme", "ACME-R1-PICK-D1-0900") == 2


async def test_stale_roster_version_is_rejected(pool) -> None:
    async with pool.connection() as conn:
        result = await apply_change(
            conn, spec("acme", "A001", "RE-A001-D0-DROP", "ACME-R1-DROP-D0-2100", version=7), now=NOW
        )
    assert result.status == BookingStatus.ROSTER_CHANGED
    assert await _request_count(pool, "acme") == 0


async def test_full_cab_returns_seat_taken_and_rolls_back(pool) -> None:
    async with pool.connection() as conn:
        result = await apply_change(conn, spec("acme", "A001", "RE-A001-D0-DROP", "ACME-R1-DROP-D0-2000"), now=NOW)
        entry = await roster.get_entry(conn, "acme", "A001", "RE-A001-D0-DROP")
    assert result.status == BookingStatus.SEAT_TAKEN
    assert entry.trip_id == "ACME-R1-DROP-D0-1900" and entry.version == 1
    assert await _request_count(pool, "acme") == 0  # request row rolled back with the seat


async def test_cross_tenant_booking_is_not_found(pool) -> None:
    async with pool.connection() as conn:
        result = await apply_change(conn, spec("acme", "G001", "RE-G001-D0-DROP", "GLX-R1-DROP-D0-2100"), now=NOW)
    assert result.status == BookingStatus.NOT_FOUND


async def test_reconcile_finds_committed_change_by_key(pool) -> None:
    change = spec("acme", "A001", "RE-A001-D0-DROP", "ACME-R1-DROP-D0-2100")
    async with pool.connection() as conn:
        assert await reconcile(conn, "acme", "A001", idempotency_key(change)) is None
        await apply_change(conn, change, now=NOW)
        found = await reconcile(conn, "acme", "A001", idempotency_key(change))
        assert await reconcile(conn, "globex", "A001", idempotency_key(change)) is None
    assert found is not None and found.status == "APPLIED"
