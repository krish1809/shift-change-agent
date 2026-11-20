"""Company and policy lookups. Policy is always loaded by the caller's own company_id."""

from __future__ import annotations

from psycopg import AsyncConnection

from app.domain.models import Company, CompanyContext, Policy


async def get_company(conn: AsyncConnection, company_id: str) -> Company | None:
    row = await (await conn.execute("SELECT * FROM companies WHERE id = %s", (company_id,))).fetchone()
    return Company(**row) if row else None


async def get_policy(conn: AsyncConnection, company_id: str) -> Policy | None:
    row = await (
        await conn.execute("SELECT * FROM company_policies WHERE company_id = %s", (company_id,))
    ).fetchone()
    return Policy(**row) if row else None


async def get_company_context(conn: AsyncConnection, company_id: str) -> CompanyContext | None:
    company = await get_company(conn, company_id)
    policy = await get_policy(conn, company_id)
    if company is None or policy is None:
        return None
    return CompanyContext(company=company, policy=policy)
