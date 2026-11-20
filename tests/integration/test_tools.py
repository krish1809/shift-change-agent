"""Tool layer: gate rules, handlers, executor failures and company-scoped wording."""

from __future__ import annotations

import pytest

from app.db.repositories import roster, trips
from app.tools.faults import FaultInjector
from app.tools.gate import GateError, validate_call
from tests.conftest import d0
from tests.tool_helpers import call, make_ctx

RS = "request_shift_change"


async def _entry_trip(pool, company, employee, entry_id) -> str | None:
    async with pool.connection() as conn:
        return (await roster.get_entry(conn, company, employee, entry_id)).trip_id


async def _seats(pool, company, trip_id) -> int:
    async with pool.connection() as conn:
        return (await trips.get_trip(conn, company, trip_id)).seats_taken


# ------------------------------------------------------------------ gate (no DB needed)


@pytest.mark.no_db
def test_gate_rejects_unknown_tool_and_mode() -> None:
    assert validate_call({"name": "drop_database", "args": {}}, mode="normal", user_texts=[]).code == "INVALID_TOOL"
    assert validate_call({"name": RS, "args": {}}, mode="pending", user_texts=[]).code == "INVALID_TOOL"


@pytest.mark.no_db
@pytest.mark.parametrize("field", ["employee_id", "company_id", "role", "trip_id"])
def test_gate_rejects_identity_fields(field) -> None:
    result = validate_call(
        {"name": RS, "args": {"change_type": "reschedule", field: "G001"}}, mode="normal", user_texts=["x"]
    )
    assert isinstance(result, GateError) and result.code == "INVALID_ARGS"


@pytest.mark.no_db
def test_gate_demotes_invented_time_to_suggestion() -> None:
    result = validate_call(
        {"name": RS, "args": {"change_type": "reschedule", "time_text": "20:30"}},
        mode="normal",
        user_texts=["later please"],
    )
    assert result.ungrounded == ["time_text"]
    assert result.args.time_text is None and result.args.suggested_time == "20:30"


@pytest.mark.no_db
def test_gate_keeps_grounded_spans_case_and_space_insensitive() -> None:
    result = validate_call(
        {"name": RS, "args": {"change_type": "reschedule", "time_text": "8:30  PM", "date_text": "Tonight"}},
        mode="normal",
        user_texts=["move my drop tonight to 8:30 pm"],
    )
    assert result.ungrounded == [] and result.args.time_text == "8:30  PM"


@pytest.mark.no_db
def test_gate_only_looks_at_last_three_user_messages() -> None:
    texts = ["9 pm please", "hi", "ok", "thanks"]
    result = validate_call({"name": RS, "args": {"time_text": "9 pm"}}, mode="normal", user_texts=texts)
    assert result.ungrounded == ["time_text"]


# ------------------------------------------------------------------ handlers


async def test_happy_path_applies_and_confirms(pool, clock) -> None:
    ctx = await make_ctx(pool, clock, "acme", "A001")
    result, reply = await call(
        ctx,
        "Move my drop today to 9 pm",
        RS,
        change_type="reschedule",
        trip_type="drop",
        date_text="today",
        time_text="9 pm",
    )
    assert result.status == "APPLIED"
    assert "9:00 PM" in reply and "7:00 PM seat has been released" in reply and "REQ-" in reply
    assert await _entry_trip(pool, "acme", "A001", "RE-A001-D0-DROP") == "ACME-R1-DROP-D0-2100"
    assert await _seats(pool, "acme", "ACME-R1-DROP-D0-2100") == 3
    assert await _seats(pool, "acme", "ACME-R1-DROP-D0-1900") == 2


async def test_full_cab_rejected_with_only_own_company_options(pool, clock) -> None:
    ctx = await make_ctx(pool, clock, "acme", "A001")
    result, reply = await call(
        ctx,
        "Can I take the 8 pm drop instead?",
        RS,
        change_type="reschedule",
        trip_type="drop",
        date_text="today",
        time_text="8 pm",
    )
    assert result.status == "REJECTED"
    assert all(t in reply for t in ("8:30 PM", "9:00 PM", "6:00 PM"))
    assert "Globex" not in reply and "KA-03" not in reply
    assert await _entry_trip(pool, "acme", "A001", "RE-A001-D0-DROP") == "ACME-R1-DROP-D0-1900"
    assert await _seats(pool, "globex", "GLX-R1-DROP-D0-2000") == 5


