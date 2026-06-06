"""``/api/v1/schedules`` REST surface (PR24 reads + admin delete migration).

The orchestrator owns scheduled-task creation and *schedule* mutation:
cron / interval / once triggers fire from inside the agent process and
the helper functions in :mod:`rolemesh.db.task` are how it persists
state. Letting the SPA edit a trigger schedule independent of the
orchestrator's runtime view would create a stale-cache race the design
hasn't worked through yet, so trigger create/update stay off v1.

Reads (``get_current_user`` only — allowlisted in the default-deny
meta-test) answer "what tasks does this coworker have scheduled". The
``DELETE`` was migrated off the legacy ``/api/admin/tasks/{id}`` face:
task removal is a tenant-management operation gated by ``task.manage``
(admin+), with cross-tenant ids reading as 404.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncpg
from fastapi import APIRouter, Depends, Query, Response

from rolemesh.db import (
    count_tasks,
    delete_task,
    get_all_tasks,
    get_task_by_id,
    get_tasks_for_coworker,
)
from webui.dependencies import get_current_user, require_action
from webui.schemas_v1 import ScheduledTask, ScheduledTaskPage
from webui.v1._pagination import DEFAULT_PAGE_LIMIT, LimitParam, OffsetParam
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


@router.get("", response_model=ScheduledTaskPage)
async def list_schedules_endpoint(
    coworker_id: str | None = Query(default=None),
    limit: LimitParam = DEFAULT_PAGE_LIMIT,
    offset: OffsetParam = 0,
    user: AuthenticatedUser = Depends(get_current_user),
) -> ScheduledTaskPage:
    if coworker_id is not None:
        rows = await get_tasks_for_coworker(
            coworker_id, tenant_id=user.tenant_id, limit=limit, offset=offset,
        )
    else:
        rows = await get_all_tasks(
            tenant_id=user.tenant_id, limit=limit, offset=offset,
        )
    total = await count_tasks(user.tenant_id, coworker_id=coworker_id)
    return ScheduledTaskPage(
        items=[_to_response(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


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


@router.delete("/{task_id}", status_code=204)
async def delete_schedule_endpoint(
    task_id: str,
    user: AuthenticatedUser = Depends(require_action("task.manage")),
) -> Response:
    """Delete a scheduled task (cross-tenant / unknown id → 404).

    Migrated from ``DELETE /api/admin/tasks/{task_id}``. Gated by
    ``task.manage`` (admin+); a malformed UUID is treated as not-found
    rather than 500, matching the read endpoints.
    """
    try:
        row = await get_task_by_id(task_id, tenant_id=user.tenant_id)
    except asyncpg.DataError:
        row = None
    if row is None:
        raise_error_response(
            "NOT_FOUND",
            "Scheduled task not found.",
            status_code=404,
            details={"task_id": task_id},
        )
    await delete_task(task_id, tenant_id=user.tenant_id)
    return Response(status_code=204)
