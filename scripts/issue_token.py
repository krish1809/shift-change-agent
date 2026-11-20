"""Mint a dev JWT for a seeded employee or manager.

python -m scripts.issue_token --company acme --employee A001
"""

from __future__ import annotations

import argparse
import asyncio

from app.auth.jwt import issue_token
from app.db.pool import open_pool
from app.db.repositories import employees
from app.domain.clock import SystemClock
from app.settings import get_settings


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--company", required=True)
    parser.add_argument("--employee", required=True)
    args = parser.parse_args()

    settings = get_settings()
    pool = await open_pool(settings.database_url, min_size=1, max_size=1)
    try:
        async with pool.connection() as conn:
            employee = await employees.get_employee(conn, args.company, args.employee)
    finally:
        await pool.close()
    if employee is None:
        raise SystemExit(f"no employee {args.employee!r} in company {args.company!r}")
    print(
        issue_token(
            employee.company_id,
            employee.id,
            employee.role,
            secret=settings.jwt_secret,
            now=SystemClock().now(),
            ttl_minutes=settings.jwt_ttl_minutes,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
