"""Tool registry: name -> (args model, handler, kind, modes). Also generates the LLM tool schemas."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from app.tools import schemas


@dataclass(frozen=True)
class ToolSpec:
    name: str
    args_model: type[BaseModel]
    mutating: bool
    modes: frozenset[str]


NORMAL = frozenset({"normal"})
BOTH = frozenset({"normal", "pending"})

REGISTRY: dict[str, ToolSpec] = {
    s.name: s
    for s in (
        ToolSpec("get_my_roster", schemas.GetMyRosterArgs, False, NORMAL),
        ToolSpec("check_cab_options", schemas.CheckCabOptionsArgs, False, NORMAL),
        ToolSpec("request_shift_change", schemas.RequestShiftChangeArgs, True, NORMAL),
        ToolSpec("respond_to_offer", schemas.RespondToOfferArgs, True, NORMAL),
        ToolSpec("get_request_status", schemas.GetRequestStatusArgs, False, BOTH),
        ToolSpec("withdraw_request", schemas.WithdrawRequestArgs, True, BOTH),
        ToolSpec("get_my_policy", schemas.GetMyPolicyArgs, False, BOTH),
    )
}


def tool_schemas(mode: str) -> list[dict[str, Any]]:
    return [schemas.openai_tool_schema(s.name, s.args_model) for s in REGISTRY.values() if mode in s.modes]
