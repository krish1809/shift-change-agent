"""Rules engine: table-driven boundaries for both companies."""

from __future__ import annotations

import pytest

from app.domain.rules.engine import evaluate
from tests.conftest import d0
from tests.factories import ctx, world

pytestmark = pytest.mark.no_db


# Acme drop cut-off 120 min, Globex 60 min. Deadline is measured from the earlier of current/target.
@pytest.mark.parametrize(
    ("entry", "target", "now", "violated"),
    [
        # Acme A001 drop 19:00 -> 20:30: deadline 17:00
        ("RE-A001-D0-DROP", "ACME-R1-DROP-D0-2030", "16:59", False),
        ("RE-A001-D0-DROP", "ACME-R1-DROP-D0-2030", "17:00", False),  # exactly at deadline: allowed
        ("RE-A001-D0-DROP", "ACME-R1-DROP-D0-2030", "17:01", True),
        # Globex G001 drop 19:30 -> 21:00: deadline 18:30
        ("RE-G001-D0-DROP", "GLX-R1-DROP-D0-2100", "18:29", False),
        ("RE-G001-D0-DROP", "GLX-R1-DROP-D0-2100", "18:30", False),
        ("RE-G001-D0-DROP", "GLX-R1-DROP-D0-2100", "18:31", True),
        # moving EARLIER: G003 21:00 -> 19:30, deadline from 19:30 = 18:30 (not 20:00)
        ("RE-G003-D0-DROP", "GLX-R1-DROP-D0-1930", "18:30", False),
        ("RE-G003-D0-DROP", "GLX-R1-DROP-D0-1930", "19:15", True),
    ],
)
def test_cutoff_boundaries(entry, target, now, violated) -> None:
    result = evaluate(ctx(entry, target, d0(now)))
    assert ("CUTOFF_PASSED" in result.codes) is violated


@pytest.mark.parametrize(
    ("entry", "now", "violated"),
    [
        # Acme pickup cut-off 180 min: A001 D1 09:00 pickup -> deadline D1 06:00
        ("RE-A001-D1-PICK", d0("06:00", day=1), False),
        ("RE-A001-D1-PICK", d0("06:01", day=1), True),
        # Globex pickup cut-off 120 min: G001 D1 08:30 -> deadline D1 06:30
        ("RE-G001-D1-PICK", d0("06:30", day=1), False),
        ("RE-G001-D1-PICK", d0("06:31", day=1), True),
    ],
)
def test_pickup_cancel_cutoff(entry, now, violated) -> None:
    result = evaluate(ctx(entry, None, now, change_type="cancel"))
    assert ("CUTOFF_PASSED" in result.codes) is violated


def test_acme_cutoff_is_escalatable_globex_is_not() -> None:
    acme = evaluate(ctx("RE-A001-D0-DROP", "ACME-R1-DROP-D0-2030", d0("17:30")))
    globex = evaluate(ctx("RE-G001-D0-DROP", "GLX-R1-DROP-D0-2100", d0("18:45")))
    assert acme.codes == ["CUTOFF_PASSED"] and acme.escalatable
    assert globex.codes == ["CUTOFF_PASSED"] and not globex.escalatable


def test_violation_params_only_carry_own_company_values() -> None:
    acme = evaluate(ctx("RE-A001-D0-DROP", "ACME-R1-DROP-D0-2030", d0("17:30"))).violations[0]
    globex = evaluate(ctx("RE-G001-D0-DROP", "GLX-R1-DROP-D0-2100", d0("18:45"))).violations[0]
    assert acme.params == {
        "company_name": "Acme Tech",
        "desk_phone": "080-4000-1111",
        "cutoff_minutes": 120,
        "cutoff_label": "2 hours",
        "deadline": "5:00 PM",
        "trip_type": "drop",
    }
    assert globex.params["company_name"] == "Globex Finance"
    assert globex.params["cutoff_label"] == "60 minutes" and globex.params["deadline"] == "6:30 PM"
    assert "Acme" not in str(globex.params) and "Globex" not in str(acme.params)


def test_capacity_uses_effective_capacity_not_vehicle_seats() -> None:
    trip = world().trips["ACME-R1-DROP-D0-2000"]
    assert (trip.vehicle_seats, trip.effective_capacity, trip.seats_taken) == (6, 4, 4)
    result = evaluate(ctx("RE-A001-D0-DROP", "ACME-R1-DROP-D0-2000", d0("15:00")))
    assert result.codes == ["NO_SEAT"] and not result.escalatable


def test_globex_uses_full_vehicle() -> None:
    result = evaluate(ctx("RE-G001-D0-DROP", "GLX-R1-DROP-D0-2000", d0("15:00")))
    assert result.allowed  # 5/6 taken: one seat left


