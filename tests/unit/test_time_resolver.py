"""Time/date resolver: verbatim user spans -> values, or an honest 'ambiguous'/'unparseable'."""

from __future__ import annotations

from datetime import date, time

import pytest

from app.domain.time_resolver import resolve_date, resolve_time

pytestmark = pytest.mark.no_db

DROP_19 = {"trip_type": "drop", "current_time": time(19, 0), "window_minutes": 180}
PICK_09 = {"trip_type": "pickup", "current_time": time(9, 0), "window_minutes": 180}


@pytest.mark.parametrize(
    ("text", "ctx", "expected"),
    [
        ("8:30 pm", {}, time(20, 30)),
        ("20:30", {}, time(20, 30)),
        ("8.30pm", {}, time(20, 30)),
        ("830pm", {}, time(20, 30)),
        ("8:30 P.M.", {}, time(20, 30)),
        ("9 pm", {}, time(21, 0)),
        ("9am", {}, time(9, 0)),
        ("12 pm", {}, time(12, 0)),
        ("12 am", {}, time(0, 0)),
        ("08:30", {}, time(8, 30)),
        ("8", DROP_19, time(20, 0)),  # 08:00 is 11 h away from a 19:00 drop
        ("8 o'clock", DROP_19, time(20, 0)),
        ("make it 9 instead", DROP_19, time(21, 0)),
        ("8", PICK_09, time(8, 0)),
        ("half past 8", DROP_19, time(20, 30)),
        ("quarter to 9", DROP_19, time(20, 45)),
        ("quarter past 8", DROP_19, time(20, 15)),
        ("30 min later", DROP_19, time(19, 30)),
        ("30 mins later", DROP_19, time(19, 30)),
        ("an hour earlier", DROP_19, time(18, 0)),
        ("half an hour later", DROP_19, time(19, 30)),
        ("+45 mins", DROP_19, time(19, 45)),
        ("2 hours later", DROP_19, time(21, 0)),
        ("noon", {}, time(12, 0)),
        ("midnight", {}, time(0, 0)),
        ("9 baje", DROP_19, time(21, 0)),
        ("saade 8", DROP_19, time(20, 30)),
        ("paune 9", DROP_19, time(20, 45)),
        ("9 tonight", {}, time(21, 0)),
        ("8 in the morning", {}, time(8, 0)),
        ("9", {"trip_type": "drop"}, time(21, 0)),  # no current trip: typical drop time anchors
    ],
)
def test_exact_times(text, ctx, expected) -> None:
    result = resolve_time(text, **ctx)
    assert result.kind == "exact", (text, result)
    assert result.value == expected


@pytest.mark.parametrize(
    ("text", "ctx", "candidates"),
    [
        ("9", {}, (time(9, 0), time(21, 0))),  # no trip type known
        ("11", DROP_19, (time(11, 0), time(23, 0))),  # neither reading within 180 min of 19:00
        ("between 8 and 9", DROP_19, (time(20, 0), time(21, 0))),
    ],
)
def test_ambiguous_times(text, ctx, candidates) -> None:
    result = resolve_time(text, **ctx)
    assert result.kind == "ambiguous"
    assert result.candidates == candidates


@pytest.mark.parametrize("text", ["a bit late", "later", "after my meeting", "", "   ", "asap", "30 min later"])
def test_unparseable_times(text) -> None:
    # "30 min later" with no current trip time can't be resolved either.
    assert resolve_time(text).kind == "unparseable"


def test_relative_across_midnight_is_unparseable() -> None:
    assert resolve_time("3 hours later", trip_type="drop", current_time=time(22, 0)).kind == "unparseable"


TODAY = date(2026, 3, 2)  # a Monday


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("today", TODAY),
        ("tonight", TODAY),
        ("aaj", TODAY),
        ("tomorrow", date(2026, 3, 3)),
        ("tmrw", date(2026, 3, 3)),
        ("kal", date(2026, 3, 3)),
        ("day after tomorrow", date(2026, 3, 4)),
        ("Friday", date(2026, 3, 6)),
        ("on monday", TODAY),
        ("next monday", date(2026, 3, 9)),
        ("on the 5th", date(2026, 3, 5)),
        ("on the 1st", date(2026, 4, 1)),  # already past this month -> next month
        ("5 march", date(2026, 3, 5)),
        ("march 1", date(2027, 3, 1)),
        ("2026-03-03", date(2026, 3, 3)),
    ],
)
def test_dates(text, expected) -> None:
    result = resolve_date(text, today=TODAY)
    assert result.kind == "exact", (text, result)
    assert result.value == expected


@pytest.mark.parametrize("text", ["", None, "soon", "whenever", "31st feb"])
def test_unparseable_dates(text) -> None:
    assert resolve_date(text, today=TODAY).kind == "unparseable"
