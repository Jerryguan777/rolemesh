"""IPC task processing logic.

Handles task IPC payloads from agents, regardless of transport.
Authorization uses AgentPermissions checked via authorization functions
at this interception point.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from croniter import croniter

from rolemesh.auth.authorization import can_manage_task, can_schedule_task
from rolemesh.core.logger import get_logger
from rolemesh.core.types import ScheduledTask
from rolemesh.db.pg import (
    DEFAULT_TENANT,
    create_task,
    delete_task,
    get_task_by_id,
    update_task,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from rolemesh.auth.permissions import AgentPermissions

logger = get_logger()


class IpcDeps(Protocol):
    """Dependencies injected into IPC handlers."""

    def send_message(self, jid: str, text: str) -> Awaitable[None]: ...
    def on_tasks_changed(self) -> Awaitable[None]: ...

    # Approval module IPC targets. on_proposal handles an agent-initiated
    # submit_proposal call; on_auto_intercept handles a PreToolUse hook
    # block that needs approval wrapped around it. Both receive the
    # TRUSTED tenant_id/coworker_id resolved by the IPC dispatcher from
    # its in-memory coworker table — the NATS payload's claimed tenantId
    # is only used as a consistency check, never trusted on its own.
    def on_proposal(
        self, data: dict[str, object], *, tenant_id: str, coworker_id: str
    ) -> Awaitable[None]: ...
    def on_auto_intercept(
        self, data: dict[str, object], *, tenant_id: str, coworker_id: str
    ) -> Awaitable[None]: ...


async def process_task_ipc(
    data: dict[str, object],
    source_group: str,
    permissions: AgentPermissions,
    deps: IpcDeps,
    tenant_id: str = DEFAULT_TENANT,
    coworker_id: str = "",
) -> None:
    """Handle a single IPC task request.

    Authorization: permissions-based checks via authorization functions.
    """
    task_type = data.get("type")

    if task_type == "schedule_task":
        prompt = data.get("prompt")
        schedule_type = data.get("schedule_type")
        schedule_value = data.get("schedule_value")
        target_coworker_id = data.get("targetCoworkerId") or coworker_id

        if not (prompt and schedule_type and schedule_value):
            return

        assert isinstance(prompt, str)
        assert isinstance(schedule_type, str)
        assert isinstance(schedule_value, str)
        assert isinstance(target_coworker_id, str)

        # Authorization: must have task:schedule permission
        if not can_schedule_task(permissions):
            logger.warning(
                "Unauthorized schedule_task: task:schedule not permitted",
                source_group=source_group,
            )
            return

        # Authorization: scheduling for another coworker requires task:manage-others
        if target_coworker_id != coworker_id and not can_manage_task(
            permissions, target_coworker_id, coworker_id
        ):
            logger.warning(
                "Unauthorized schedule_task: cannot schedule for other coworker",
                source_group=source_group,
                target_coworker_id=target_coworker_id,
            )
            return

        next_run: str | None = None
        if schedule_type == "cron":
            try:
                cron = croniter(schedule_value, datetime.now(UTC))
                next_run = cron.get_next(datetime).isoformat()
            except (ValueError, KeyError):
                logger.warning("Invalid cron expression", schedule_value=schedule_value)
                return
        elif schedule_type == "interval":
            try:
                ms = int(schedule_value)
            except (ValueError, TypeError):
                logger.warning("Invalid interval", schedule_value=schedule_value)
                return
            if ms <= 0:
                logger.warning("Invalid interval", schedule_value=schedule_value)
                return
            next_run = datetime.fromtimestamp(time.time() + ms / 1000.0, tz=UTC).isoformat()
        elif schedule_type == "once":
            try:
                dt = datetime.fromisoformat(schedule_value)
                next_run = dt.isoformat()
            except (ValueError, TypeError):
                logger.warning("Invalid timestamp", schedule_value=schedule_value)
                return

        raw_task_id = str(data.get("taskId", ""))
        # DB column is UUID — generate one if the agent sent a non-UUID id
        try:
            uuid.UUID(raw_task_id)
            task_id = raw_task_id
        except (ValueError, AttributeError):
            task_id = str(uuid.uuid4())
        raw_context = data.get("context_mode")
        context_mode: str = raw_context if raw_context in ("group", "isolated") else "isolated"  # type: ignore[assignment]

        conversation_id = str(data.get("conversationId", "")) or None

        await create_task(
            ScheduledTask(
                id=task_id,
                tenant_id=tenant_id,
                coworker_id=target_coworker_id,
                conversation_id=conversation_id,
                prompt=prompt,
                schedule_type=schedule_type,  # type: ignore[arg-type]
                schedule_value=schedule_value,
                context_mode=context_mode,  # type: ignore[arg-type]
                next_run=next_run,
                status="active",
                created_at=datetime.now(UTC).isoformat(),
            )
        )
        logger.info(
            "Task created via IPC",
            task_id=task_id,
            source_group=source_group,
            target_coworker_id=target_coworker_id,
            context_mode=context_mode,
        )
        await deps.on_tasks_changed()

    elif task_type == "pause_task":
        task_id_val = data.get("taskId")
        if task_id_val:
            assert isinstance(task_id_val, str)
            task = await get_task_by_id(task_id_val)
            if task and can_manage_task(permissions, task.coworker_id, coworker_id):
                await update_task(task_id_val, status="paused")
                logger.info("Task paused via IPC", task_id=task_id_val, source_group=source_group)
                await deps.on_tasks_changed()
            else:
                logger.warning("Unauthorized task pause attempt", task_id=task_id_val, source_group=source_group)

    elif task_type == "resume_task":
        task_id_val = data.get("taskId")
        if task_id_val:
            assert isinstance(task_id_val, str)
            task = await get_task_by_id(task_id_val)
            if task and can_manage_task(permissions, task.coworker_id, coworker_id):
                await update_task(task_id_val, status="active")
                logger.info("Task resumed via IPC", task_id=task_id_val, source_group=source_group)
                await deps.on_tasks_changed()
            else:
                logger.warning("Unauthorized task resume attempt", task_id=task_id_val, source_group=source_group)

    elif task_type == "cancel_task":
        task_id_val = data.get("taskId")
        if task_id_val:
            assert isinstance(task_id_val, str)
            task = await get_task_by_id(task_id_val)
            if task and can_manage_task(permissions, task.coworker_id, coworker_id):
                await delete_task(task_id_val)
                logger.info("Task cancelled via IPC", task_id=task_id_val, source_group=source_group)
                await deps.on_tasks_changed()
            else:
                logger.warning("Unauthorized task cancel attempt", task_id=task_id_val, source_group=source_group)

    elif task_type == "update_task":
        task_id_val = data.get("taskId")
        if not task_id_val:
            return
        assert isinstance(task_id_val, str)

        task = await get_task_by_id(task_id_val)
        if not task:
            logger.warning("Task not found for update", task_id=task_id_val, source_group=source_group)
            return
        if not can_manage_task(permissions, task.coworker_id, coworker_id):
            logger.warning("Unauthorized task update attempt", task_id=task_id_val, source_group=source_group)
            return

        updates: dict[str, str | None] = {}
        if data.get("prompt") is not None:
            updates["prompt"] = str(data["prompt"])
        if data.get("schedule_type") is not None:
            updates["schedule_type"] = str(data["schedule_type"])
        if data.get("schedule_value") is not None:
            updates["schedule_value"] = str(data["schedule_value"])

        # Recompute next_run if schedule changed
        if data.get("schedule_type") or data.get("schedule_value"):
            updated_schedule_type = updates.get("schedule_type", task.schedule_type)
            updated_schedule_value = updates.get("schedule_value", task.schedule_value)

            if updated_schedule_type == "cron":
                try:
                    assert isinstance(updated_schedule_value, str)
                    cron = croniter(updated_schedule_value, datetime.now(UTC))
                    updates["next_run"] = cron.get_next(datetime).isoformat()
                except (ValueError, KeyError):
                    logger.warning(
                        "Invalid cron in task update",
                        task_id=task_id_val,
                        value=updated_schedule_value,
                    )
                    return
            elif updated_schedule_type == "interval":
                try:
                    assert isinstance(updated_schedule_value, str)
                    ms = int(updated_schedule_value)
                    if ms > 0:
                        updates["next_run"] = datetime.fromtimestamp(time.time() + ms / 1000.0, tz=UTC).isoformat()
                except (ValueError, TypeError):
                    pass

        await update_task(task_id_val, **updates)
        logger.info("Task updated via IPC", task_id=task_id_val, source_group=source_group, updates=updates)
        await deps.on_tasks_changed()

    elif task_type == "submit_proposal":
        # Approval authorization is evaluated inside ApprovalEngine against
        # the resolved_approvers list on the generated request, not on the
        # submitter's AgentPermissions — a low-permission agent may still
        # propose if a policy exists. Pass the orchestrator-trusted
        # tenant_id/coworker_id, not values from the message body.
        await deps.on_proposal(
            data, tenant_id=tenant_id, coworker_id=coworker_id
        )

    elif task_type == "auto_approval_request":
        # The Hook already matched a policy; the orchestrator still
        # revalidates (policy may have been disabled between snapshot and
        # intercept) inside ApprovalEngine.handle_auto_intercept. Trusted
        # IDs are passed explicitly — see on_proposal branch for why.
        await deps.on_auto_intercept(
            data, tenant_id=tenant_id, coworker_id=coworker_id
        )

    else:
        logger.warning("Unknown IPC task type", type=task_type)
