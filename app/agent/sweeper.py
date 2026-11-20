"""Background safety net. Makes the approval path robust to crashes and silent managers.

Each pass: (1) resume decisions that were recorded but never delivered, (2) expire requests whose
approval window has closed, (3) re-drive pending requests whose conversation isn't paused.
Every action is idempotent, so running it often (or from two workers) is safe.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import FastAPI

from app.agent.runner import redrive_unpaused, resume_request
from app.db.repositories import audit
from app.db.repositories import requests as requests_repo
from app.domain import approvals

log = logging.getLogger(__name__)


async def run_once(app: FastAPI) -> dict[str, int]:
    pool, clock = app.state.pool, app.state.clock
    done = {"resumed": 0, "expired": 0, "redriven": 0}

    async with pool.connection() as conn:
        undelivered = await approvals.decided_not_resumed(conn)
    for request in undelivered:
        done["resumed"] += int(await _safe(resume_request, app, request))

    async with pool.connection() as conn:
        overdue = await approvals.overdue_pending(conn, clock.now())
    for request in overdue:
        async with pool.connection() as conn:
            expired = await requests_repo.record_decision(
                conn, request.company_id, request.id, decision="expired", decided_by="system", note=None,
                at=clock.now(),
            )  # fmt: skip
            if expired is None:  # a manager decided just now
                continue
            await audit.add_audit(
                conn, request.company_id, at=clock.now(), actor_type="system", actor_id="sweeper", action="EXPIRED",
                request_id=request.id, thread_id=request.thread_id,
            )  # fmt: skip
        done["expired"] += int(await _safe(resume_request, app, expired))

    async with pool.connection() as conn:
        open_requests = await approvals.open_pending(conn)
    for request in open_requests:
        done["redriven"] += int(await _safe(redrive_unpaused, app, request))
    return done


async def _safe(fn, app: FastAPI, request) -> bool:  # type: ignore[no-untyped-def]
    try:
        return await fn(app, request)
    except Exception:
        log.exception("sweeper step failed", extra={"fields": {"request_id": request.id}})
        return False


async def sweeper_loop(app: FastAPI) -> None:
    interval = app.state.settings.sweeper_interval_s
    while True:
        try:
            result = await run_once(app)
            if any(result.values()):
                log.info("sweeper pass", extra={"fields": result})
        except Exception:
            log.exception("sweeper pass failed")
        await asyncio.sleep(interval)


def start_sweeper(app: FastAPI) -> asyncio.Task[None]:
    return asyncio.create_task(sweeper_loop(app), name="sweeper")


async def stop_sweeper(task: asyncio.Task[None]) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
