"""Fake models for tests and offline eval. Tests never call a real LLM."""

from __future__ import annotations

from typing import Any

from app.llm.base import LLMBadOutput, LLMRateLimited, LLMResponse, LLMTimeout, LLMUnavailable, Message


def response_from_spec(spec: Any) -> LLMResponse | Exception:
    """Turn the eval-file shapes into a response: {tool,args} | {text} | {garbage} | {raise}."""
    if isinstance(spec, (LLMResponse, Exception)):
        return spec
    if "tool" in spec:
        return LLMResponse(tool_calls=[{"name": spec["tool"], "args": dict(spec.get("args", {}))}])
    if "tools" in spec:
        return LLMResponse(tool_calls=[{"name": t["tool"], "args": dict(t.get("args", {}))} for t in spec["tools"]])
    if "text" in spec:
        return LLMResponse(text=spec["text"])
    if "garbage" in spec:
        return LLMResponse(tool_calls=[{"name": "request_shift_change", "args": {"change_type": ["not", "valid"]}}])
    if "raise" in spec:
        return {
            "rate_limit": LLMRateLimited("429"),
            "timeout": LLMTimeout("timeout"),
            "unavailable": LLMUnavailable("down"),
            "bad_output": LLMBadOutput("bad"),
        }[spec["raise"]]
    raise ValueError(f"unknown fake LLM spec: {spec!r}")


class ScriptedLLM:
    """Replays a fixed list of responses/exceptions, one per call. Records what it was shown."""

    def __init__(self, script: list[Any]) -> None:
        self._script = [response_from_spec(s) for s in script]
        self.calls: list[list[Message]] = []

    async def invoke(self, messages: list[Message], tools: list[dict[str, Any]], *, timeout_s: float) -> LLMResponse:
        self.calls.append(messages)
        if not self._script:
            raise LLMUnavailable("script exhausted")
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class LoopingLLM:
    """Always asks for another read tool: proves the code-enforced budget ends the turn."""

    def __init__(self) -> None:
        self.calls: list[list[Message]] = []

    async def invoke(self, messages: list[Message], tools: list[dict[str, Any]], *, timeout_s: float) -> LLMResponse:
        self.calls.append(messages)
        return LLMResponse(tool_calls=[{"name": "get_my_policy", "args": {}}])


class GarbageLLM:
    """Always returns tool calls that fail validation."""

    def __init__(self) -> None:
        self.calls: list[list[Message]] = []

    async def invoke(self, messages: list[Message], tools: list[dict[str, Any]], *, timeout_s: float) -> LLMResponse:
        self.calls.append(messages)
        return LLMResponse(tool_calls=[{"name": "request_shift_change", "args": {"change_type": ["x"]}}])


class RaisingLLM:
    def __init__(self, error: Exception) -> None:
        self._error = error
        self.calls: list[list[Message]] = []

    async def invoke(self, messages: list[Message], tools: list[dict[str, Any]], *, timeout_s: float) -> LLMResponse:
        self.calls.append(messages)
        raise self._error
