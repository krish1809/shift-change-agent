"""Grounding guard for the only LLM-written text we ever show: small talk.

If the prose claims an outcome ("booked", "cancelled", "approved"...) and no tool in this turn
actually produced one, the prose is replaced by a safe template. A made-up confirmation is worse
than no answer.
"""

from __future__ import annotations

import re

OUTCOME_CLAIM = re.compile(
    r"\b(confirm\w*|book(ed|ing)?|chang(ed|ing)|mov(ed|ing)|approv\w*|cancel+(ed|ing)?|done|updated?|"
    r"scheduled|rescheduled|reserved|assigned|request(ed)? (has been|was) (sent|raised|created)|escalat\w*)\b",
    re.IGNORECASE,
)
MAX_PROSE_CHARS = 400


def check_prose(text: str, *, outcome_this_turn: bool) -> tuple[bool, str]:
    """Returns (allowed, reason)."""
    if not text or not text.strip():
        return False, "empty"
    if len(text) > MAX_PROSE_CHARS:
        return False, "too_long"
    if not outcome_this_turn and OUTCOME_CLAIM.search(text):
        return False, "ungrounded_outcome_claim"
    return True, "ok"