@pytest.mark.parametrize(
    ("entry", "target", "violated"),
    [
        ("RE-A003-D0-DROP", "ACME-R1-DROP-D0-2100", False),  # 18:00 -> 21:00 = 180, boundary inclusive
        ("RE-A003-D0-DROP", "ACME-R1-DROP-D0-2200", True),  # 18:00 -> 22:00 = 240 > 180
        ("RE-G002-D0-DROP", "GLX-R1-DROP-D0-1930", False),  # 17:30 -> 19:30 = 120
        ("RE-G002-D0-DROP", "GLX-R1-DROP-D0-2100", True),  # 17:30 -> 21:00 = 210 > 120
    ],
)
def test_change_window(entry, target, violated) -> None:
    result = evaluate(ctx(entry, target, d0("14:00")))
    assert ("OUTSIDE_CHANGE_WINDOW" in result.codes) is violated
    if violated:
        assert result.escalatable


@pytest.mark.parametrize(("changes", "violated"), [(0, False), (1, False), (2, True), (3, True)])
def test_daily_limit_globex(changes, violated) -> None:
    result = evaluate(ctx("RE-G003-D0-DROP", "GLX-R1-DROP-D0-2000", d0("15:00"), changes_today=changes))
    assert ("DAILY_CHANGE_LIMIT" in result.codes) is violated
    if violated:
        assert result.escalatable


def test_acme_has_no_daily_limit() -> None:
    assert evaluate(ctx("RE-A001-D0-DROP", "ACME-R1-DROP-D0-2100", d0("15:00"), changes_today=50)).allowed


def test_mixed_hard_and_soft_is_not_escalatable() -> None:
    # A003 at 17:00: 18:00 -> 22:00 breaks cut-off (soft) AND window (soft) -> escalatable
    soft = evaluate(ctx("RE-A003-D0-DROP", "ACME-R1-DROP-D0-2200", d0("17:00")))
    assert set(soft.codes) == {"CUTOFF_PASSED", "OUTSIDE_CHANGE_WINDOW"} and soft.escalatable
    # A001 at 17:30 -> full 20:00 cab: NO_SEAT (hard) + CUTOFF_PASSED (soft) -> not escalatable
    mixed = evaluate(ctx("RE-A001-D0-DROP", "ACME-R1-DROP-D0-2000", d0("17:30")))
    assert set(mixed.codes) == {"NO_SEAT", "CUTOFF_PASSED"} and not mixed.escalatable


def test_all_violations_collected() -> None:
    result = evaluate(ctx("RE-G003-D0-DROP", "GLX-R1-DROP-D0-1830", d0("19:15"), changes_today=2))
    # 18:30 already left, is full, is past the cut-off, is 150 min away (> 120) and limit is hit.
    assert result.codes == [
        "TRIP_IN_PAST",
        "NO_SEAT",
        "CUTOFF_PASSED",
        "OUTSIDE_CHANGE_WINDOW",
        "DAILY_CHANGE_LIMIT",
    ]


def test_target_in_past() -> None:
    result = evaluate(ctx("RE-A001-D0-DROP", "ACME-R1-DROP-D0-1800", d0("18:30")))
    assert "TRIP_IN_PAST" in result.codes and not result.escalatable


def test_already_on_trip_is_a_no_op_not_a_violation() -> None:
    result = evaluate(ctx("RE-A001-D0-DROP", "ACME-R1-DROP-D0-1900", d0("15:00")))
    assert result.already_on_trip and result.violations == []


def test_route_not_served() -> None:
    # A001 lives in Whitefield; route R2 serves Electronic City only.
    result = evaluate(ctx("RE-A001-D0-DROP", "ACME-R2-DROP-D0-2100", d0("15:00")))
    assert result.codes == ["ROUTE_NOT_SERVED"]


def test_wrong_type_or_date() -> None:
    result = evaluate(ctx("RE-A001-D0-DROP", "ACME-R1-DROP-D1-2000", d0("15:00")))
    assert "TRIP_TYPE_MISMATCH" in result.codes


def test_no_trip_at_requested_time_is_never_snapped() -> None:
    result = evaluate(ctx("RE-A001-D0-DROP", None, d0("15:00"), requested_label="7:30 PM"))
    assert result.codes == ["NO_TRIP_AT_TIME"] and not result.escalatable


def test_other_company_trip_id_is_not_found() -> None:
    # The handler looks trips up by the caller's company, so a Globex id resolves to no trip at all.
    assert evaluate(ctx("RE-A001-D0-DROP", None, d0("15:00"))).codes == ["TRIP_NOT_FOUND"]
