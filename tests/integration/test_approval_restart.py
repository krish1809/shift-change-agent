"""Manager approval: the pause survives a REAL restart (new app, new pool, new graph, same database)."""

from __future__ import annotations

import pytest

from app.agent import sweeper
from app.db.repositories import requests as requests_repo
from app.db.repositories import roster, trips
from app.llm.fake import ScriptedLLM
from app.main import build_app
from tests.conftest import d0
from tests.helpers import bearer, chat, make_settings, running, tool

RS = "request_shift_change"
ESCALATE_SCRIPT = [
    tool(RS, change_type="reschedule", trip_type="drop", date_text="today", time_text="8:30 pm"),
    tool("respond_to_offer", accept=True),
]


def new_app(clock, llm=None):
    """A brand-new application instance: this is what a process restart gives you."""
    return build_app(make_settings(), clock=clock, llm=llm or ScriptedLLM([]))


def manager(clock, company="acme", employee="AM01"):
    return bearer(company, employee, clock.now(), role="manager")


async def escalate(clock):
    """A001 asks for 8:30 PM at 17:30 (past Acme's cut-off) and says yes to the manager. Returns ids; app shut down."""
    clock.set(d0("17:30"))
    async with running(new_app(clock, ScriptedLLM(list(ESCALATE_SCRIPT)))) as client:
        first = (await chat(client, "acme", "A001", "move my drop to 8:30 pm", clock.now())).json()
        assert first["status"] == "needs_clarification" and "send it to your transport manager" in first["reply"]
        second = (await chat(client, "acme", "A001", "yes, ask my manager", clock.now(), first["session_id"])).json()
        assert second["status"] == "pending_approval" and second["request"]["status"] == "PENDING_APPROVAL"
        return first["session_id"], second["request"]["id"]


async def snapshot(client_app, company="acme", employee="A001", entry="RE-A001-D0-DROP", trip="ACME-R1-DROP-D0-2030"):
    async with client_app.state.pool.connection() as conn:
        e = await roster.get_entry(conn, company, employee, entry)
        t = await trips.get_trip(conn, company, trip)
    return e.trip_id, t.seats_taken


async def history(client, clock, session, company="acme", employee="A001"):
    r = await client.get(f"/chat/{session}/messages", headers=bearer(company, employee, clock.now()))
    return [m["content"] for m in r.json()["messages"]]


async def test_res01_approve_after_restart(clock) -> None:
    session, req_id = await escalate(clock)
    app = new_app(clock)  # <- restart: nothing in memory survives
    async with running(app) as client:
        r = await client.post(
            f"/manager/requests/{req_id}/decision", json={"decision": "approve"}, headers=manager(clock)
        )
        assert r.status_code == 200 and r.json()["delivered"] is True
        texts = await history(client, clock, session)
        assert any("approved" in t and "8:30 PM" in t for t in texts)
        assert await snapshot(app) == ("ACME-R1-DROP-D0-2030", 4)
        async with app.state.pool.connection() as conn:
            assert (await requests_repo.get_request(conn, "acme", req_id)).status == "APPROVED_APPLIED"
            audit = [
                r["action"]
                for r in await (
                    await conn.execute("SELECT action FROM audit_log WHERE request_id=%s ORDER BY id", (req_id,))
                ).fetchall()
            ]
        assert audit == ["ESCALATED", "DECISION_RECORDED", "RESUMED", "CHANGE_APPLIED"]


async def test_res02_reject_with_note(clock) -> None:
    session, req_id = await escalate(clock)
    app = new_app(clock)
    async with running(app) as client:
        r = await client.post(
            f"/manager/requests/{req_id}/decision",
            json={"decision": "reject", "note": "team dinner is optional"},
            headers=manager(clock),
        )
        assert r.status_code == 200
        texts = await history(client, clock, session)
        assert any("declined" in t and "team dinner is optional" in t and "7:00 PM" in t for t in texts)
        assert await snapshot(app) == ("ACME-R1-DROP-D0-1900", 3)


async def test_res03_decision_recorded_but_not_resumed_then_sweeper_resumes_exactly_once(clock) -> None:
    session, req_id = await escalate(clock)
    app1 = new_app(clock)
    async with running(app1):
        async with app1.state.pool.connection() as conn:  # "crash" right after the outbox write
            await requests_repo.record_decision(
                conn, "acme", req_id, decision="approved", decided_by="AM01", note=None, at=clock.now()
            )
    app2 = new_app(clock)  # restart
    async with running(app2) as client:
        first = await sweeper.run_once(app2)
        again = await sweeper.run_once(app2)
        assert first["resumed"] == 1 and again["resumed"] == 0
        assert await snapshot(app2) == ("ACME-R1-DROP-D0-2030", 4)
        texts = await history(client, clock, session)
        assert sum("approved" in t for t in texts) == 1  # one notification, not two


