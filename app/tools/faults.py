"""Fault injection for tests, eval and the demo (FAULTS env or POST /dev/faults).

Modes per tool (or 'roster_repo' for the roster read at the start of a turn):
  timeout      never answers (the executor's timeout fires)
  error        raises
  fail_once    raises once, then works
  slow:<ms>    does the work, THEN waits <ms> (a write that commits after the client gave up)
"""

from __future__ import annotations

import asyncio
import re

VALID = re.compile(r"^(timeout|error|fail_once|slow:\d{1,6})$")


class InjectedFault(Exception):
    pass


class FaultInjector:
    def __init__(self, spec: str = "") -> None:
        self._modes: dict[str, str] = {}
        for item in filter(None, (s.strip() for s in spec.split(","))):
            tool, _, mode = item.partition("=")
            self.arm(tool.strip(), mode.strip())

    def arm(self, tool: str, mode: str) -> None:
        if not VALID.match(mode):
            raise ValueError(f"unknown fault mode {mode!r}")
        self._modes[tool] = mode

    def clear(self, tool: str | None = None) -> None:
        if tool is None:
            self._modes.clear()
        else:
            self._modes.pop(tool, None)

    def active(self) -> dict[str, str]:
        return dict(self._modes)

    async def before(self, tool: str) -> None:
        mode = self._modes.get(tool)
        if mode == "timeout":
            await asyncio.sleep(3600)
        elif mode == "error":
            raise InjectedFault(tool)
        elif mode == "fail_once":
            self._modes.pop(tool, None)
            raise InjectedFault(tool)

    async def after(self, tool: str) -> None:
        mode = self._modes.get(tool, "")
        if mode.startswith("slow:"):
            await asyncio.sleep(int(mode.split(":", 1)[1]) / 1000)
