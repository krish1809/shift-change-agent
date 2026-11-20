from __future__ import annotations

from psycopg import AsyncConnection

from app.domain.models import Employee


async def get_employee(conn: AsyncConnection, company_id: str, employee_id: str) -> Employee | None:
    row = await (
        await conn.execute(
            "SELECT * FROM employees WHERE company_id = %s AND id = %s", (company_id, employee_id)
        )
    ).fetchone()
    return Employee(**row) if row else None


async def list_employees(conn: AsyncConnection, company_id: str) -> list[Employee]:
    rows = await (
        await conn.execute("SELECT * FROM employees WHERE company_id = %s ORDER BY id", (company_id,))
    ).fetchall()
    return [Employee(**r) for r in rows]
