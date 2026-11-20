"""The gate between model output and code. The model proposes; the gate decides what is acceptable.

Rules (SPEC §6.1): allowlist per mode, schema validation (identity fields are rejected because the
schemas forbid extras), at most one mutating call per turn, and span grounding: a time/option the
model "extracted" must literally appear in what the user just said, otherwise the model invented it
and it is demoted to a suggestion that we confirm with the user. An ungrounded DATE is flagged and
the handler only accepts it when it is the soonest upcoming trip (the "tonight" a model assumes).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from app.domain.time_resolver import resolve_time
from app.tools.registry import REGISTRY

GROUNDED_FIELDS = ("time_text", "date_text", "option_ref")
RECENT_USER_MESSAGES = 3


@dataclass(frozen=True)
class GateError:
    code: str  # INVALID_TOOL | INVALID_ARGS
    detail: str  # safe, short: used for the single repair call to the model, never shown to the user


@dataclass
class ValidCall:
    name: str
    args: BaseModel
    ungrounded: list[str] = field(default_factory=list)
    dropped_calls: int = 0


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def _compact(text: str) -> str:
    """Lower-case with spaces and dots removed, so '9pm', '9 pm' and '9 p.m.' compare equal."""
    return re.sub(r"[\s.]+", "", text.lower())


_TIME_TOKEN = re.compile(r"\d{1,2}(?:[:.]\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)?", re.IGNORECASE)


def _same_explicit_time(span: str, texts: list[str]) -> bool:
    """'9:00 pm' vs the user's '9 pm': same clock time, so not an invention.

    Only when the USER's own token is explicit (has am/pm or is 24h). A bare '8' stays ambiguous, so a model
    can't turn it into '20:00' behind the user's back.
    """
    wanted = resolve_time(span)
    if wanted.kind != "exact":
        return False
    for text in texts:
        for match in _TIME_TOKEN.finditer(text):
            theirs = resolve_time(match.group(0))
            if theirs.kind == "exact" and theirs.value == wanted.value:
                return True
    return False


def is_grounded(span: str, user_texts: list[str], *, is_time: bool = False) -> bool:
    recent = user_texts[-RECENT_USER_MESSAGES:]
    needle = _norm(span)
    if not needle:
        return False
    if any(needle in _norm(t) or _compact(span) in _compact(t) for t in recent):
        return True
    return is_time and _same_explicit_time(span, recent)


def select_call(tool_calls: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, int]:
    """One tool per turn: keep the first call and drop the rest (returns how many were dropped).

    This is stricter than "at most one mutating call": a turn produces exactly one typed result, so the
    reply can never mix the outcomes of two actions.
    """
    if not tool_calls:
        return None, 0
    return tool_calls[0], len(tool_calls) - 1


def validate_call(tool_call: dict[str, Any], *, mode: str, user_texts: list[str]) -> ValidCall | GateError:
    name = tool_call.get("name", "")
    spec = REGISTRY.get(name)
    if spec is None or mode not in spec.modes:
        return GateError("INVALID_TOOL", f"tool {name!r} is not available")
    raw = tool_call.get("args") or {}
    if not isinstance(raw, dict):
        return GateError("INVALID_ARGS", "arguments must be an object")
    try:
        args = spec.args_model.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['type']}" for e in exc.errors()[:4])
        return GateError("INVALID_ARGS", problems)
    return _ground(ValidCall(name, args), user_texts)


def _ground(call: ValidCall, user_texts: list[str]) -> ValidCall:
    updates: dict[str, Any] = {}
    for fname in GROUNDED_FIELDS:
        value = getattr(call.args, fname, None)
        if value and not is_grounded(value, user_texts, is_time=(fname == "time_text")):
            call.ungrounded.append(fname)
            if fname == "date_text":
                continue  # low risk: the handler only accepts it for the soonest trip, and replies name the day
            updates[fname] = None
            if fname == "time_text" and hasattr(call.args, "suggested_time") and not call.args.suggested_time:
                updates["suggested_time"] = value[:20]  # an invented time can only ever be a question
    if updates:
        call.args = call.args.model_copy(update=updates)
    return call