async def test_res04_seat_taken_meanwhile_is_reported_honestly(clock) -> None:
    session, req_id = await escalate(clock)
    llm = ScriptedLLM([tool(RS, change_type="reschedule", trip_type="drop", date_text="today", time_text="8:30 pm")])
    app = new_app(clock, llm)
    async with running(app) as client:
        # A002 is on the 9 PM drop: deadline 6:30 PM, so at 5:30 PM they may still take the last 8:30 seat.
        taken = (await chat(client, "acme", "A002", "move my drop to 8:30 pm", clock.now())).json()
        assert taken["request"]["status"] == "APPLIED"
        r = await client.post(
            f"/manager/requests/{req_id}/decision", json={"decision": "approve"}, headers=manager(clock)
        )
        assert r.status_code == 200
        texts = await history(client, clock, session)
        last = texts[-1]
        assert "approved" in last and "nothing was changed" in last and "still booked" in last
        assert await snapshot(app) == ("ACME-R1-DROP-D0-1900", 4)  # A001 unchanged; the seat is A002's
        async with app.state.pool.connection() as conn:
            assert (await requests_repo.get_request(conn, "acme", req_id)).status == "APPROVED_BUT_UNAVAILABLE"


async def test_res05_expiry(clock) -> None:
    session, req_id = await escalate(clock)
    clock.set(d0("18:31"))  # 7:00 PM trip minus Acme's 30 min approval timeout = 6:30 PM
    app = new_app(clock)
    async with running(app) as client:
        done = await sweeper.run_once(app)
        assert done["expired"] == 1
        texts = await history(client, clock, session)
        assert "expired" in texts[-1] and "7:00 PM" in texts[-1]
        assert await snapshot(app) == ("ACME-R1-DROP-D0-1900", 3)
        r = await client.post(
            f"/manager/requests/{req_id}/decision", json={"decision": "approve"}, headers=manager(clock)
        )
        assert r.status_code == 409


async def test_not_yet_overdue_request_is_left_alone(clock) -> None:
    await escalate(clock)
    clock.set(d0("18:29"))
    app = new_app(clock)
    async with running(app):
        assert (await sweeper.run_once(app)) == {"resumed": 0, "expired": 0, "redriven": 0}


async def test_res06_status_while_pending_creates_nothing_and_stays_paused(clock) -> None:
    session, req_id = await escalate(clock)
    llm = ScriptedLLM([tool("get_request_status")])
    app = new_app(clock, llm)
    async with running(app) as client:
        r = (await chat(client, "acme", "A001", "any update?", clock.now(), session)).json()
        assert "with your transport manager" in r["reply"] and req_id in r["reply"] and "080-4000-1111" in r["reply"]
        assert r["meta"]["llm_calls"] <= 1
        async with app.state.pool.connection() as conn:
            assert (await (await conn.execute("SELECT count(*) AS n FROM shift_requests")).fetchone())["n"] == 1
        # still paused: the manager's decision still reaches this very conversation afterwards
        d = await client.post(
            f"/manager/requests/{req_id}/decision", json={"decision": "approve"}, headers=manager(clock)
        )
        assert d.json()["delivered"] is True


async def test_other_change_requests_while_pending_get_a_polite_answer(clock) -> None:
    session, req_id = await escalate(clock)
    llm = ScriptedLLM([tool(RS, change_type="reschedule", trip_type="pickup", date_text="tomorrow", time_text="8 am")])
    app = new_app(clock, llm)
    async with running(app) as client:
        r = (await chat(client, "acme", "A001", "also move my pickup tomorrow to 8 am", clock.now(), session)).json()
        assert "waiting for your transport manager" in r["reply"] and req_id in r["reply"]
        assert r["meta"]["llm_calls"] == 1
        async with app.state.pool.connection() as conn:
            assert (await (await conn.execute("SELECT count(*) AS n FROM shift_requests")).fetchone())["n"] == 1


async def test_res07_withdraw_then_manager_decision_conflicts(clock) -> None:
    session, req_id = await escalate(clock)
    llm = ScriptedLLM([tool("withdraw_request")])
    app = new_app(clock, llm)
    async with running(app) as client:
        w = (await chat(client, "acme", "A001", "withdraw it", clock.now(), session)).json()
        assert "withdrawn" in w["reply"] and "7:00 PM" in w["reply"]
        r = await client.post(
            f"/manager/requests/{req_id}/decision", json={"decision": "approve"}, headers=manager(clock)
        )
        assert r.status_code == 409
        assert await snapshot(app) == ("ACME-R1-DROP-D0-1900", 3)


