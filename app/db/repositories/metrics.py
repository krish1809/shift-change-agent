from __future__ import annotations

from datetime import datetime

from psycopg import AsyncConnection


async def record_turn(
    conn: AsyncConnection,
    company_id: str,
    *,
    at: datetime,
    thread_id: str,
    correlation_id: str,
    latency_ms: int,
    llm_calls: int,
    tool_calls: int,
    outcome: str | None,
) -> None:
    await conn.execute(
        """INSERT INTO turn_metrics (at, company_id, thread_id, correlation_id, latency_ms, llm_calls,
                                     tool_calls, outcome)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
        (at, company_id, thread_id, correlation_id, latency_ms, llm_calls, tool_calls, outcome),
    )
