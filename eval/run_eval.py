"""Run the evaluation set end to end through the real app (in-process) and judge every case.

    python -m eval.run_eval --mode fake            # offline: scripted model output, deterministic
    python -m eval.run_eval --mode live            # real Groq/Gemini: measures tool choice + span extraction
    python -m eval.run_eval --mode fake --filter isolation

Writes eval/results.<mode>.jsonl with EXACTLY the keys id, input, expected, actual, pass, notes.
Judging is deterministic (statuses, tool names, DB state, call budget, substrings of templated text):
no LLM-as-judge, so results are reproducible and free.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from contextlib import AsyncExitStack
from contextvars import ContextVar
from datetime import datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Any

import httpx

from app.auth.jwt import issue_token
from app.db.pool import apply_schema, open_pool
from app.db.seed import load_seed, reseed
from app.domain.clock import DEFAULT_TZ, FrozenClock
from app.llm.base import ChatLLM, LLMResponse, LLMUnavailable
from app.llm.fake import LoopingLLM, ScriptedLLM
from app.llm.openai_compat import build_llm
from app.llm.rule_based import RuleBasedLLM
from app.main import build_app
from app.settings import Settings, get_settings

D0_DATE = datetime(2026, 3, 2).date()  # the date the seed data is anchored to
ROOT = Path(__file__).parent
DEFAULT_CASES = ROOT / "cases.jsonl"
ROLES = {(e["company_id"], e["id"]): e["role"] for e in load_seed()["employees"]}
SECRET = "eval-secret-0123456789-0123456789-0123456789"


# ------------------------------------------------------------------ small helpers


def parse_when(text: str) -> datetime:
    """'D0T18:45' / 'D1T09:00' -> aware datetime on the seed date."""
    match = re.fullmatch(r"D(\d)T(\d{1,2}):(\d{2})", text)
    if not match:
        raise ValueError(f"bad time {text!r}")
    day = D0_DATE + timedelta(days=int(match[1]))
    return datetime.combine(day, dtime(int(match[2]), int(match[3])), DEFAULT_TZ)


class EvalLLM:
    """The app's model. Per-step scripts are carried in a ContextVar so concurrent requests don't collide."""

    def __init__(self, live: ChatLLM | None = None) -> None:
        self._var: ContextVar[ChatLLM | None] = ContextVar("eval_llm", default=None)
        self.live = live

    def use(self, fake_spec: Any) -> None:
        if self.live is not None:
            self._var.set(None)
        elif fake_spec == "LOOPING":
            self._var.set(LoopingLLM())
        else:
            self._var.set(ScriptedLLM(list(fake_spec or [])))

    async def invoke(self, messages, tools, *, timeout_s) -> LLMResponse:  # type: ignore[no-untyped-def]
        inner = self._var.get() or self.live
        if inner is None:
            raise LLMUnavailable("no script")
        return await inner.invoke(messages, tools, timeout_s=timeout_s)


class Harness:
    """One case's world: a running app, a frozen clock, and bookkeeping of what happened."""

    def __init__(self, settings: Settings, llm: EvalLLM, now: datetime) -> None:
        self.settings = settings
        self.llm = llm
        self.clock = FrozenClock(now)
        self.stack: AsyncExitStack | None = None
        self.app = None
        self.client: httpx.AsyncClient | None = None
        self.sessions: dict[tuple[str, str], str] = {}
        self.record: dict[str, Any] = defaultdict(list)
        self.record["http_status"] = None
        self.armed_faults: list[str] = []

    async def start(self) -> None:
        self.stack = AsyncExitStack()
        self.app = build_app(self.settings, clock=self.clock, llm=self.llm)
        await self.stack.enter_async_context(self.app.router.lifespan_context(self.app))
        transport = httpx.ASGITransport(app=self.app)
        self.client = await self.stack.enter_async_context(
            httpx.AsyncClient(transport=transport, base_url="http://eval", timeout=60)
        )

    async def stop(self) -> None:
        if self.stack is not None:
            await self.stack.aclose()
            self.stack = None

    async def restart(self) -> None:
        await self.stop()  # pool closed, sweeper cancelled, graph dropped: a real process restart
        await self.start()

    def token(self, actor: dict[str, str], *, tampered: bool = False) -> str:
        role = ROLES.get((actor["company_id"], actor["employee_id"]), "employee")
        token = issue_token(
            actor["company_id"], actor["employee_id"], actor.get("role", role),
            secret=SECRET, now=self.clock.now(),
        )  # fmt: skip
        if tampered:
            head, payload, sig = token.split(".")
            token = f"{head}.{payload}.{'A' if sig[0] != 'A' else 'B'}{sig[1:]}"
        return token

    def headers(self, actor: dict[str, str], **kw: Any) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token(actor, **kw)}"}


