"""One conversation turn at a time per thread, across all workers, using a Postgres advisory lock.

Employee messages, manager resumes and the sweeper all take this lock, so they can never interleave
on the same thread. The lock is session-level, so we hold one pooled connection for its lifetime.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from psycopg_pool import AsyncConnectionPool


class ThreadBusy(Exception):
    pass


@asynccontextmanager
async def thread_lock(pool: AsyncConnectionPool, thread_id: str, wait_ms: int) -> AsyncIterator[None]:
    deadline = asyncio.get_running_loop().time() + wait_ms / 1000
    async with pool.connection() as conn:
        acquired = False
        while True:
            row = await (
                await conn.execute("SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS ok", (thread_id,))
            ).fetchone()
            acquired = bool(row and row["ok"])
            if acquired or asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(0.05)
        if not acquired:
            raise ThreadBusy(thread_id)
        try:
            yield
        finally:
            # Must be released before the connection goes back to the pool (even if cancelled).
            await asyncio.shield(conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (thread_id,)))
