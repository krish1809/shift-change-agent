"""Plain-language words for request statuses, shared by status replies and duplicate replies."""

from __future__ import annotations

STATUS_WORDS = {
    "APPLIED": "already done",
    "APPROVED_APPLIED": "approved and applied",
    "APPROVAL_REJECTED": "declined by your manager",
    "APPROVED_BUT_UNAVAILABLE": "approved, but the seat was no longer available",
    "EXPIRED": "expired without a decision",
    "WITHDRAWN": "withdrawn",
    "REJECTED": "not allowed",
    "PENDING_APPROVAL": "waiting for your transport manager",
}
