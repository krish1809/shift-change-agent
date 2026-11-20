"""Phase 2: identity comes only from a valid token."""

from __future__ import annotations

from datetime import timedelta

import jwt
import pytest

from app.auth.jwt import AUDIENCE, ISSUER
from app.main import build_app
from tests.helpers import SECRET, bearer, make_settings, running, token


@pytest.fixture
async def client(clock):
    async with running(build_app(make_settings(), clock=clock)) as c:
        yield c


async def test_valid_token_reads_own_roster(client, clock) -> None:
    r = await client.get("/me/roster", headers=bearer("acme", "A001", clock.now()))
    assert r.status_code == 200
    assert {e["id"] for e in r.json()["entries"]} == {"RE-A001-D0-DROP", "RE-A001-D1-PICK", "RE-A001-D1-DROP"}


async def test_roster_date_filter(client, clock) -> None:
    r = await client.get("/me/roster?date=2026-03-03", headers=bearer("acme", "A001", clock.now()))
    assert {e["id"] for e in r.json()["entries"]} == {"RE-A001-D1-PICK", "RE-A001-D1-DROP"}


async def test_missing_token_401(client) -> None:
    r = await client.get("/me/roster")
    assert r.status_code == 401
    assert r.json()["status"] == "error" and r.json()["meta"]["correlation_id"]


async def test_expired_token_401(client, clock) -> None:
    old = token("acme", "A001", "employee", clock.now() - timedelta(hours=9))
    r = await client.get("/me/roster", headers={"Authorization": f"Bearer {old}"})
    assert r.status_code == 401


async def test_bad_signature_401(client, clock) -> None:
    forged = jwt.encode(
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": "A001",
            "company_id": "acme",
            "role": "employee",
            "iat": int(clock.now().timestamp()),
            "exp": int(clock.now().timestamp()) + 600,
        },
        "some-other-secret-that-is-long-enough-000000",
        algorithm="HS256",
    )
    r = await client.get("/me/roster", headers={"Authorization": f"Bearer {forged}"})
    assert r.status_code == 401


async def test_tampered_payload_401(client, clock) -> None:
    good = token("acme", "A001", "employee", clock.now())
    header, payload, sig = good.split(".")
    other = token("globex", "G001", "employee", clock.now()).split(".")[1]
    r = await client.get("/me/roster", headers={"Authorization": f"Bearer {header}.{other}.{sig}"})
    assert r.status_code == 401


async def test_alg_none_401(client, clock) -> None:
    unsigned = jwt.encode(
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": "A001",
            "company_id": "acme",
            "role": "employee",
            "iat": int(clock.now().timestamp()),
            "exp": int(clock.now().timestamp()) + 600,
        },
        None,
        algorithm="none",
    )
    r = await client.get("/me/roster", headers={"Authorization": f"Bearer {unsigned}"})
    assert r.status_code == 401


async def test_missing_company_claim_401(client, clock) -> None:
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "A001",
        "role": "employee",
        "iat": int(clock.now().timestamp()),
        "exp": int(clock.now().timestamp()) + 600,
    }
    r = await client.get(
        "/me/roster", headers={"Authorization": f"Bearer {jwt.encode(claims, SECRET, algorithm='HS256')}"}
    )
    assert r.status_code == 401


async def test_unknown_employee_401(client, clock) -> None:
    r = await client.get("/me/roster", headers=bearer("acme", "G001", clock.now()))
    assert r.status_code == 401


async def test_role_mismatch_with_db_401(client, clock) -> None:
    r = await client.get("/me/roster", headers=bearer("acme", "A001", clock.now(), role="manager"))
    assert r.status_code == 401


async def test_employee_on_manager_route_403(client, clock) -> None:
    r = await client.get("/manager/requests", headers=bearer("acme", "A001", clock.now()))
    assert r.status_code == 403


async def test_manager_on_employee_route_403(client, clock) -> None:
    r = await client.get("/me/roster", headers=bearer("acme", "AM01", clock.now(), role="manager"))
    assert r.status_code == 403


async def test_dev_token_looks_up_role(client) -> None:
    r = await client.post("/dev/token", json={"company_id": "acme", "employee_id": "AM01"})
    assert r.status_code == 200 and r.json()["role"] == "manager"
    r = await client.post("/dev/token", json={"company_id": "acme", "employee_id": "A001", "role": "manager"})
    assert r.status_code == 422  # role can't be chosen by the caller


async def test_dev_token_unknown_employee_404(client) -> None:
    r = await client.post("/dev/token", json={"company_id": "acme", "employee_id": "G001"})
    assert r.status_code == 404


async def test_dev_endpoints_disabled_outside_dev(clock) -> None:
    async with running(build_app(make_settings(env="prod"), clock=clock)) as c:
        r = await c.post("/dev/token", json={"company_id": "acme", "employee_id": "A001"})
        assert r.status_code == 404
        r = await c.get("/dev/people")
        assert r.status_code == 404
