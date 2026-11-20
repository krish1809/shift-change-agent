"""LLM contract, typed errors and the per-turn call budget.

We use our own tiny message/response types so tests, the rule-based interpreter and the real
provider all look identical to the graph.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

Message = dict[str, str]  # {"role": "system" | "user" | "assistant", "content": "..."}


@dataclass
class LLMResponse:
    text: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)  # [{"name": ..., "args": {...}}]


class LLMError(Exception):
    """Base for provider failures. Messages are for logs only, never shown to the employee."""


class LLMRateLimited(LLMError):
    pass


class LLMTimeout(LLMError):
    pass


class LLMUnavailable(LLMError):
    pass


class LLMBadOutput(LLMError):
    pass


class BudgetExceeded(LLMError):
    pass


class ChatLLM(Protocol):
    async def invoke(
        self, messages: list[Message], tools: list[dict[str, Any]], *, timeout_s: float
    ) -> LLMResponse: ...


class TurnBudget:
    """Counts LLM calls for ONE employee message. The cap is enforced here, in code, not in a prompt."""

    def __init__(self, max_calls: int) -> None:
        self.max_calls = max_calls
        self.calls = 0

    @property
    def remaining(self) -> int:
        return self.max_calls - self.calls

    def spend(self) -> None:
        if self.calls >= self.max_calls:
            raise BudgetExceeded(f"more than {self.max_calls} LLM calls in one turn")
        self.calls += 1


class BudgetedLLM:
    """Wraps any ChatLLM for one turn. A call is counted even if it fails: it still cost quota/time."""

    def __init__(self, inner: ChatLLM, budget: TurnBudget) -> None:
        self._inner = inner
        self.budget = budget

    async def invoke(self, messages: list[Message], tools: list[dict[str, Any]], *, timeout_s: float) -> LLMResponse:
        self.budget.spend()
        return await self._inner.invoke(messages, tools, timeout_s=timeout_s)