# ------------------------------------------------------------------ steps


async def send_chat(
    h: Harness, actor: dict[str, str], step: dict[str, Any], *, fresh_session: bool = False
) -> dict[str, Any]:
    key = (actor["company_id"], actor["employee_id"])
    body: dict[str, Any] = {"message": step["user"]}
    session = resolve_session(h, step.get("session")) or (None if fresh_session else h.sessions.get(key))
    if session:
        body["session_id"] = session
    h.llm.use(step.get("fake_llm"))
    response = await h.client.post("/chat", json=body, headers=h.headers(actor))  # type: ignore[union-attr]
    data = response.json()
    if response.status_code == 200 and data.get("session_id") and not fresh_session:
        h.sessions[key] = data["session_id"]
    return {"http": response.status_code, "data": data}


def resolve_session(h: Harness, ref: str | None) -> str | None:
    """'of:globex/G001' -> the session id that actor is currently using (to test cross-user session reuse)."""
    if ref and ref.startswith("of:"):
        company, employee = ref[3:].split("/")
        return h.sessions.get((company, employee))
    return ref


def record_turn(h: Harness, result: dict[str, Any]) -> None:
    data, rec = result["data"], h.record
    meta = data.get("meta", {})
    rec["http_status"] = result["http"]
    rec["replies"].append(data.get("reply", ""))
    rec["final_status"] = data.get("status")
    rec["llm_calls"].append(meta.get("llm_calls", 0))
    rec["latency_ms"].append(meta.get("latency_ms", 0))
    if result["http"] == 200 or meta.get("outcome"):
        rec["outcomes"].append(meta.get("outcome"))
        rec["tools_called"].append(meta.get("tools", []))


async def latest_request(h: Harness) -> dict[str, Any] | None:
    """'request: last' = the newest request still waiting for approval, else the newest request."""
    async with h.app.state.pool.connection() as conn:  # type: ignore[union-attr]
        pending = await (
            await conn.execute(
                "SELECT * FROM shift_requests WHERE status = 'PENDING_APPROVAL' "
                "ORDER BY created_at DESC, id DESC LIMIT 1"
            )
        ).fetchone()
        if pending:
            return pending
        return await (
            await conn.execute("SELECT * FROM shift_requests ORDER BY created_at DESC, id DESC LIMIT 1")
        ).fetchone()


async def request_by_id(h: Harness, request_id: str) -> dict[str, Any] | None:
    async with h.app.state.pool.connection() as conn:  # type: ignore[union-attr]
        return await (await conn.execute("SELECT * FROM shift_requests WHERE id = %s", (request_id,))).fetchone()


async def thread_last_reply(h: Harness, request: dict[str, Any]) -> str | None:
    session = request["thread_id"].split(":", 2)[2]
    actor = {"company_id": request["company_id"], "employee_id": request["employee_id"]}
    r = await h.client.get(f"/chat/{session}/messages", headers=h.headers(actor))  # type: ignore[union-attr]
    messages = r.json().get("messages", [])
    return messages[-1]["content"] if messages else None


async def after_system_step(h: Harness, before: dict[str, Any] | None) -> None:
    """After a manager decision / sweeper pass: if the last request left PENDING, record its final status + message."""
    after = await request_by_id(h, before["id"]) if before else None
    if after and before["status"] == "PENDING_APPROVAL" and after["status"] != "PENDING_APPROVAL":  # type: ignore[index]
        h.record["outcomes"].append(after["status"])
        reply = await thread_last_reply(h, after)
        if reply:
            h.record["replies"].append(reply)


async def run_step(h: Harness, step: dict[str, Any], actor: dict[str, str], live_delay: float) -> None:
    if "restart" in step:
        await h.restart()
    elif "set_clock" in step:
        h.clock.set(parse_when(step["set_clock"]))
    elif "fault" in step:
        h.app.state.faults.arm(step["tool"], step["mode"])  # type: ignore[union-attr]
        h.armed_faults.append(step["tool"])
    elif "run_sweeper" in step:
        from app.agent import sweeper

        before = await latest_request(h)
        await sweeper.run_once(h.app)  # type: ignore[arg-type]
        await after_system_step(h, before)
    elif "record_decision_only" in step or "manager_decision" in step:
        await manager_step(h, step)
    elif "concurrent" in step:
        await concurrent_step(h, step, actor)
    elif "direct_api" in step:
        await direct_api_step(h, step, actor)
    elif "user" in step:
        who = step.get("as", actor)
        result = await send_chat(h, who, step)
        record_turn(h, result)
        for tool in h.armed_faults:
            h.app.state.faults.clear(tool)  # type: ignore[union-attr]
        h.armed_faults.clear()
        if h.llm.live is not None:
            await asyncio.sleep(live_delay)
    else:
        raise ValueError(f"unknown step {step!r}")


