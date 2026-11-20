"""The LangGraph agent: a bounded, gated loop. The model proposes, code disposes.

ingest -> interpret(LLM) -> tool_gate -> execute -> respond_outcome -> END
              |  prose only -> guard_text -> END
              |  LLM down / garbage -> fallback -> END

After a mutating tool the model is NOT called again: a template states the outcome.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import interrupt

from app.agent import prompts, templates
from app.agent.approval_flow import finalize_request
from app.agent.context import RunContext
from app.agent.guard import check_prose
from app.agent.state import ConversationState
from app.db.repositories import audit, roster
from app.domain.formatting import fmt_day, fmt_time
from app.llm.base import BudgetExceeded, LLMError
from app.llm.rule_based import interpret as rule_based_interpret
from app.tools.context import ToolContext
from app.tools.executor import execute_tool
from app.tools.gate import GateError, ValidCall, select_call, validate_call
from app.tools.registry import tool_schemas
from app.tools.results import ToolResult

log = logging.getLogger(__name__)
Rt = Runtime[RunContext]

MAX_REPAIRS = 1


# ------------------------------------------------------------------ helpers


def _now_label(ctx: RunContext) -> str:
    now = ctx.clock.now().astimezone(ctx.company.tz)
    return f"{now.strftime('%a %d %b')}, {fmt_time(now)}"


async def _roster_lines(ctx: RunContext) -> list[str]:
    """Today + tomorrow, as the employee would describe them. Read for the caller's own identity only."""
    await ctx.faults.before("roster_repo")
    today = ctx.clock.now().astimezone(ctx.company.tz).date()
    from datetime import timedelta

    async with ctx.pool.connection() as conn:
        entries = await roster.get_entries_for_employee(
            conn, ctx.auth.company_id, ctx.auth.employee_id, [today, today + timedelta(days=1)]
        )
    lines = []
    for e in entries:
        day = fmt_day(e.service_date, today)
        lines.append(
            f"{e.trip_type} {day} {fmt_time(e.trip.scheduled_at, ctx.company.tz)}"
            if e.trip
            else f"{e.trip_type} {day} cancelled"
        )
    return lines


def _user_texts(state: ConversationState) -> list[str]:
    return [m["content"] for m in state.get("messages", []) if m["role"] == "user"]


def _history(state: ConversationState) -> list[dict[str, str]]:
    """Earlier turns only (the current user message is added separately by build_messages)."""
    past = [{"role": m["role"], "content": m["content"]} for m in state.get("messages", [])]
    return past[:-1] if past and past[-1]["role"] == "user" else past


def _remaining_s(ctx: RunContext) -> float:
    llm_cap = ctx.settings.llm_timeout_ms / 1000
    if ctx.deadline is None:
        return llm_cap
    return max(min(llm_cap, ctx.deadline - asyncio.get_running_loop().time() - 0.5), 0.2)


def _tool_context(state: ConversationState, ctx: RunContext) -> ToolContext:
    return ToolContext(
        auth=ctx.auth, pool=ctx.pool, clock=ctx.clock, company=ctx.company, policy=ctx.policy,
        employee=ctx.employee, thread_id=ctx.thread_id, faults=ctx.faults, draft=state.get("draft"),
        offered_options=list(state.get("offered_options") or []), pending_offer=state.get("pending_offer"),
        pending_request_id=state.get("pending_request_id"), recent_user_texts=_user_texts(state),
        tool_timeout_s=ctx.settings.tool_timeout_ms / 1000,
    )  # fmt: skip


def _assistant_message(ctx: RunContext, text: str) -> dict[str, str]:
    return {"role": "assistant", "content": text, "at": ctx.clock.now().isoformat()}


# ------------------------------------------------------------------ nodes


async def ingest(state: ConversationState, runtime: Rt) -> dict[str, Any]:
    ctx = runtime.context
    text = state["user_text"]
    return {
        "messages": [{"role": "user", "content": text, "at": ctx.clock.now().isoformat()}],
        "mode": "pending" if state.get("pending_request_id") else "normal",
        "roster_lines": await _roster_lines(ctx),
        "llm_response": None, "degraded": False, "repair_note": None, "repairs": 0, "call": None,
        "result": None, "fail_reason": None, "tools_called": [], "reply": None, "status": None,
        "request": None, "options": None,
    }  # fmt: skip


