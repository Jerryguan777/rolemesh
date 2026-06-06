"""Scheduled tasks + task run logs."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from rolemesh.core.types import ScheduledTask, TaskRunLog
from rolemesh.db._pool import _to_dt, admin_conn, tenant_conn

if TYPE_CHECKING:
    import asyncpg

__all__ = [
    "cancel_tasks_for_user",
    "count_tasks",
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
    """Create a new scheduled task.

    ``task.created_by_user_id`` lands on the DB row directly (v6.1
    §P1.7). The orchestrator-side scheduler reads it back when the
    task fires so the run's ``AgentInput.user_id`` carries the
    originating user identity into the audit chain.
    """
    async with tenant_conn(task.tenant_id) as conn:
        await conn.execute(
            """
            INSERT INTO scheduled_tasks (
                id, tenant_id, coworker_id, conversation_id, prompt,
                schedule_type, schedule_value, context_mode, next_run,
                status, created_at, created_by_user_id
            )
            VALUES (
                $1::uuid, $2::uuid, $3::uuid, $4::uuid, $5, $6, $7, $8,
                $9, $10, now(), $11::uuid
            )
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
            task.created_by_user_id,
        )


def _record_to_scheduled_task(row: asyncpg.Record) -> ScheduledTask:
    """Convert an asyncpg.Record to a ScheduledTask dataclass."""
    nr = row["next_run"]
    lr = row["last_run"]
    ca = row["created_at"]
    conv_id = row.get("conversation_id")
    creator = row.get("created_by_user_id")
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
        created_by_user_id=str(creator) if creator is not None else None,
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
    coworker_id: str, *, tenant_id: str, limit: int | None = None, offset: int = 0,
) -> list[ScheduledTask]:
    """Get tasks for a specific coworker (newest first), optionally paginated."""
    sql = (
        "SELECT * FROM scheduled_tasks "
        "WHERE coworker_id = $1::uuid AND tenant_id = $2::uuid "
        "ORDER BY created_at DESC"
    )
    params: list[object] = [coworker_id, tenant_id]
    if limit is not None:
        params.extend((limit, offset))
        sql += f" LIMIT ${len(params) - 1} OFFSET ${len(params)}"
    async with tenant_conn(tenant_id) as conn:
        rows = await conn.fetch(sql, *params)
    return [_record_to_scheduled_task(row) for row in rows]


async def count_tasks(
    tenant_id: str, *, coworker_id: str | None = None,
) -> int:
    """Total scheduled-task count for a tenant (optionally one coworker)."""
    sql = "SELECT COUNT(*) AS n FROM scheduled_tasks WHERE tenant_id = $1::uuid"
    params: list[object] = [tenant_id]
    if coworker_id is not None:
        params.append(coworker_id)
        sql += f" AND coworker_id = ${len(params)}::uuid"
    async with tenant_conn(tenant_id) as conn:
        row = await conn.fetchrow(sql, *params)
    return int(row["n"]) if row else 0


async def get_all_tasks(
    tenant_id: str | None = None, *, limit: int | None = None, offset: int = 0,
) -> list[ScheduledTask]:
    """Get all scheduled tasks, optionally filtered by tenant and paginated.

    Treats both ``None`` and ``""`` as "no tenant scope" so callers
    that build the parameter from an admin REST query string don't
    accidentally pass an empty value into ``tenant_conn`` and trigger
    fail-closed (current_tenant_id = NULL → RLS drops every row).
    """
    if tenant_id:
        sql = (
            "SELECT * FROM scheduled_tasks "
            "WHERE tenant_id = $1::uuid ORDER BY created_at DESC"
        )
        params: list[object] = [tenant_id]
        if limit is not None:
            params.extend((limit, offset))
            sql += f" LIMIT ${len(params) - 1} OFFSET ${len(params)}"
        async with tenant_conn(tenant_id) as conn:
            rows = await conn.fetch(sql, *params)
    else:
        sql = "SELECT * FROM scheduled_tasks ORDER BY created_at DESC"
        params = []
        if limit is not None:
            params.extend((limit, offset))
            sql += " LIMIT $1 OFFSET $2"
        async with admin_conn() as conn:
            rows = await conn.fetch(sql, *params)
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

    Tasks belonging to a *suspended* tenant are excluded from the
    cross-tenant scheduler sweep: a suspended tenant's schedules pause
    (they are neither run nor failed/deleted) and resume on its own once
    the tenant is reactivated — the rows are untouched, just not picked
    up. The join lives here, at the single point the scheduler reads from,
    rather than in the per-tick dispatch loop.
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
        # inv-1-ok: cross-tenant scheduler sweep — the tenant_id-less
        # branch is the documented multi-tenant scheduler path; the
        # caller iterates results to dispatch jobs per-tenant. Runs under
        # admin_conn (BYPASSRLS) so the join to ``tenants`` is visible.
        async with admin_conn() as conn:
            rows = await conn.fetch(
                """
                SELECT st.* FROM scheduled_tasks st
                JOIN tenants t ON t.id = st.tenant_id
                WHERE st.status = 'active' AND t.status = 'active'
                  AND st.next_run IS NOT NULL
                  AND st.next_run <= $1
                ORDER BY st.next_run
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


async def cancel_tasks_for_user(
    user_id: str,
    tenant_id: str,
    *,
    conn: asyncpg.pool.PoolConnectionProxy[asyncpg.Record] | None = None,
) -> int:
    """Soft-cancel every active task whose ``created_by_user_id``
    equals ``user_id`` (v6.1 §P1.8).

    ``status='cancelled'`` slots into the scheduler's existing
    ``WHERE status='active'`` filter (see ``get_due_tasks``) so the
    next tick simply skips the row. The audit row stays alive.

    Optional ``conn`` so the caller can run this inside its own
    transaction — required by ``delete_user`` so the cancel and the
    DELETE land atomically (otherwise a tick between cancel and
    delete would run the task with the just-cancelled user).
    """
    sql = (
        "UPDATE scheduled_tasks "
        "SET status = 'cancelled' "
        "WHERE created_by_user_id = $1::uuid "
        "  AND tenant_id = $2::uuid "
        "  AND status = 'active'"
    )
    if conn is None:
        async with tenant_conn(tenant_id) as c:
            result = await c.execute(sql, user_id, tenant_id)
    else:
        result = await conn.execute(sql, user_id, tenant_id)
    # asyncpg returns the tag string e.g. "UPDATE 3"; the trailing
    # integer is the affected row count. Fall back to 0 on a shape
    # we don't recognise — the caller is using the return only for
    # logging.
    try:
        return int(result.split()[-1])
    except (ValueError, IndexError):
        return 0


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