async def test_globex_cutoff_is_hard_and_mentions_only_globex(pool, clock) -> None:
    clock.set(d0("18:45"))
    ctx = await make_ctx(pool, clock, "globex", "G001")
    result, reply = await call(
        ctx,
        "Can you push my drop tonight to 9 pm?",
        RS,
        change_type="reschedule",
        trip_type="drop",
        date_text="tonight",
        time_text="9 pm",
    )
    assert result.status == "REJECTED"
    for needle in ("60 minutes", "7:30 PM", "080-4000-2222"):
        assert needle in reply
    assert "Acme" not in reply and "2 hours" not in reply and "manager" not in reply
    assert await _entry_trip(pool, "globex", "G001", "RE-G001-D0-DROP") == "GLX-R1-DROP-D0-1930"


async def test_acme_cutoff_offers_escalation_and_acme_numbers_only(pool, clock) -> None:
    clock.set(d0("17:30"))
    ctx = await make_ctx(pool, clock, "acme", "A001")
    result, reply = await call(
        ctx,
        "move my drop to 8:30 pm",
        RS,
        change_type="reschedule",
        trip_type="drop",
        date_text="today",
        time_text="8:30 pm",
    )
    assert result.status == "NEEDS_APPROVAL" and ctx.pending_offer["kind"] == "escalate"
    assert "2 hours" in reply and "5:00 PM" in reply and "Globex" not in reply and "60 minutes" not in reply
    assert await _entry_trip(pool, "acme", "A001", "RE-A001-D0-DROP") == "ACME-R1-DROP-D0-1900"


async def test_boundary_inclusive_at_deadline(pool, clock) -> None:
    clock.set(d0("17:00"))
    ctx = await make_ctx(pool, clock, "acme", "A001")
    result, _ = await call(
        ctx,
        "move my drop to 8:30 pm",
        RS,
        change_type="reschedule",
        trip_type="drop",
        date_text="today",
        time_text="8:30 pm",
    )
    assert result.status == "APPLIED"


async def test_yes_creates_pending_request_once(pool, clock) -> None:
    clock.set(d0("17:30"))
    ctx = await make_ctx(pool, clock, "acme", "A001")
    await call(
        ctx,
        "move my drop to 8:30 pm",
        RS,
        change_type="reschedule",
        trip_type="drop",
        date_text="today",
        time_text="8:30 pm",
    )
    result, reply = await call(ctx, "yes, ask my manager", "respond_to_offer", accept=True)
    assert result.status == "PENDING_APPROVAL" and ctx.pending_request_id == result.request["id"]
    assert "Sent to your transport manager" in reply and "7:00 PM" in reply
    # accepting again must not create a second request
    ctx.pending_offer = {
        "kind": "escalate",
        "entry_id": "RE-A001-D0-DROP",
        "change_type": "reschedule",
        "target_trip_id": "ACME-R1-DROP-D0-2030",
    }
    again, _ = await call(ctx, "yes", "respond_to_offer", accept=True)
    assert again.status == "DUPLICATE"
    async with pool.connection() as conn:
        n = (await (await conn.execute("SELECT count(*) AS n FROM shift_requests")).fetchone())["n"]
    assert n == 1


async def test_decline_offer_changes_nothing(pool, clock) -> None:
    clock.set(d0("17:30"))
    ctx = await make_ctx(pool, clock, "acme", "A001")
    await call(
        ctx,
        "move my drop to 8:30 pm",
        RS,
        change_type="reschedule",
        trip_type="drop",
        date_text="today",
        time_text="8:30 pm",
    )
    result, reply = await call(ctx, "no thanks", "respond_to_offer", accept=False)
    assert result.status == "NEEDS_CLARIFICATION" and "still booked" in reply and ctx.pending_offer is None


async def test_clarify_then_follow_up_uses_draft(pool, clock) -> None:
    clock.set(d0("14:00"))
    ctx = await make_ctx(pool, clock, "acme", "A001")
    first, reply = await call(ctx, "can I go a bit late today?", RS, change_type="reschedule", date_text="today")
    assert first.status == "NEEDS_CLARIFICATION"
    assert all(t in reply for t in ("8:30 PM", "9:00 PM", "10:00 PM"))
    second, _ = await call(ctx, "make it 9 instead", RS, change_type="reschedule", time_text="9")
    assert second.status == "APPLIED"
    assert await _entry_trip(pool, "acme", "A001", "RE-A001-D0-DROP") == "ACME-R1-DROP-D0-2100"


