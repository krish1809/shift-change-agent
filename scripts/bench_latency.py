"""Latency benchmark against a LIVE model: p50/p95/max reply time and LLM calls per message.

    python -m scripts.bench_latency            # needs GROQ_API_KEY + GROQ_MODEL (or Gemini) in .env
Writes eval/latency.json. Requests are paced to stay inside free-tier rate limits.
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import sys
from pathlib import Path

from app.db.pool import apply_schema, open_pool
from app.db.seed import reseed
from app.llm.openai_compat import build_llm
from app.llm.rule_based import RuleBasedLLM
from app.settings import get_settings
from eval.run_eval import D0_DATE, SECRET, EvalLLM, Harness, parse_when, send_chat

ACME = {"company_id": "acme", "employee_id": "A001"}
GLOBEX = {"company_id": "globex", "employee_id": "G001"}
# (company actor, [messages of one conversation]) - clear, ambiguous, follow-ups, questions, status
CONVERSATIONS = [
    (ACME, ["Move my drop today to 9 pm"]),
    (ACME, ["can I go a bit late today?", "make it 9 instead"]),
    (ACME, ["8 pm drop please", "the second one"]),
    (ACME, ["What's the cut-off for changing my drop?"]),
    (ACME, ["What time is my pickup tomorrow?"]),
    (ACME, ["What drops are there after 8 tonight?"]),
    (ACME, ["change my cab tomorrow", "the drop", "8 pm"]),
    (ACME, ["cancel my pickup tomorrow"]),
    (ACME, ["thanks!"]),
    (ACME, ["30 mins later"]),
    (GLOBEX, ["Can you push my drop tonight to 9 pm?"]),
    (GLOBEX, ["What's my schedule?"]),
    (GLOBEX, ["move it to 9"]),
    (GLOBEX, ["What's the cut-off?"]),
    (GLOBEX, ["cancel tomorrow's pickup"]),
]


async def main() -> int:
    base = get_settings()
    live = base.model_copy(
        update={"jwt_secret": SECRET, "env": "test", "seed_on_start": "never", "sweeper_interval_s": 0}
    )
    if live.effective_llm_provider() in ("fake", "rule_based"):
        print("Set GROQ_API_KEY + GROQ_MODEL (or the Gemini pair) in .env first.", file=sys.stderr)
        return 2
    db_url = os.environ.get("EVAL_DATABASE_URL") or base.database_url
    live = live.model_copy(update={"database_url": db_url})
    pool = await open_pool(db_url, min_size=2, max_size=10)
    await apply_schema(pool)
    latencies: list[int] = []
    calls: list[int] = []
    try:
        for _ in range(4):  # 4 rounds x ~15 conversations ~ 60+ messages
            for actor, messages in CONVERSATIONS:
                async with pool.connection() as conn:
                    await reseed(conn, D0_DATE)
                h = Harness(live, EvalLLM(build_llm(live, RuleBasedLLM())), parse_when("D0T14:00"))
                await h.start()
                try:
                    for text in messages:
                        result = await send_chat(h, actor, {"user": text})
                        meta = result["data"].get("meta", {})
                        latencies.append(int(meta.get("latency_ms", 0)))
                        calls.append(int(meta.get("llm_calls", 0)))
                        await asyncio.sleep(2.5)  # free-tier pacing
                finally:
                    await h.stop()
    finally:
        await pool.close()
    latencies.sort()
    pct = lambda q: latencies[min(len(latencies) - 1, int(q * len(latencies)))]  # noqa: E731
    report = {
        "messages": len(latencies),
        "provider": live.effective_llm_provider(),
        "p50_ms": pct(0.50),
        "p95_ms": pct(0.95),
        "max_ms": latencies[-1],
        "mean_llm_calls": round(statistics.mean(calls), 2),
        "max_llm_calls": max(calls),
        "p95_under_5s": pct(0.95) < 5000,
        "max_calls_ok": max(calls) <= 4,
    }
    await asyncio.to_thread(Path("eval/latency.json").write_text, json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