async def manager_step(h: Harness, step: dict[str, Any]) -> None:
    request = await latest_request(h)
    if request is None:
        raise RuntimeError("no request to decide")
    manager = step["manager"]
    decision = "approved" if step["decision"] == "approve" else "rejected"
    if "record_decision_only" in step:
        from app.db.repositories import requests as requests_repo

        async with h.app.state.pool.connection() as conn:  # type: ignore[union-attr]
            await requests_repo.record_decision(
                conn, request["company_id"], request["id"], decision=decision, decided_by=manager["employee_id"],
                note=step.get("note"), at=h.clock.now(),
            )  # fmt: skip
        return
    body: dict[str, Any] = {"decision": step["decision"]}
    if step.get("note"):
        body["note"] = step["note"]
    before = request
    r = await h.client.post(  # type: ignore[union-attr]
        f"/manager/requests/{request['id']}/decision", json=body,
        headers=h.headers({**manager, "role": "manager"}),
    )  # fmt: skip
    h.record["http_status"] = r.status_code
    await after_system_step(h, before)


async def concurrent_step(h: Harness, step: dict[str, Any], default_actor: dict[str, str]) -> None:
    async def one(req: dict[str, Any]) -> dict[str, Any]:
        return await send_chat(h, req.get("actor", default_actor), req, fresh_session=True)

    results = await asyncio.gather(*(one(r) for r in step["concurrent"]))
    for result in results:
        record_turn(h, result)
        h.record["concurrent_outcomes"].append(result["data"].get("meta", {}).get("outcome"))
        # concurrent turns are judged as a set, so they don't extend the ordered per-step lists
        h.record["outcomes"].pop()
        h.record["tools_called"].pop()


async def direct_api_step(h: Harness, step: dict[str, Any], actor: dict[str, str]) -> None:
    mode = step.get("token", "actor")
    headers = {} if mode == "none" else h.headers(step.get("as", actor), tampered=(mode == "tampered"))
    path = re.sub(r"\{session:([^}]+)\}", lambda m: resolve_session(h, "of:" + m[1]) or "none", step["path"])
    r = await h.client.request(step["method"], path, json=step.get("body"), headers=headers)  # type: ignore[union-attr]
    h.record["http_status"] = r.status_code
    try:
        body = r.json()
        h.record["replies"].append(body.get("reply") or json.dumps(body, ensure_ascii=False))
    except ValueError:
        h.record["replies"].append(r.text)


# ------------------------------------------------------------------ db snapshot + judging


