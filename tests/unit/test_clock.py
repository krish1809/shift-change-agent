from __future__ import annotations

import ast
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.domain.clock import DEFAULT_TZ, FrozenClock, OverrideClock

pytestmark = pytest.mark.no_db


def test_frozen_clock_set_and_advance() -> None:
    start = datetime(2026, 3, 2, 15, 0, tzinfo=DEFAULT_TZ)
    clock = FrozenClock(start)
    clock.advance(minutes=90)
    assert clock.now() == start + timedelta(minutes=90)
    with pytest.raises(ValueError):
        clock.set(datetime(2026, 3, 2, 15, 0))


def test_override_clock_keeps_time_flowing_from_chosen_moment() -> None:
    base = FrozenClock(datetime(2026, 3, 2, 9, 0, tzinfo=DEFAULT_TZ))
    clock = OverrideClock(base)
    clock.set(datetime(2026, 3, 2, 17, 30, tzinfo=DEFAULT_TZ))
    base.advance(minutes=5)
    assert clock.now() == datetime(2026, 3, 2, 17, 35, tzinfo=DEFAULT_TZ)
    clock.set(None)
    assert clock.now() == base.now()


def test_nothing_outside_clock_module_reads_wall_clock() -> None:
    """Invariant: business time comes only from the injected Clock."""
    offenders = []
    for path in Path("app").rglob("*.py"):
        if path.name in ("clock.py", "logging.py"):
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Attribute) and node.attr in ("now", "utcnow", "today"):
                if isinstance(node.value, ast.Name) and node.value.id in ("datetime", "date"):
                    offenders.append(f"{path}:{node.lineno}")
    assert offenders == []
