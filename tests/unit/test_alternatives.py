"""Nearest-slot finder."""

from __future__ import annotations

import pytest

from app.domain.alternatives import find_alternatives
from app.domain.formatting import fmt_time
from tests.conftest import d0
from tests.factories import ctx, world

pytestmark = pytest.mark.no_db


def _labels(alts):
    return [fmt_time(a.trip.scheduled_at) for a in alts]


def _drops(company_prefix: str = ""):
    return [t for t in world().trips.values() if t.trip_type == "drop" and t.id.startswith(company_prefix)]


def test_full_cab_offers_nearest_three() -> None:
    base = ctx("RE-A001-D0-DROP", None, d0("15:00"))
    alts = find_alternatives(_drops("ACME"), base, desired_at=d0("20:00"))
    assert _labels(alts) == ["8:30 PM", "9:00 PM", "6:00 PM"]  # tie 6:00 vs 10:00 -> earlier wins
    assert all(a.allowed for a in alts)


def test_never_offers_another_companys_trips_even_if_passed_in() -> None:
    base = ctx("RE-A001-D0-DROP", None, d0("15:00"))
    alts = find_alternatives(_drops(), base, desired_at=d0("20:00"))  # Globex trips mixed in
    assert all(a.trip.company_id == "acme" for a in alts)
    assert "GLX-R1-DROP-D0-2000" not in {a.trip.id for a in alts}


def test_later_direction_for_clarification() -> None:
    base = ctx("RE-A001-D0-DROP", None, d0("14:00"))
    alts = find_alternatives(_drops("ACME"), base, direction="later")
    assert _labels(alts) == ["8:30 PM", "9:00 PM", "10:00 PM"]  # 8:00 PM is full


def test_excludes_routes_not_serving_zone_past_trips_and_current() -> None:
    base = ctx("RE-A001-D0-DROP", None, d0("18:10"))
    ids = {a.trip.id for a in find_alternatives(_drops("ACME"), base, desired_at=d0("19:00"))}
    assert "ACME-R2-DROP-D0-2100" not in ids  # Electronic City route
    assert "ACME-R1-DROP-D0-1800" not in ids  # already left
    assert "ACME-R1-DROP-D0-1900" not in ids  # current trip


def test_annotates_needs_approval_after_acme_cutoff() -> None:
    base = ctx("RE-A001-D0-DROP", None, d0("17:30"))
    alts = find_alternatives(_drops("ACME"), base, desired_at=d0("20:30"))
    assert alts and all(a.needs_approval == ("CUTOFF_PASSED",) for a in alts)
    assert find_alternatives(_drops("ACME"), base, desired_at=d0("20:30"), only_allowed=True) == []


def test_globex_after_cutoff_has_no_options() -> None:
    base = ctx("RE-G001-D0-DROP", None, d0("18:45"))
    assert find_alternatives(_drops("GLX"), base, desired_at=d0("21:00")) == []


def test_respects_alternatives_count_and_window() -> None:
    base = ctx("RE-A003-D0-DROP", None, d0("14:00"))
    alts = find_alternatives(_drops("ACME"), base, desired_at=d0("18:00"))
    assert len(alts) <= world().policies["acme"].alternatives_count
    assert all(abs((a.trip.scheduled_at - d0("18:00")).total_seconds()) <= 180 * 60 for a in alts)
