"""Scheduled tasks + task run logs."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from rolemesh.core.types import ScheduledTask, TaskRunLog
from rolemesh.db._pool import _to_dt, admin_conn, tenant_conn

if TYPE_CHECKING:
    import asyncpg

__all__ = [
    "create_task",
    "delete_task",
    "get_all_tasks",
    "get_due_tasks",
    "get_task_by_id",
    "get_tasks_for_coworker",
    "log_task_run",
    "update_task",
    "update_task_after_run",
]


# ---------------------------------------------------------------------------
# Scheduled tasks (new: per-coworker with UUID/TIMESTAMPTZ)
# ---------------------------------------------------------------------------


async def create_task(task: ScheduledTask) -> None:
    """Create a new scheduled task."""
    async with tenant_conn(task.tenant_id) as conn:
        await conn.execute(
            """
            INSERT INTO scheduled_tasks (id, tenant_id, coworker_id, conversation_id, prompt, schedule_type, schedule_value, context_mode, next_run, status, created_at)
            VALUES ($1::uuid, $2::uuid, $3::uuid, $4::uuid, $5, $6, $7, $8, $9, $10, now())
            """,
            task.id,
            task.tenant_id,
            task.coworker_id,
            task.conversation_id,
            task.prompt,
            task.schedule_type,
            task.schedule_value,
            task.context_mode or "isolated",
            _to_dt(task.next_run),
            task.status,
        )


def _record_to_scheduled_task(row: asyncpg.Record) -> ScheduledTask:
    """Convert an asyncpg.Record to a ScheduledTask dataclass."""
    nr = row["next_run"]
    lr = row["last_run"]
    ca = row["created_at"]
    conv_id = row.get("conversation_id")
    return ScheduledTask(
        id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        coworker_id=str(row["coworker_id"]),
        conversation_id=str(conv_id) if conv_id else None,
        prompt=row["prompt"],
        schedule_type=row["schedule_type"],
        schedule_value=row["schedule_value"],
        context_mode=row["context_mode"] or "isolated",
        next_run=nr.isoformat() if nr else None,
        last_run=lr.isoformat() if lr else None,
        last_result=row["last_result"],
        status=row["status"],
        created_at=ca.isoformat() if ca else "",
    )


async def get_task_by_id(task_id: str, *, tenant_id: str) -> ScheduledTask | None:
    """Fetch a task by id, scoped to ``tenant_id``.

    See ``get_user`` for the tenant-filter rationale.
    """
    async with tenant_conn(tenant_id) as conn:
        row = await conn.fetchrow(
            "SELECT * FROM scheduled_tasks WHERE id = $1::uuid AND tenant_id = $2::uuid",
            task_id,
            tenant_id,
        )
    if row is None:
        return None
    return _record_to_scheduled_task(row)


async def get_tasks_for_coworker(
    coworker_id: str, *, tenant_id: str
) -> list[ScheduledTask]:
    """Get all tasks for a specific coworker."""
    async with tenant_conn(tenant_id) as conn:
        rows = await conn.fetch(
            "SELECT * FROM scheduled_tasks "
            "WHERE coworker_id = $1::uuid AND tenant_id = $2::uuid "
            "ORDER BY created_at DESC",
            coworker_id,
            tenant_id,
        )
    return [_record_to_scheduled_task(row) for row in rows]


async def get_all_tasks(tenant_id: str | None = None) -> list[ScheduledTask]:
    """Get all scheduled tasks, optionally filtered by tenant.

    Treats both ``None`` and ``""`` as "no tenant scope" so callers
    that build the parameter from an admin REST query string don't
    accidentally pass an empty value into ``tenant_conn`` and trigger
    fail-closed (current_tenant_id = NULL → RLS drops every row).
    """
    if tenant_id:
        async with tenant_conn(tenant_id) as conn:
            rows = await conn.fetch(
                "SELECT * FROM scheduled_tasks "
                "WHERE tenant_id = $1::uuid ORDER BY created_at DESC",
                tenant_id,
            )
    else:
        async with admin_conn() as conn:
            rows = await conn.fetch(
                "SELECT * FROM scheduled_tasks ORDER BY created_at DESC"
            )
    return [_record_to_scheduled_task(row) for row in rows]


async def update_task(
    task_id: str,
    *,
    tenant_id: str,
    prompt: str | None = None,
    schedule_type: str | None = None,
    schedule_value: str | None = None,
    next_run: str | None = None,
    status: str | None = None,
) -> ScheduledTask | None:
    """Update selected fields on a scheduled task, scoped to ``tenant_id``.

    Mirrors ``update_user`` / ``update_coworker``: when no fields are
    supplied, returns the current row instead of silently doing
    nothing — keeps PATCH-style callers consistent across resources.
    Returns ``None`` if the row doesn't exist or belongs to another
    tenant.
    """
    fields: list[str] = []
    values: list[Any] = []
    param_idx = 1

    if prompt is not None:
        fields.append(f"prompt = ${param_idx}")
        values.append(prompt)
        param_idx += 1
    if schedule_type is not None:
        fields.append(f"schedule_type = ${param_idx}")
        values.append(schedule_type)
        param_idx += 1
    if schedule_value is not None:
        fields.append(f"schedule_value = ${param_idx}")
        values.append(schedule_value)
        param_idx += 1
    if next_run is not None:
        fields.append(f"next_run = ${param_idx}")
        values.append(_to_dt(next_run))
        param_idx += 1
    if status is not None:
        fields.append(f"status = ${param_idx}")
        values.append(status)
        param_idx += 1

    if not fields:
        return await get_task_by_id(task_id, tenant_id=tenant_id)

    values.append(task_id)
    values.append(tenant_id)
    async with tenant_conn(tenant_id) as conn:
        row = await conn.fetchrow(
            f"UPDATE scheduled_tasks SET {', '.join(fields)} "
            f"WHERE id = ${param_idx}::uuid AND tenant_id = ${param_idx + 1}::uuid "
            f"RETURNING *",
            *values,
        )
    if row is None:
        return None
    return _record_to_scheduled_task(row)


async def delete_task(task_id: str, *, tenant_id: str) -> None:
    """Delete a task and its run logs (CASCADE handles task_run_logs)."""
    async with tenant_conn(tenant_id) as conn:
        await conn.execute(
            "DELETE FROM scheduled_tasks WHERE id = $1::uuid AND tenant_id = $2::uuid",
            task_id,
            tenant_id,
        )


async def get_due_tasks(tenant_id: str | None = None) -> list[ScheduledTask]:
    """Get all active tasks whose next_run is in the past.

    Treats both ``None`` and ``""`` as "global scheduler scan" so an
    empty string from a misconfigured caller doesn't enter
    ``tenant_conn`` and silently filter every row to zero.
    """
    now = datetime.now(UTC)
    if tenant_id:
        async with tenant_conn(tenant_id) as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM scheduled_tasks
                WHERE tenant_id = $1::uuid AND status = 'active'
                  AND next_run IS NOT NULL AND next_run <= $2
                ORDER BY next_run
                """,
                tenant_id,
                now,
            )
    else:
        async with admin_conn() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM scheduled_tasks
                WHERE status = 'active' AND next_run IS NOT NULL
                  AND next_run <= $1
                ORDER BY next_run
                """,
                now,
            )
    return [_record_to_scheduled_task(row) for row in rows]


async def update_task_after_run(
    task_id: str,
    next_run: str | None,
    last_result: str,
    *,
    tenant_id: str,
) -> None:
    """Update task state after execution."""
    now = datetime.now(UTC)
    new_status = "completed" if next_run is None else None
    async with tenant_conn(tenant_id) as conn:
        await conn.execute(
            """
            UPDATE scheduled_tasks
            SET next_run = $1, last_run = $2, last_result = $3,
                status = COALESCE($4::text, status)
            WHERE id = $5::uuid AND tenant_id = $6::uuid
            """,
            _to_dt(next_run),
            now,
            last_result[:500] if last_result else last_result,
            new_status,
            task_id,
            tenant_id,
        )


async def log_task_run(log: TaskRunLog) -> None:
    """Insert a task run log entry."""
    async with tenant_conn(log.tenant_id) as conn:
        await conn.execute(
            """
            INSERT INTO task_run_logs (tenant_id, task_id, run_at, duration_ms, status, result, error)
            VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, $7)
            """,
            log.tenant_id,
            log.task_id,
            _to_dt(log.run_at),
            log.duration_ms,
            log.status,
            log.result[:500] if log.result else log.result,
            log.error,
        )


