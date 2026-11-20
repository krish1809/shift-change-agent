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
    """Wraps any ChatLLM for one turn.

    Every request that reaches a provider is counted, even if it fails (it still cost quota and time), and
    a fallback attempt counts as a separate call. So "at most 4 LLM calls" holds even under the strictest
    reading: at most 4 provider requests per employee message.
    """

    def __init__(self, inner: ChatLLM, budget: TurnBudget) -> None:
        self._inner = inner
        self.budget = budget

    async def invoke(self, messages: list[Message], tools: list[dict[str, Any]], *, timeout_s: float) -> LLMResponse:
        counted = getattr(self._inner, "invoke_counted", None)
        if counted is not None:  # a provider chain: let it charge each attempt
            return await counted(messages, tools, timeout_s=timeout_s, spend=self.budget.spend)
        self.budget.spend()
        return await self._inner.invoke(messages, tools, timeout_s=timeout_s)