async def test_option_reference_resolves_against_offered_options(pool, clock) -> None:
    for ref, expected in (
        ("2", "ACME-R1-DROP-D0-2100"),
        ("the second one", "ACME-R1-DROP-D0-2100"),
        ("the 8:30 one", "ACME-R1-DROP-D0-2030"),
        ("9 pm one", "ACME-R1-DROP-D0-2100"),
    ):
        async with pool.connection() as conn:
            from app.db.seed import reseed
            from tests.conftest import BASE_DATE

            await reseed(conn, BASE_DATE)
        ctx = await make_ctx(pool, clock, "acme", "A001")
        await call(
            ctx, "8 pm please", RS, change_type="reschedule", trip_type="drop", date_text="today", time_text="8 pm"
        )
        assert [o["time"] for o in ctx.offered_options][:2] == ["8:30 PM", "9:00 PM"]
        result, _ = await call(ctx, ref, RS, change_type="reschedule", option_ref=ref)
        assert result.status == "APPLIED", ref
        assert await _entry_trip(pool, "acme", "A001", "RE-A001-D0-DROP") == expected, ref


async def test_option_of_another_company_never_resolves(pool, clock) -> None:
    ctx = await make_ctx(
        pool,
        clock,
        "acme",
        "A001",
        offered_options=[
            {"ref": 1, "trip_id": "ACME-R1-DROP-D0-2100", "time": "9:00 PM", "seats_left": 2, "needs_approval": []}
        ],
    )
    result, _ = await call(
        ctx,
        "put me on GLX-R1-DROP-D0-2000",
        RS,
        change_type="reschedule",
        trip_type="drop",
        date_text="today",
        option_ref="GLX-R1-DROP-D0-2000",
    )
    assert result.status != "APPLIED"
    assert await _entry_trip(pool, "acme", "A001", "RE-A001-D0-DROP") == "ACME-R1-DROP-D0-1900"
    assert await _entry_trip(pool, "globex", "G001", "RE-G001-D0-DROP") == "GLX-R1-DROP-D0-1930"


async def test_invented_time_becomes_a_question_not_an_action(pool, clock) -> None:
    clock.set(d0("14:00"))
    ctx = await make_ctx(pool, clock, "acme", "A001")
    result, reply = await call(
        ctx, "later please", RS, change_type="reschedule", trip_type="drop", date_text="today", time_text="20:30"
    )
    assert result.status == "NEEDS_CLARIFICATION" and "8:30 PM" in reply
    assert await _entry_trip(pool, "acme", "A001", "RE-A001-D0-DROP") == "ACME-R1-DROP-D0-1900"
    applied, _ = await call(ctx, "yes", "respond_to_offer", accept=True)
    assert applied.status == "APPLIED"


async def test_no_trip_at_time_is_not_snapped(pool, clock) -> None:
    ctx = await make_ctx(pool, clock, "acme", "A001")
    result, reply = await call(
        ctx,
        "30 mins later",
        RS,
        change_type="reschedule",
        trip_type="drop",
        date_text="today",
        time_text="30 mins later",
    )
    assert result.status == "REJECTED" and "no drop at 7:30 PM" in reply
    assert await _entry_trip(pool, "acme", "A001", "RE-A001-D0-DROP") == "ACME-R1-DROP-D0-1900"


async def test_ambiguous_bare_hour_asks(pool, clock) -> None:
    clock.set(d0("20:00"))
    ctx = await make_ctx(pool, clock, "acme", "A001")
    result, _ = await call(ctx, "change my cab tomorrow", RS, change_type="reschedule", date_text="tomorrow")
    assert result.template == "clarify_trip_type" and ctx.draft["date"] == "2026-03-03"
    result, _ = await call(ctx, "the drop", RS, change_type="reschedule", trip_type="drop")
    assert result.template == "clarify_time"
    result, _ = await call(ctx, "8 pm", RS, change_type="reschedule", time_text="8 pm")
    assert result.status == "APPLIED"
    assert await _entry_trip(pool, "acme", "A001", "RE-A001-D1-DROP") == "ACME-R1-DROP-D1-2000"