async def test_res08_double_approve_second_is_409_and_seat_moves_once(clock) -> None:
    session, req_id = await escalate(clock)
    app = new_app(clock)
    async with running(app) as client:
        first = await client.post(
            f"/manager/requests/{req_id}/decision", json={"decision": "approve"}, headers=manager(clock)
        )
        second = await client.post(
            f"/manager/requests/{req_id}/decision", json={"decision": "approve"}, headers=manager(clock)
        )
        assert (first.status_code, second.status_code) == (200, 409)
        assert await snapshot(app) == ("ACME-R1-DROP-D0-2030", 4)
        assert sum("approved" in t for t in await history(client, clock, session)) == 1


async def test_iso06_other_company_manager_gets_404_and_nothing_changes(clock) -> None:
    session, req_id = await escalate(clock)
    app = new_app(clock)
    async with running(app) as client:
        r = await client.post(
            f"/manager/requests/{req_id}/decision",
            json={"decision": "approve"},
            headers=manager(clock, "globex", "GM01"),
        )
        assert r.status_code == 404
        listing = await client.get("/manager/requests", headers=manager(clock, "globex", "GM01"))
        assert listing.json()["requests"] == []
        mine = await client.get("/manager/requests", headers=manager(clock))
        assert [x["id"] for x in mine.json()["requests"]] == [req_id]
        async with app.state.pool.connection() as conn:
            assert (await requests_repo.get_request(conn, "acme", req_id)).status == "PENDING_APPROVAL"
        assert await snapshot(app) == ("ACME-R1-DROP-D0-1900", 3)


async def test_employee_token_cannot_decide(clock) -> None:
    session, req_id = await escalate(clock)
    async with running(new_app(clock)) as client:
        r = await client.post(
            f"/manager/requests/{req_id}/decision",
            json={"decision": "approve"},
            headers=bearer("acme", "A001", clock.now()),
        )
        assert r.status_code == 403


async def test_thread_continues_normally_after_the_decision(clock) -> None:
    session, req_id = await escalate(clock)
    app = new_app(clock, ScriptedLLM([tool("get_my_policy")]))
    async with running(app) as client:
        await client.post(f"/manager/requests/{req_id}/decision", json={"decision": "approve"}, headers=manager(clock))
        r = (await chat(client, "acme", "A001", "what's the cut-off?", clock.now(), session)).json()
        assert "Acme Tech" in r["reply"] and r["meta"]["llm_calls"] == 1


async def test_pending_request_whose_pause_was_never_saved_is_redriven(clock) -> None:
    """Crash between inserting the request and persisting the interrupt: the sweeper repairs the thread."""
    from app.domain.booking import ChangeSpec, idempotency_key, record_request

    clock.set(d0("17:30"))
    app1 = new_app(clock)
    thread = "acme:A001:33333333-3333-4333-8333-333333333333"
    async with running(app1):
        spec = ChangeSpec("acme", "A001", "RE-A001-D0-DROP", "reschedule", "ACME-R1-DROP-D0-2030", 1, thread)
        async with app1.state.pool.connection() as conn:
            await record_request(
                conn,
                spec,
                status="PENDING_APPROVAL",
                from_trip_id="ACME-R1-DROP-D0-1900",
                violations=[],
                now=clock.now(),
                key=idempotency_key(spec, salt="escalate"),
            )
    app2 = new_app(clock)
    async with running(app2) as client:
        assert (await sweeper.run_once(app2))["redriven"] == 1
        assert (await sweeper.run_once(app2))["redriven"] == 0
        listing = await client.get("/manager/requests", headers=manager(clock))
        req_id = listing.json()["requests"][0]["id"]
        d = await client.post(
            f"/manager/requests/{req_id}/decision", json={"decision": "approve"}, headers=manager(clock)
        )
        assert d.json()["delivered"] is True
        assert await snapshot(app2) == ("ACME-R1-DROP-D0-2030", 4)


@pytest.mark.parametrize("body", [{"decision": "maybe"}, {"decision": "approve", "company_id": "globex"}, {}])
async def test_decision_body_is_strict(clock, body) -> None:
    session, req_id = await escalate(clock)
    async with running(new_app(clock)) as client:
        r = await client.post(f"/manager/requests/{req_id}/decision", json=body, headers=manager(clock))
        assert r.status_code == 422
