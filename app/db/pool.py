"""Async Postgres pool shared by domain repositories and the LangGraph checkpointer."""
from __future__ import annotations

from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


async def open_pool(database_url: str, *, min_size: int = 2, max_size: int = 20) -> AsyncConnectionPool:
    # autocommit + dict rows + no server-side prepares are what AsyncPostgresSaver expects.
    # Domain writes open explicit transactions (`async with conn.transaction()`).
    pool = AsyncConnectionPool(
        database_url,
        min_size=min_size,
        max_size=max_size,
        kwargs={"autocommit": True, "row_factory": dict_row, "prepare_threshold": 0},
        open=False,
    )
    await pool.open(wait=True, timeout=30)
    return pool


async def apply_schema(pool: AsyncConnectionPool) -> None:
    # One statement per execute: the pool disables prepared statements, which forbids batches.
    statements = [s.strip() for s in SCHEMA_PATH.read_text().split(";") if s.strip()]
    async with pool.connection() as conn:
        for statement in statements:
            await conn.execute(statement)