async def test_trip_type_ambiguity_across_days_globex(pool, clock) -> None:
    clock.set(d0("8:00"))
    ctx = await make_ctx(pool, clock, "globex", "G001")
    result, _ = await call(ctx, "move it to 9", RS, change_type="reschedule", time_text="9")
    assert result.template == "clarify_trip_type"


async def test_cancel_frees_seat(pool, clock) -> None:
    clock.set(d0("20:00"))
    ctx = await make_ctx(pool, clock, "acme", "A002")
    result, reply = await call(
        ctx, "WFH tomorrow, cancel my pickup", RS, change_type="cancel", trip_type="pickup", date_text="tomorrow"
    )
    assert result.status == "APPLIED" and "cancelled" in reply
    assert await _seats(pool, "acme", "ACME-R1-PICK-D1-0900") == 2


async def test_cancel_after_globex_cutoff_rejected(pool, clock) -> None:
    clock.set(d0("19:00"))
    ctx = await make_ctx(pool, clock, "globex", "G001")
    result, _ = await call(
        ctx, "cancel tonight's drop", RS, change_type="cancel", trip_type="drop", date_text="tonight"
    )
    assert result.status == "REJECTED"
    assert await _entry_trip(pool, "globex", "G001", "RE-G001-D0-DROP") == "GLX-R1-DROP-D0-1930"


async def test_already_on_trip(pool, clock) -> None:
    ctx = await make_ctx(pool, clock, "acme", "A001")
    result, _ = await call(
        ctx, "move my drop to 7 pm", RS, change_type="reschedule", trip_type="drop", date_text="today", time_text="7 pm"
    )
    assert result.status == "ALREADY_ON_TRIP"


async def test_seat_taken_while_deciding_offers_fresh_options(pool, clock) -> None:
    ctx = await make_ctx(pool, clock, "acme", "A001")
    await call(ctx, "8 pm please", RS, change_type="reschedule", trip_type="drop", date_text="today", time_text="8 pm")
    other = await make_ctx(pool, clock, "acme", "A004")
    taken, _ = await call(
        other, "8:30 pm", RS, change_type="reschedule", trip_type="drop", date_text="today", time_text="8:30 pm"
    )
    assert taken.status == "APPLIED"
    result, reply = await call(ctx, "the 8:30 one", RS, change_type="reschedule", option_ref="the 8:30 one")
    assert result.status == "SEAT_TAKEN" and "filled up" in reply and "9:00 PM" in reply


async def test_daily_limit_globex_escalates(pool, clock) -> None:
    ctx = await make_ctx(pool, clock, "globex", "G003")
    for target in ("8 pm", "9 pm"):
        result, _ = await call(
            ctx,
            f"move my drop to {target}",
            RS,
            change_type="reschedule",
            trip_type="drop",
            date_text="today",
            time_text=target,
        )
        assert result.status == "APPLIED", target
    third, _ = await call(
        ctx,
        "move my drop to 7:30 pm",
        RS,
        change_type="reschedule",
        trip_type="drop",
        date_text="today",
        time_text="7:30 pm",
    )
    assert third.status == "NEEDS_APPROVAL" and third.params["violations"][0]["code"] == "DAILY_CHANGE_LIMIT"


async def test_roster_policy_and_options_reads(pool, clock) -> None:
    ctx = await make_ctx(pool, clock, "acme", "A001")
    roster_result, reply = await call(ctx, "what time is my pickup tomorrow?", "get_my_roster", date_text="tomorrow")
    assert "9:00 AM" in reply
    _, policy_reply = await call(ctx, "what's the cut-off?", "get_my_policy")
    assert "2 hours" in policy_reply and "Acme Tech" in policy_reply and "Globex" not in policy_reply
    gctx = await make_ctx(pool, clock, "globex", "G001")
    _, globex_reply = await call(gctx, "what's the cut-off?", "get_my_policy")
    assert "60 minutes" in globex_reply and "Globex Finance" in globex_reply and "Acme" not in globex_reply
    _, options_reply = await call(
        ctx,
        "what drops are there after 8 tonight?",
        "check_cab_options",
        trip_type="drop",
        date_text="tonight",
        time_text="8",
    )
    assert "8:30 PM" in options_reply and "Globex" not in options_reply


# ------------------------------------------------------------------ executor failures


