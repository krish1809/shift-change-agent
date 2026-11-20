"""Atomic seat changes. Every change is ONE transaction: seat, roster, request row and audit row.

Expected outcomes (no seat, duplicate, stale roster) are returned as typed results, never raised.
Concurrency guarantees:
  * last seat: the seat is taken by a conditional UPDATE (`seats_taken < effective_capacity`), so two
    racing transactions can't both succeed; the DB CHECK constraint is a second line of defence.
  * double submit: the roster entry row is locked first, so the second identical request sees the
    first one's result (ALREADY_ON_TRIP); retries carry the same idempotency key (DUPLICATE).
  * no deadlocks: both trip rows are locked in id order before anything else is written.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from app.db.repositories import audit
from app.db.repositories import requests as requests_repo
from app.domain.models import ChangeType, ShiftRequest
from app.logging import get_correlation_id

ActorType = Literal["employee", "manager", "agent", "system"]


class BookingStatus(StrEnum):
    APPLIED = "APPLIED"
    SEAT_TAKEN = "SEAT_TAKEN"
    ROSTER_CHANGED = "ROSTER_CHANGED"
    DUPLICATE = "DUPLICATE"
    ALREADY_ON_TRIP = "ALREADY_ON_TRIP"
    NOT_FOUND = "NOT_FOUND"


@dataclass(frozen=True)
class BookingResult:
    status: BookingStatus
    request: ShiftRequest | None = None


@dataclass(frozen=True)
class ChangeSpec:
    """What to change, for whom. Identity fields come from the verified token, never from the model."""

    company_id: str
    employee_id: str
    roster_entry_id: str
    change_type: ChangeType
    to_trip_id: str | None  # None for cancel
    expected_version: int
    thread_id: str


class _Abort(Exception):
    """Internal: roll the transaction back and return this result."""

    def __init__(self, result: BookingResult) -> None:
        self.result = result


def idempotency_key(spec: ChangeSpec, *, salt: str = "") -> str:
    raw = "|".join(
        [
            spec.company_id,
            spec.employee_id,
            spec.roster_entry_id,
            spec.to_trip_id or "cancel",
            str(spec.expected_version),
            salt,
        ]
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def new_request_id() -> str:
    return "REQ-" + secrets.token_hex(4).upper()


async def apply_change(
    conn: AsyncConnection,
    spec: ChangeSpec,
    *,
    now: datetime,
    actor_type: ActorType = "employee",
    actor_id: str | None = None,
    approved_request_id: str | None = None,
) -> BookingResult:
    """Apply a reschedule or cancel atomically.

    `approved_request_id` is set when a manager approved a pending request: that row is moved to
    APPROVED_APPLIED instead of inserting a new APPLIED row (same code path for both).
    """
    try:
        async with conn.transaction():
            return await _apply_in_tx(conn, spec, now, actor_type, actor_id or spec.employee_id, approved_request_id)
    except _Abort as abort:
        return abort.result


async def _apply_in_tx(
    conn: AsyncConnection,
    spec: ChangeSpec,
    now: datetime,
    actor_type: ActorType,
    actor_id: str,
    approved_request_id: str | None,
) -> BookingResult:
    entry = await (
        await conn.execute(
            """SELECT * FROM roster_entries
                WHERE company_id = %s AND employee_id = %s AND id = %s FOR UPDATE""",
            (spec.company_id, spec.employee_id, spec.roster_entry_id),
        )
    ).fetchone()
    if entry is None:
        return BookingResult(BookingStatus.NOT_FOUND)

    if spec.change_type == "reschedule" and entry["trip_id"] == spec.to_trip_id:
        return BookingResult(BookingStatus.ALREADY_ON_TRIP)
    if spec.change_type == "cancel" and entry["status"] == "cancelled":
        return BookingResult(BookingStatus.ALREADY_ON_TRIP)
    if entry["version"] != spec.expected_version:
        return BookingResult(BookingStatus.ROSTER_CHANGED)

    from_trip_id: str | None = entry["trip_id"]
    await _lock_trips(conn, spec.company_id, [t for t in (from_trip_id, spec.to_trip_id) if t])

    key = idempotency_key(spec)
    if approved_request_id is None:
        request_id = await _insert_request(conn, spec, from_trip_id, key, "APPLIED", [], now)
        if request_id is None:
            existing = await requests_repo.find_by_idempotency_key(conn, spec.company_id, spec.employee_id, key)
            return BookingResult(BookingStatus.DUPLICATE, existing)
    else:
        request_id = approved_request_id

    if spec.to_trip_id is not None:
        taken = await (
            await conn.execute(
                """UPDATE trips SET seats_taken = seats_taken + 1
                    WHERE company_id = %s AND id = %s AND seats_taken < effective_capacity
                RETURNING id""",
                (spec.company_id, spec.to_trip_id),
            )
        ).fetchone()
        if taken is None:
            raise _Abort(BookingResult(BookingStatus.SEAT_TAKEN))

    if from_trip_id is not None:
        await conn.execute(
            "UPDATE trips SET seats_taken = seats_taken - 1 WHERE company_id = %s AND id = %s",
            (spec.company_id, from_trip_id),
        )

    await conn.execute(
        """UPDATE roster_entries SET trip_id = %s, status = %s, version = version + 1
            WHERE company_id = %s AND id = %s AND version = %s""",
        (
            spec.to_trip_id,
            "scheduled" if spec.to_trip_id else "cancelled",
            spec.company_id,
            spec.roster_entry_id,
            spec.expected_version,
        ),
    )

    if approved_request_id is not None:
        moved = await (
            await conn.execute(
                """UPDATE shift_requests SET status = 'APPROVED_APPLIED', updated_at = %s, resumed_at = %s
                    WHERE company_id = %s AND id = %s AND status = 'PENDING_APPROVAL'
                RETURNING id""",
                (now, now, spec.company_id, approved_request_id),
            )
        ).fetchone()
        if moved is None:  # already finalised by an earlier run: make this run a no-op
            raise _Abort(BookingResult(BookingStatus.DUPLICATE))

    await audit.add_audit(
        conn,
        spec.company_id,
        at=now,
        actor_type=actor_type,
        actor_id=actor_id,
        action="CHANGE_APPLIED",
        request_id=request_id,
        thread_id=spec.thread_id,
        correlation_id=get_correlation_id(),
        payload={"change_type": spec.change_type, "from": from_trip_id, "to": spec.to_trip_id},
    )
    request = await requests_repo.get_request(conn, spec.company_id, request_id)
    return BookingResult(BookingStatus.APPLIED, request)


async def _lock_trips(conn: AsyncConnection, company_id: str, trip_ids: list[str]) -> None:
    """Lock both trip rows in id order BEFORE any FK-checked insert.

    NO KEY UPDATE is the lock a seat-count UPDATE takes anyway; unlike FOR UPDATE it doesn't conflict
    with the KEY SHARE locks that foreign-key checks on shift_requests take, so racing transactions
    queue up instead of deadlocking.
    """
    await conn.execute(
        "SELECT id FROM trips WHERE company_id = %s AND id = ANY(%s) ORDER BY id FOR NO KEY UPDATE",
        (company_id, sorted(set(trip_ids))),
    )


async def _insert_request(
    conn: AsyncConnection,
    spec: ChangeSpec,
    from_trip_id: str | None,
    key: str,
    status: str,
    violations: list[dict[str, Any]],
    now: datetime,
) -> str | None:
    """Insert a request row; None if an identical (same idempotency key / open approval) row exists."""
    row = await (
        await conn.execute(
            """INSERT INTO shift_requests (id, company_id, employee_id, roster_entry_id, change_type,
                   from_trip_id, to_trip_id, status, violations, idempotency_key, thread_id,
                   roster_version, created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT DO NOTHING
               RETURNING id""",
            (
                new_request_id(),
                spec.company_id,
                spec.employee_id,
                spec.roster_entry_id,
                spec.change_type,
                from_trip_id,
                spec.to_trip_id,
                status,
                Jsonb(violations),
                key,
                spec.thread_id,
                spec.expected_version,
                now,
                now,
            ),
        )
    ).fetchone()
    return row["id"] if row else None


async def record_request(
    conn: AsyncConnection,
    spec: ChangeSpec,
    *,
    status: Literal["REJECTED", "PENDING_APPROVAL"],
    from_trip_id: str | None,
    violations: list[dict[str, Any]],
    now: datetime,
    key: str | None = None,
) -> BookingResult:
    """Record a request that was NOT applied: a rejection (for the audit trail) or an open approval.

    Rejections get a unique key (they are history, not retries). Approvals use the key of the offer
    being accepted, so accepting the same offer twice yields DUPLICATE with the existing row.
    """
    key = key or ("rej:" + secrets.token_hex(8))
    async with conn.transaction():
        request_id = await _insert_request(conn, spec, from_trip_id, key, status, violations, now)
        if request_id is None:
            existing = await requests_repo.find_by_idempotency_key(conn, spec.company_id, spec.employee_id, key)
            if existing is None:  # another approval is already open for this roster entry
                existing = await _open_request_for_entry(conn, spec)
            return BookingResult(BookingStatus.DUPLICATE, existing)
        action = "ESCALATED" if status == "PENDING_APPROVAL" else "CHANGE_REJECTED"
        await audit.add_audit(
            conn,
            spec.company_id,
            at=now,
            actor_type="employee",
            actor_id=spec.employee_id,
            action=action,
            request_id=request_id,
            thread_id=spec.thread_id,
            correlation_id=get_correlation_id(),
            payload={"violations": [v.get("code") for v in violations], "to": spec.to_trip_id},
        )
        request = await requests_repo.get_request(conn, spec.company_id, request_id)
    return BookingResult(BookingStatus.APPLIED, request)


async def _open_request_for_entry(conn: AsyncConnection, spec: ChangeSpec) -> ShiftRequest | None:
    row = await (
        await conn.execute(
            """SELECT * FROM shift_requests
                WHERE company_id = %s AND employee_id = %s AND roster_entry_id = %s AND status = 'PENDING_APPROVAL'""",
            (spec.company_id, spec.employee_id, spec.roster_entry_id),
        )
    ).fetchone()
    return ShiftRequest(**row) if row else None


async def reconcile(conn: AsyncConnection, company_id: str, employee_id: str, key: str) -> ShiftRequest | None:
    """After a write timed out: did it actually happen? The database is the source of truth."""
    return await requests_repo.find_by_idempotency_key(conn, company_id, employee_id, key)