async def interpret(state: ConversationState, runtime: Rt) -> dict[str, Any]:
    ctx = runtime.context
    context = prompts.build_context(
        now_label=_now_label(ctx), first_name=ctx.employee.first_name, roster_lines=state.get("roster_lines", []),
        draft=state.get("draft"), offered_options=state.get("offered_options") or [],
        pending_offer=state.get("pending_offer"),
    )  # fmt: skip
    messages = prompts.build_messages(
        context=context, history=_history(state), user_text=state["user_text"], mode=state.get("mode", "normal")
    )
    if state.get("repair_note"):
        messages.append({"role": "user", "content": state["repair_note"]})
    try:
        response = await ctx.llm.invoke(
            messages, tool_schemas(state.get("mode", "normal")), timeout_s=_remaining_s(ctx)
        )
    except BudgetExceeded:
        return {"fail_reason": "budget"}
    except LLMError as exc:
        log.warning("llm unavailable, trying rule-based", extra={"fields": {"error": type(exc).__name__}})
        # Degraded mode: canonical phrasings still work with no model at all.
        fallback = rule_based_interpret(state["user_text"])
        if fallback.tool_calls or fallback.text:
            return {"llm_response": {"text": fallback.text, "tool_calls": fallback.tool_calls}, "degraded": True}
        return {"fail_reason": "llm_down"}
    return {"llm_response": {"text": response.text, "tool_calls": response.tool_calls}}


def route_after_interpret(state: ConversationState) -> Literal["tool_gate", "guard_text", "fallback"]:
    if state.get("fail_reason"):
        return "fallback"
    response = state.get("llm_response") or {}
    if response.get("tool_calls"):
        return "tool_gate"
    return "guard_text"


async def tool_gate(state: ConversationState, runtime: Rt) -> dict[str, Any]:
    ctx = runtime.context
    first, dropped = select_call((state["llm_response"] or {}).get("tool_calls", []), state.get("mode", "normal"))
    if dropped:
        log.info("extra tool calls dropped", extra={"fields": {"dropped": dropped}})
    verdict = validate_call(first or {}, mode=state.get("mode", "normal"), user_texts=_user_texts(state))
    if isinstance(verdict, GateError):
        if verdict.code == "INVALID_TOOL" and state.get("mode") == "pending":
            # They asked for something else while a request is with the manager: polite, zero extra LLM calls.
            blocked = ToolResult(
                status="PENDING_BLOCKED", template="pending_blocked",
                params={"request_id": state.get("pending_request_id") or ""},
            )  # fmt: skip
            return {"call": None, "result": blocked.model_dump()}
        repairs = state.get("repairs", 0)
        can_repair = repairs < MAX_REPAIRS and ctx.llm.budget.remaining > 0 and not state.get("degraded")
        log.warning("tool call rejected by gate", extra={"fields": {"code": verdict.code, "detail": verdict.detail}})
        if can_repair:
            note = (
                f"Your last tool call was invalid ({verdict.detail}). "
                "Call one valid tool using only its documented arguments."
            )
            return {"repair_note": note, "repairs": repairs + 1, "call": None}
        return {"fail_reason": "invalid", "call": None}
    assert isinstance(verdict, ValidCall)
    return {
        "call": {"name": verdict.name, "args": verdict.args.model_dump(), "ungrounded": verdict.ungrounded},
        "repair_note": None,
    }


def route_after_gate(state: ConversationState) -> Literal["execute", "interpret", "fallback", "respond_outcome"]:
    if state.get("call"):
        return "execute"
    if state.get("result"):
        return "respond_outcome"
    return "fallback" if state.get("fail_reason") else "interpret"


async def execute(state: ConversationState, runtime: Rt) -> dict[str, Any]:
    ctx = runtime.context
    call = state["call"] or {}
    tool_ctx = _tool_context(state, ctx)
    tool_ctx.ungrounded = set(call.get("ungrounded", []))
    from app.tools.registry import REGISTRY

    args = REGISTRY[call["name"]].args_model.model_validate(call["args"])
    result = await execute_tool(tool_ctx, call["name"], args)
    ctx.tool_calls.append(call["name"])
    update: dict[str, Any] = {
        "result": result.model_dump(),
        "tools_called": [*state.get("tools_called", []), call["name"]],
    }
    update.update(result.updates)  # draft / offered_options / pending_offer / pending_request_id
    return update


async def respond_outcome(state: ConversationState, runtime: Rt) -> dict[str, Any]:
    ctx = runtime.context
    result = ToolResult.model_validate(state["result"])
    reply = templates.render(result.template, result.params)
    return {
        "reply": reply, "status": result.envelope_status, "request": result.request, "options": result.options,
        "last_outcome": result.status, "messages": [_assistant_message(ctx, reply)],
    }  # fmt: skip


