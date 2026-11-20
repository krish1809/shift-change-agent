"""Shared fixtures. Tests use the real Postgres (`shift_test`), a frozen clock and fake LLMs only."""
from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import date, datetime

import pytest
from psycopg_pool import AsyncConnectionPool

from app.db.pool import apply_schema, open_pool
from app.db.seed import reseed
from app.domain.clock import DEFAULT_TZ, FrozenClock

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    os.environ.get("DATABASE_URL", "postgresql://shift:shift@localhost:55432/shift_test"),
)
BASE_DATE = date(2026, 3, 2)


def d0(hhmm: str, day: int = 0) -> datetime:
    """Local Asia/Kolkata datetime on D0 (+day)."""
    hour, minute = (int(x) for x in hhmm.split(":"))
    return datetime(BASE_DATE.year, BASE_DATE.month, BASE_DATE.day + day, hour, minute, tzinfo=DEFAULT_TZ)


@pytest.fixture(scope="session")
async def pool() -> AsyncIterator[AsyncConnectionPool]:
    pool = await open_pool(TEST_DATABASE_URL, min_size=2, max_size=30)
    await apply_schema(pool)
    # The checkpointer tables must exist before any app instance runs; create them once here.
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    await AsyncPostgresSaver(pool).setup()
    yield pool
    await pool.close()


@pytest.fixture(autouse=True)
async def _fresh_data(request: pytest.FixtureRequest) -> None:
    if "no_db" in request.keywords:
        return
    db_pool: AsyncConnectionPool = request.getfixturevalue("pool")
    async with db_pool.connection() as conn:
        await reseed(conn, BASE_DATE)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(d0("15:00"))


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "no_db: test does not need the database")