async def test_read_timeout_says_nothing_changed(pool, clock) -> None:
    faults = FaultInjector("check_cab_options=timeout")
    ctx = await make_ctx(pool, clock, "acme", "A001", faults=faults, tool_timeout_s=0.2)
    result, reply = await call(
        ctx,
        "What drops are there after 8 tonight?",
        "check_cab_options",
        trip_type="drop",
        date_text="tonight",
        time_text="8",
    )
    assert result.status == "TOOL_UNAVAILABLE"
    assert "nothing was changed" in reply and "7:00 PM" in reply and "Traceback" not in reply


async def test_write_fail_once_retries_and_applies_once(pool, clock) -> None:
    ctx = await make_ctx(pool, clock, "acme", "A001", faults=FaultInjector(f"{RS}=fail_once"))
    result, _ = await call(
        ctx, "move my drop to 9 pm", RS, change_type="reschedule", trip_type="drop", date_text="today", time_text="9 pm"
    )
    assert result.status == "APPLIED"
    async with pool.connection() as conn:
        n = (await (await conn.execute("SELECT count(*) AS n FROM shift_requests")).fetchone())["n"]
    assert n == 1


async def test_write_that_commits_after_timeout_is_reconciled(pool, clock) -> None:
    ctx = await make_ctx(pool, clock, "acme", "A001", faults=FaultInjector(f"{RS}=slow:1500"), tool_timeout_s=0.5)
    result, reply = await call(
        ctx, "move my drop to 9 pm", RS, change_type="reschedule", trip_type="drop", date_text="today", time_text="9 pm"
    )
    assert result.status == "OUTCOME_RECONCILED" and "9:00 PM" in reply and "nothing was changed" not in reply
    assert await _entry_trip(pool, "acme", "A001", "RE-A001-D0-DROP") == "ACME-R1-DROP-D0-2100"
    async with pool.connection() as conn:
        n = (await (await conn.execute("SELECT count(*) AS n FROM shift_requests")).fetchone())["n"]
    assert n == 1


async def test_write_error_twice_reports_unavailable_and_verifies_unchanged(pool, clock) -> None:
    ctx = await make_ctx(pool, clock, "acme", "A001", faults=FaultInjector(f"{RS}=error"))
    result, reply = await call(
        ctx, "move my drop to 9 pm", RS, change_type="reschedule", trip_type="drop", date_text="today", time_text="9 pm"
    )
    assert result.status == "TOOL_UNAVAILABLE" and "nothing was changed" in reply
    assert await _entry_trip(pool, "acme", "A001", "RE-A001-D0-DROP") == "ACME-R1-DROP-D0-1900"
    async with pool.connection() as conn:
        failed = (
            await (await conn.execute("SELECT count(*) AS n FROM audit_log WHERE action='TOOL_FAILED'")).fetchone()
        )["n"]
    assert failed == 1


# ------------------------------------------------------------------ status / withdraw / isolation


async def test_status_and_withdraw_only_for_own_requests(pool, clock) -> None:
    clock.set(d0("17:30"))
    ctx = await make_ctx(pool, clock, "acme", "A001")
    await call(
        ctx,
        "move my drop to 8:30 pm",
        RS,
        change_type="reschedule",
        trip_type="drop",
        date_text="today",
        time_text="8:30 pm",
    )
    created, _ = await call(ctx, "yes", "respond_to_offer", accept=True)
    req_id = created.request["id"]

    status, reply = await call(ctx, "any news?", "get_request_status")
    assert status.template == "status_pending" and req_id in reply and "080-4000-1111" in reply

    # another employee, and another company, can't see or withdraw it
    for company, emp in (("acme", "A004"), ("globex", "G001")):
        other = await make_ctx(pool, clock, company, emp)
        seen, _ = await call(other, "status", "get_request_status", request_id=req_id)
        assert seen.status == "NOT_FOUND"
        withdrawn, _ = await call(other, "withdraw", "withdraw_request", request_id=req_id)
        assert withdrawn.status == "NOT_FOUND"

    done, reply = await call(ctx, "withdraw it", "withdraw_request")
    assert done.status == "WITHDRAWN" and "7:00 PM" in reply
    async with pool.connection() as conn:
        row = await (
            await conn.execute("SELECT status, decision FROM shift_requests WHERE id=%s", (req_id,))
        ).fetchone()
    assert (row["status"], row["decision"]) == ("WITHDRAWN", "withdrawn")