async def guard_text(state: ConversationState, runtime: Rt) -> dict[str, Any]:
    """The model answered in prose (greeting, thanks, off-topic). Let it through only if it claims nothing."""
    ctx = runtime.context
    text = ((state.get("llm_response") or {}).get("text") or "").strip()
    allowed, reason = check_prose(text, outcome_this_turn=False)
    if not allowed:
        if reason == "ungrounded_outcome_claim":
            await _audit_guard(ctx, text)
        text = templates.render("generic_help")
    return {"reply": text, "status": "ok", "last_outcome": "SMALL_TALK", "messages": [_assistant_message(ctx, text)]}


async def fallback(state: ConversationState, runtime: Rt) -> dict[str, Any]:
    """Nothing usable came back from the model. Say so plainly; nothing was changed."""
    ctx = runtime.context
    template = "fallback_llm_down" if state.get("fail_reason") == "llm_down" else "could_not_understand"
    reply = templates.render(template)
    return {"reply": reply, "status": "error", "last_outcome": "FALLBACK", "messages": [_assistant_message(ctx, reply)]}


async def await_manager(state: ConversationState, runtime: Rt) -> dict[str, Any]:
    """Pause here until a manager decides, the request expires, or the employee writes.

    interrupt() MUST be the first statement: on resume LangGraph re-runs this node from the top, so
    anything before it would run twice. The pause is stored by the Postgres checkpointer, so it
    survives a process restart.
    """
    payload = interrupt({"request_id": state.get("pending_request_id")})
    if payload.get("kind") == "employee_message":
        return {"user_text": payload["text"], "resume": None}
    return {"resume": payload}


def route_after_await(state: ConversationState) -> Literal["ingest", "apply_decision"]:
    return "apply_decision" if state.get("resume") else "ingest"


async def apply_decision(state: ConversationState, runtime: Rt) -> dict[str, Any]:
    """Re-validate against current data, apply through the booking service, tell the employee."""
    ctx = runtime.context
    payload = state.get("resume") or {}
    if payload.get("request_id") != state.get("pending_request_id"):
        log.warning("resume for a different request ignored")
        return {"resume": None}
    result = await finalize_request(ctx, payload["request_id"])
    if result is None:  # already finalised elsewhere: stay quiet, just stop waiting
        return {"resume": None, "pending_request_id": None}
    reply = templates.render(result.template, result.params)
    update: dict[str, Any] = {
        "resume": None, "reply": reply, "status": result.envelope_status, "request": result.request,
        "options": result.options, "last_outcome": result.status, "messages": [_assistant_message(ctx, reply)],
    }  # fmt: skip
    update.update(result.updates)
    return update


def route_after_respond(state: ConversationState) -> Literal["await_manager", "__end__"]:
    return "await_manager" if state.get("pending_request_id") else END


async def _audit_guard(ctx: RunContext, text: str) -> None:
    try:
        async with ctx.pool.connection() as conn:
            await audit.add_audit(
                conn,
                ctx.auth.company_id,
                at=ctx.clock.now(),
                actor_type="agent",
                actor_id="guard",
                action="GUARD_BLOCKED",
                thread_id=ctx.thread_id,
                correlation_id=ctx.correlation_id,
                payload={"blocked_text": text[:200]},
            )
    except Exception:
        log.exception("could not write GUARD_BLOCKED audit row")


# ------------------------------------------------------------------ graph


def build_graph(checkpointer: Any):  # type: ignore[no-untyped-def]
    graph = StateGraph(ConversationState, context_schema=RunContext)
    graph.add_node("ingest", ingest)
    graph.add_node("interpret", interpret)
    graph.add_node("tool_gate", tool_gate)
    graph.add_node("execute", execute)
    graph.add_node("respond_outcome", respond_outcome)
    graph.add_node("guard_text", guard_text)
    graph.add_node("fallback", fallback)
    graph.add_node("await_manager", await_manager)
    graph.add_node("apply_decision", apply_decision)

    graph.add_edge(START, "ingest")
    graph.add_edge("ingest", "interpret")
    graph.add_conditional_edges("interpret", route_after_interpret)
    graph.add_conditional_edges("tool_gate", route_after_gate)
    graph.add_edge("execute", "respond_outcome")
    graph.add_conditional_edges("respond_outcome", route_after_respond)
    graph.add_conditional_edges("await_manager", route_after_await)
    for terminal in ("guard_text", "fallback", "apply_decision"):
        graph.add_edge(terminal, END)
    return graph.compile(checkpointer=checkpointer)
