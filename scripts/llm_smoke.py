"""One real model call: prints which tool it chose, with which arguments, and how long it took.

    python -m scripts.llm_smoke "can I go a bit late today?"

Needs GROQ_API_KEY + GROQ_MODEL (or the Gemini pair) in .env. Never prints the key.
"""

from __future__ import annotations

import asyncio
import sys
import time

from app.agent.prompts import build_context, build_messages, estimate_tokens
from app.llm.openai_compat import build_llm
from app.llm.rule_based import RuleBasedLLM
from app.settings import get_settings
from app.tools.registry import tool_schemas


async def main(text: str) -> None:
    settings = get_settings()
    provider = settings.effective_llm_provider()
    if provider in ("fake", "rule_based"):
        raise SystemExit("No LLM key/model configured: set GROQ_API_KEY and GROQ_MODEL in .env first.")
    llm = build_llm(settings, RuleBasedLLM())
    context = build_context(
        now_label="Mon 2 Mar, 2:00 PM",
        first_name="Priya",
        roster_lines=["drop today 7:00 PM", "pickup tomorrow 9:00 AM"],
        draft=None,
        offered_options=[],
        pending_offer=None,
    )
    messages = build_messages(context=context, history=[], user_text=text)
    started = time.perf_counter()
    response = await llm.invoke(messages, tool_schemas("normal"), timeout_s=settings.llm_timeout_ms / 1000)
    elapsed = int((time.perf_counter() - started) * 1000)
    print(f"provider={provider} prompt_tokens~{estimate_tokens(messages)} latency={elapsed}ms")
    print("tool_calls:", response.tool_calls)
    print("text:", response.text)


if __name__ == "__main__":
    asyncio.run(main(" ".join(sys.argv[1:]) or "can I go a bit late today?"))
