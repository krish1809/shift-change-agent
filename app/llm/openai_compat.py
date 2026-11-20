"""One OpenAI-compatible client for Groq and Gemini (both expose /chat/completions)."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

import openai
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from app.llm.base import (
    BudgetExceeded,
    ChatLLM,
    LLMBadOutput,
    LLMError,
    LLMRateLimited,
    LLMResponse,
    LLMTimeout,
    LLMUnavailable,
    Message,
)
from app.settings import Settings

log = logging.getLogger(__name__)


def map_provider_error(exc: BaseException) -> LLMError:
    """Provider/SDK exceptions -> our typed errors (the only ones the graph knows about)."""
    if isinstance(exc, LLMError):
        return exc
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError, openai.APITimeoutError)):
        return LLMTimeout(type(exc).__name__)
    if isinstance(exc, openai.RateLimitError):
        return LLMRateLimited("rate limited")
    if isinstance(exc, openai.APIConnectionError):
        return LLMUnavailable("connection error")
    if isinstance(exc, openai.APIStatusError):
        if exc.status_code == 429:
            return LLMRateLimited("rate limited")
        if exc.status_code >= 500 or exc.status_code in (401, 403):
            return LLMUnavailable(f"http {exc.status_code}")
        return LLMBadOutput(f"http {exc.status_code}")  # 400: e.g. the model produced a malformed tool call
    return LLMUnavailable(type(exc).__name__)


def _to_langchain(messages: list[Message]) -> list[BaseMessage]:
    kinds = {"system": SystemMessage, "user": HumanMessage, "assistant": AIMessage}
    return [kinds[m["role"]](content=m["content"]) for m in messages]


class OpenAICompatLLM:
    def __init__(self, *, base_url: str, api_key: str, model: str, name: str, timeout_s: float) -> None:
        self.name = name
        self.model = model
        # max_retries=0: our own latency budget decides about retries/fallback, not the SDK.
        self._client = ChatOpenAI(
            base_url=base_url, api_key=api_key, model=model, temperature=0, timeout=timeout_s, max_retries=0
        )

    async def invoke(self, messages: list[Message], tools: list[dict[str, Any]], *, timeout_s: float) -> LLMResponse:
        started = time.perf_counter()
        try:
            bound = self._client.bind_tools(tools, tool_choice="auto")
            ai = await asyncio.wait_for(bound.ainvoke(_to_langchain(messages)), timeout_s)
        except Exception as exc:
            error = map_provider_error(exc)
            log.warning("llm call failed", extra={"fields": {"provider": self.name, "error": type(error).__name__}})
            raise error from exc
        calls = [{"name": c["name"], "args": c.get("args") or {}} for c in (ai.tool_calls or [])]
        if not calls and getattr(ai, "invalid_tool_calls", None):
            raise LLMBadOutput("malformed tool call")
        text = ai.content if isinstance(ai.content, str) else None
        log.info(
            "llm call ok",
            extra={
                "fields": {
                    "provider": self.name,
                    "ms": int((time.perf_counter() - started) * 1000),
                    "tools": len(calls),
                }
            },
        )
        return LLMResponse(text=(text or None), tool_calls=calls)


class FallbackLLM:
    """Primary provider, then the fallback if configured and enough of the turn's time is left.

    A provider that answered 429 (rate limited) is skipped for `cooldown_s`: while Groq's free quota is
    exhausted, every message goes straight to Gemini instead of first wasting a call and ~0.3 s on a
    guaranteed 429. After the cooldown the primary is tried again. If every provider is cooling down, they
    are all tried anyway (better a likely 429 than no attempt at all).
    """

    MIN_TIME_FOR_FALLBACK_S = 1.0

    def __init__(self, providers: list[ChatLLM], *, cooldown_s: float = 60.0) -> None:
        self._providers = providers
        self._cooldown_s = cooldown_s
        self._cooling_until: dict[int, float] = {}  # provider index -> loop time when it may be tried again

    async def invoke(self, messages: list[Message], tools: list[dict[str, Any]], *, timeout_s: float) -> LLMResponse:
        return await self.invoke_counted(messages, tools, timeout_s=timeout_s, spend=None)

    def _order(self, now: float) -> list[int]:
        ready = [i for i in range(len(self._providers)) if self._cooling_until.get(i, 0.0) <= now]
        return ready or list(range(len(self._providers)))

    async def invoke_counted(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]],
        *,
        timeout_s: float,
        spend: Callable[[], None] | None,
    ) -> LLMResponse:
        """`spend` is charged once per provider attempt (raises BudgetExceeded when the turn's cap is hit)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        last: LLMError | None = None
        for index in self._order(loop.time()):
            provider = self._providers[index]
            remaining = deadline - loop.time()
            if last is not None and remaining < self.MIN_TIME_FOR_FALLBACK_S:
                break
            if spend is not None:
                try:
                    spend()
                except BudgetExceeded:
                    if last is not None:
                        raise last from None  # report the provider failure, not the cap
                    raise
            try:
                response = await provider.invoke(messages, tools, timeout_s=max(remaining, 0.1))
            except LLMRateLimited as exc:
                self._cooling_until[index] = loop.time() + self._cooldown_s
                last = exc
            except LLMError as exc:
                last = exc
            else:
                self._cooling_until.pop(index, None)
                return response
        raise last or LLMUnavailable("no provider configured")


def build_llm(settings: Settings, rule_based: ChatLLM) -> ChatLLM:
    """Provider chain from settings. Without a usable key the deterministic interpreter takes over."""
    primary = settings.effective_llm_provider()
    if primary in ("fake", "rule_based"):
        return rule_based
    chain: list[ChatLLM] = [_make(settings, primary)]
    fallback = settings.llm_fallback_provider
    if fallback and fallback != primary and settings.provider_configured(fallback):
        chain.append(_make(settings, fallback))
    return FallbackLLM(chain, cooldown_s=settings.llm_rate_limit_cooldown_s)


def _make(settings: Settings, provider: str) -> OpenAICompatLLM:
    timeout_s = settings.llm_timeout_ms / 1000
    if provider == "groq":
        return OpenAICompatLLM(
            base_url=settings.groq_base_url,
            api_key=settings.groq_api_key,
            model=settings.groq_model,
            name="groq",
            timeout_s=timeout_s,
        )
    return OpenAICompatLLM(
        base_url=settings.gemini_base_url,
        api_key=settings.gemini_api_key,
        model=settings.gemini_model,
        name="gemini",
        timeout_s=timeout_s,
    )
