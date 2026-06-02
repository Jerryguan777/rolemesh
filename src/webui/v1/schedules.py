"""``/api/v1/schedules`` REST surface (PR24 — read-only).

The orchestrator owns scheduled-task creation and mutation: cron /
interval / once triggers fire from inside the agent process and the
helper functions in :mod:`rolemesh.db.task` are how it persists state.

This module exposes the table read-side for the UI so a user can
answer "what tasks does this coworker have scheduled" without going
through the orchestrator IPC. Writes are intentionally not on the
v1 surface — letting the SPA mutate the trigger schedule independent
of the orchestrator's runtime view would create a stale-cache race
the design hasn't worked through yet (orchestrator caches its task
list and only refreshes on schedule fire).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncpg
from fastapi import APIRouter, Depends, Query

from rolemesh.db import (
    get_all_tasks,
    get_task_by_id,
    get_tasks_for_coworker,
)
from webui.dependencies import get_current_user
from webui.schemas_v1 import ScheduledTask
from webui.v1.errors import raise_error_response

if TYPE_CHECKING:
    from rolemesh.auth.provider import AuthenticatedUser
    from rolemesh.core.types import ScheduledTask as ScheduledTaskDataclass

router = APIRouter(prefix="/schedules", tags=["Schedules"])


def _to_response(t: ScheduledTaskDataclass) -> ScheduledTask:
    return ScheduledTask(
        id=t.id,
        tenant_id=t.tenant_id,
        coworker_id=t.coworker_id,
        conversation_id=t.conversation_id,
        prompt=t.prompt,
        schedule_type=t.schedule_type,
        schedule_value=t.schedule_value,
        context_mode=t.context_mode,
        next_run=t.next_run,
        last_run=t.last_run,
        last_result=t.last_result,
        status=t.status,
        created_at=t.created_at,
    )


@router.get("", response_model=list[ScheduledTask])
async def list_schedules_endpoint(
    coworker_id: str | None = Query(default=None),
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[ScheduledTask]:
    if coworker_id is not None:
        rows = await get_tasks_for_coworker(coworker_id, tenant_id=user.tenant_id)
    else:
        rows = await get_all_tasks(tenant_id=user.tenant_id)
    return [_to_response(r) for r in rows]


@router.get("/{task_id}", response_model=ScheduledTask)
async def get_schedule_endpoint(
    task_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> ScheduledTask:
    try:
        row = await get_task_by_id(task_id, tenant_id=user.tenant_id)
    except asyncpg.DataError:
        # Malformed UUID — same handling pattern as skills/coworkers
        # (treat as not-found rather than 500).
        row = None
    if row is None:
        raise_error_response(
            "NOT_FOUND",
            "Scheduled task not found.",
            status_code=404,
            details={"task_id": task_id},
        )
    return _to_response(row)