async def db_snapshot(h: Harness, wanted: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    async with h.app.state.pool.connection() as conn:  # type: ignore[union-attr]
        if "roster" in wanted:
            out["roster"] = {}
            for entry_id in wanted["roster"]:
                row = await (
                    await conn.execute("SELECT trip_id FROM roster_entries WHERE id = %s", (entry_id,))
                ).fetchone()
                out["roster"][entry_id] = row["trip_id"] if row else "<missing>"
        if "seats" in wanted:
            out["seats"] = {}
            for trip_id in wanted["seats"]:
                row = await (await conn.execute("SELECT seats_taken FROM trips WHERE id = %s", (trip_id,))).fetchone()
                out["seats"][trip_id] = row["seats_taken"] if row else None
        if "requests" in wanted:
            rows = await (await conn.execute("SELECT status FROM shift_requests ORDER BY created_at, id")).fetchall()
            out["requests"] = {"count": len(rows), "statuses": [r["status"] for r in rows]}
    return out


def judge(expected: dict[str, Any], actual: dict[str, Any], *, live: bool) -> tuple[bool, list[str]]:
    checks: list[str] = []
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        (checks if ok else failures).append(name if ok else f"FAILED {name} {detail}".strip())

    if "outcomes" in expected:
        check(
            "outcomes",
            actual["outcomes"] == expected["outcomes"],
            f"expected {expected['outcomes']} got {actual['outcomes']}",
        )
    if "outcomes_multiset" in expected:
        check("outcomes_multiset", Counter(actual["concurrent_outcomes"]) == Counter(expected["outcomes_multiset"]),
              f"expected {expected['outcomes_multiset']} got {actual['concurrent_outcomes']}")  # fmt: skip
    if "outcomes_multiset_any" in expected:
        got = Counter(actual["concurrent_outcomes"])
        check("outcomes_multiset_any", any(got == Counter(opt) for opt in expected["outcomes_multiset_any"]),
              f"got {actual['concurrent_outcomes']}")  # fmt: skip
    if "final_status" in expected:
        check("final_status", actual["final_status"] == expected["final_status"], f"got {actual['final_status']}")
    if "tools_called" in expected:
        want, got = expected["tools_called"], actual["tools_called"]
        if live:
            allowed = expected.get("tools_allowed_live", want)
            ok = len(got) == len(allowed) and all(
                (not a and not g) or (g and g[0] in a) for g, a in zip(got, allowed, strict=False)
            )
        else:
            ok = got == want
        check("tools", ok, f"expected {want} got {got}")
    last = actual["replies"][-1] if actual["replies"] else ""
    for i, needles in enumerate(expected.get("replies_contain", [])):
        text = actual["replies"][i] if i < len(actual["replies"]) else ""
        for needle in needles:
            check(f"reply[{i}] has '{needle}'", needle.lower() in text.lower(), f"in: {text[:120]!r}")
    for i, needles in enumerate(expected.get("replies_not_contain", [])):
        text = actual["replies"][i] if i < len(actual["replies"]) else ""
        for needle in needles:
            check(f"reply[{i}] lacks '{needle}'", needle.lower() not in text.lower(), f"in: {text[:120]!r}")
    for needle in expected.get("reply_contains", []):
        check(f"reply has '{needle}'", needle.lower() in last.lower(), f"in: {last[:160]!r}")
    for needle in expected.get("reply_not_contains", []):
        check(f"reply lacks '{needle}'", needle.lower() not in last.lower(), f"in: {last[:160]!r}")
    if "http_status" in expected:
        check("http_status", actual["http_status"] == expected["http_status"], f"got {actual['http_status']}")
    db = expected.get("db", {})
    for entry_id, trip_id in db.get("roster", {}).items():
        check(
            f"roster {entry_id}",
            actual["db"]["roster"].get(entry_id) == trip_id,
            f"got {actual['db']['roster'].get(entry_id)}",
        )
    for trip_id, seats in db.get("seats", {}).items():
        check(
            f"seats {trip_id}", actual["db"]["seats"].get(trip_id) == seats, f"got {actual['db']['seats'].get(trip_id)}"
        )
    if "requests" in db:
        got_req = actual["db"]["requests"]
        if "count" in db["requests"]:
            check("request count", got_req["count"] == db["requests"]["count"], f"got {got_req['count']}")
        if "statuses" in db["requests"]:
            check("request statuses", got_req["statuses"] == db["requests"]["statuses"], f"got {got_req['statuses']}")
    cap = expected.get("max_llm_calls_per_turn")
    if cap is not None:
        worst = max(actual["llm_calls"], default=0)
        check("llm budget", worst <= cap, f"max {worst} > {cap}")
    return not failures, checks + failures


# ------------------------------------------------------------------ main


async def run_case(
    case: dict[str, Any], *, settings: Settings, pool: Any, live_llm: ChatLLM | None, delay: float
) -> dict[str, Any]:
    spec = case["input"]
    async with pool.connection() as conn:
        await reseed(conn, D0_DATE)
    llm = EvalLLM(live_llm)
    h = Harness(settings, llm, parse_when(spec.get("now", "D0T15:00")))
    await h.start()
    error: str | None = None
    try:
        for step in spec["steps"]:
            await run_step(h, step, spec["actor"], delay)
        db = await db_snapshot(h, case["expected"].get("db", {}))
    except Exception as exc:  # a crashing case is a failed case, not a crashed run
        error = f"{type(exc).__name__}: {exc}"
        db = {"roster": {}, "seats": {}, "requests": {"count": 0, "statuses": []}}
    finally:
        await h.stop()
    rec = h.record
    actual = {
        "mode": "live" if live_llm else "fake",
        "outcomes": rec["outcomes"], "tools_called": rec["tools_called"], "replies": rec["replies"],
        "final_status": rec["final_status"], "http_status": rec["http_status"], "db": db,
        "llm_calls": rec["llm_calls"], "latency_ms": rec["latency_ms"],
        "concurrent_outcomes": rec["concurrent_outcomes"],
    }  # fmt: skip
    ok, checks = judge(case["expected"], actual, live=live_llm is not None)
    if error:
        ok, checks = False, [f"FAILED runner error {error}", *checks]
    return {
        "id": case["id"], "input": spec, "expected": case["expected"], "actual": actual, "pass": ok,
        "notes": f"{case.get('notes', '')} | checks: {'; '.join(checks)}",
    }  # fmt: skip


def load_cases(path: Path, mode: str, category: str | None) -> list[dict[str, Any]]:
    cases = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [
        c for c in cases if mode in c.get("modes", ["fake", "live"]) and (not category or c.get("category") == category)
    ]


def print_summary(cases: list[dict[str, Any]], results: list[dict[str, Any]]) -> None:
    by_cat: dict[str, list[bool]] = defaultdict(list)
    cats = {c["id"]: c.get("category", "?") for c in cases}
    for r in results:
        by_cat[cats[r["id"]]].append(r["pass"])
    print(f"\n{'category':<24}{'pass':>6}{'total':>7}")
    for cat, flags in sorted(by_cat.items()):
        print(f"{cat:<24}{sum(flags):>6}{len(flags):>7}")
    total = [r["pass"] for r in results]
    worst = max((max(r["actual"]["llm_calls"], default=0) for r in results), default=0)
    print(f"{'TOTAL':<24}{sum(total):>6}{len(total):>7}")
    print(f"BUD-01  max LLM calls in any turn across all cases: {worst}  ->  {'PASS' if worst <= 4 else 'FAIL'}")
    for r in results:
        if not r["pass"]:
            print(f"  FAIL {r['id']}: {r['notes'].split('| checks:')[-1].strip()[:300]}")


async def main_async(args: argparse.Namespace) -> int:
    base = get_settings()
    db_url = os.environ.get("EVAL_DATABASE_URL") or base.database_url
    settings = Settings(
        _env_file=None, env="test", database_url=db_url, seed_on_start="never", jwt_secret=SECRET,
        llm_provider="fake", llm_fallback_provider="", log_level="ERROR", sweeper_interval_s=0,
        tool_timeout_ms=500 if args.mode == "fake" else base.tool_timeout_ms,
        groq_api_key=base.groq_api_key, groq_model=base.groq_model,
        gemini_api_key=base.gemini_api_key, gemini_model=base.gemini_model,
    )  # fmt: skip
    live_llm: ChatLLM | None = None
    provider_note = ""
    if args.mode == "live":
        live_settings = settings.model_copy(
            update={"llm_provider": base.llm_provider, "llm_fallback_provider": base.llm_fallback_provider}
        )
        provider = live_settings.effective_llm_provider()
        if provider in ("fake", "rule_based"):
            print("live mode needs GROQ_API_KEY + GROQ_MODEL (or the Gemini pair) in .env", file=sys.stderr)
            return 2
        live_llm = build_llm(live_settings, RuleBasedLLM())
        model = live_settings.groq_model if provider == "groq" else live_settings.gemini_model
        provider_note = f"provider={provider} model={model}"

    cases = load_cases(Path(args.cases), args.mode, args.filter)
    pool = await open_pool(db_url, min_size=2, max_size=20)
    await apply_schema(pool)
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    await AsyncPostgresSaver(pool).setup()
    results = []
    try:
        for case in cases:
            started = time.perf_counter()
            result = await run_case(case, settings=settings, pool=pool, live_llm=live_llm, delay=args.delay)
            if provider_note:
                result["notes"] += f" | {provider_note}"
            results.append(result)
            flag = "ok  " if result["pass"] else "FAIL"
            print(f"{flag} {case['id']:<8} {time.perf_counter() - started:5.2f}s")
    finally:
        await pool.close()

    out = Path(args.out or ROOT / f"results.{args.mode}.jsonl")
    lines = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in results)
    await asyncio.to_thread(out.write_text, lines)
    print_summary(cases, results)
    print(f"\nwrote {out}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=["fake", "live"], default="fake")
    parser.add_argument("--filter", help="only this category, e.g. isolation")
    parser.add_argument("--cases", default=str(DEFAULT_CASES))
    parser.add_argument("--out")
    parser.add_argument("--delay", type=float, default=2.5, help="seconds between live model calls (free-tier pacing)")
    raise SystemExit(asyncio.run(main_async(parser.parse_args())))


if __name__ == "__main__":
    main()
