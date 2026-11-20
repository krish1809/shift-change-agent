"""What the LLM may send. Note what is NOT here: company_id, employee_id, role, trip ids.

`extra="forbid"` turns any identity field the model invents into a validation error. Times and dates
are verbatim user spans; code resolves them.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GetMyRosterArgs(ToolArgs):
    date_text: str | None = Field(default=None, max_length=80, description="Date words exactly as the user wrote them.")


class CheckCabOptionsArgs(ToolArgs):
    trip_type: Literal["pickup", "drop"] | None = None
    date_text: str | None = Field(default=None, max_length=80, description="Date words exactly as the user wrote them.")
    time_text: str | None = Field(default=None, max_length=80, description="Time words exactly as the user wrote them.")


class RequestShiftChangeArgs(ToolArgs):
    change_type: Literal["reschedule", "cancel"] = "reschedule"
    trip_type: Literal["pickup", "drop"] | None = None
    date_text: str | None = Field(
        default=None, max_length=80, description="Date words copied verbatim, e.g. 'tomorrow', 'tonight'."
    )
    time_text: str | None = Field(
        default=None, max_length=80, description="Time words copied verbatim, e.g. '8', 'half past 8', '30 min later'."
    )
    option_ref: str | None = Field(
        default=None, max_length=80, description="The user's words pointing at an offered option, e.g. 'the 8:30 one'."
    )
    suggested_time: str | None = Field(
        default=None, max_length=20, description="Only if the user gave no usable time: your best guess, e.g. '20:30'."
    )


class RespondToOfferArgs(ToolArgs):
    accept: bool


class GetRequestStatusArgs(ToolArgs):
    request_id: str | None = Field(default=None, max_length=20)


class WithdrawRequestArgs(ToolArgs):
    request_id: str | None = Field(default=None, max_length=20)


class GetMyPolicyArgs(ToolArgs):
    pass


DESCRIPTIONS = {
    "get_my_roster": "Show the user's own pickups/drops for a date other than today/tomorrow.",
    "check_cab_options": "List cabs with free seats the user could switch to (does not change anything).",
    "request_shift_change": "Change (reschedule) or cancel one of the user's pickups/drops. Rules are checked by the system.",
    "respond_to_offer": "The user answered yes/no to the question in the last reply (send to manager? / did you mean?).",
    "get_request_status": "Status of the user's shift-change request (default: latest open one).",
    "withdraw_request": "Withdraw the user's request that is waiting for manager approval.",
    "get_my_policy": "The user's company rules: cut-offs, how far a trip can move, daily limits.",
}


def openai_tool_schema(name: str, model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema()
    schema.pop("title", None)
    for prop in schema.get("properties", {}).values():
        prop.pop("title", None)
    schema["additionalProperties"] = False
    return {"type": "function", "function": {"name": name, "description": DESCRIPTIONS[name], "parameters": schema}}
