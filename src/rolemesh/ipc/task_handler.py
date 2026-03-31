"""IPC task processing logic.

Handles task and message IPC payloads from agents, regardless of transport.
Authorization: admin coworker can manage any task; non-admin coworkers are
restricted to their own scope.
"""

from __future__ import annotations

import random
import string
import time
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from croniter import croniter

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

    from rolemesh.container.runner import AvailableGroup
    from rolemesh.core.types import ChannelBinding, Conversation, Coworker

logger = get_logger()


class IpcDeps(Protocol):
    """Dependencies injected into IPC handlers."""

    def send_message(self, jid: str, text: str) -> Awaitable[None]: ...
    def get_coworker_by_folder(self, tenant_id: str, folder: str) -> Awaitable[Coworker | None]: ...
    def get_channel_binding_for_coworker(
        self, coworker_id: str, channel_type: str
    ) -> Awaitable[ChannelBinding | None]: ...
    def register_conversation(
        self,
        tenant_id: str,
        coworker_id: str,
        channel_binding_id: str,
        channel_chat_id: str,
        name: str | None,
    ) -> Awaitable[Conversation]: ...
    def sync_groups(self, force: bool) -> Awaitable[None]: ...
    def get_available_groups(self) -> Awaitable[list[AvailableGroup]]: ...
    def write_groups_snapshot(
        self,
        tenant_id: str,
        coworker_folder: str,
        is_main: bool,
        available_groups: list[AvailableGroup],
        registered_jids: set[str],
    ) -> None: ...
    def on_tasks_changed(self) -> Awaitable[None]: ...


async def process_task_ipc(
    data: dict[str, object],
    source_group: str,
    is_main: bool,
    deps: IpcDeps,
    tenant_id: str = DEFAULT_TENANT,
    coworker_id: str = "",
) -> None:
    """Handle a single IPC task request.

    Authorization: admin coworker can manage any task; non-admin coworkers
    are restricted to their own scope.
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

        # Authorization: non-admin can only schedule for themselves
        if not is_main and target_coworker_id != coworker_id:
            logger.warning(
                "Unauthorized schedule_task attempt blocked",
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
            if task and (is_main or task.coworker_id == coworker_id):
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
            if task and (is_main or task.coworker_id == coworker_id):
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
            if task and (is_main or task.coworker_id == coworker_id):
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
        if not is_main and task.coworker_id != coworker_id:
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

    elif task_type == "refresh_conversations":
        if is_main:
            logger.info("Conversation refresh requested via IPC", source_group=source_group)
            await deps.sync_groups(True)
            available_groups = await deps.get_available_groups()
            deps.write_groups_snapshot(
                tenant_id,
                source_group,
                True,
                available_groups,
                set(),
            )
        else:
            logger.warning("Unauthorized refresh_conversations attempt blocked", source_group=source_group)

    elif task_type == "register_conversation":
        if not is_main:
            logger.warning("Unauthorized register_conversation attempt blocked", source_group=source_group)
            return

        channel_chat_id = data.get("channel_chat_id")
        name = data.get("name")

        if not channel_chat_id:
            logger.warning("Invalid register_conversation request - missing channel_chat_id", data=data)
            return

        assert isinstance(channel_chat_id, str)

        # Find the coworker from the source context
        coworker = await deps.get_coworker_by_folder(tenant_id, source_group)
        if not coworker:
            logger.warning("Coworker not found for register_conversation", source_group=source_group)
            return

        # Infer channel type from chat_id prefix (or use provided)
        channel_type = str(data.get("channel_type", ""))
        if not channel_type:
            if channel_chat_id.startswith("tg:") or channel_chat_id.lstrip("-").isdigit():
                channel_type = "telegram"
            elif channel_chat_id.startswith("slack:") or channel_chat_id.startswith("C"):
                channel_type = "slack"
            else:
                channel_type = "telegram"

        binding = await deps.get_channel_binding_for_coworker(coworker.id, channel_type)
        if not binding:
            logger.warning(
                "No channel binding for coworker",
                coworker_id=coworker.id,
                channel_type=channel_type,
            )
            return

        await deps.register_conversation(
            tenant_id=tenant_id,
            coworker_id=coworker.id,
            channel_binding_id=binding.id,
            channel_chat_id=channel_chat_id,
            name=str(name) if name else None,
        )
        logger.info(
            "Conversation registered via IPC",
            channel_chat_id=channel_chat_id,
            coworker=coworker.name,
        )

    # Legacy support: register_group still works
    elif task_type == "register_group":
        if not is_main:
            logger.warning("Unauthorized register_group attempt blocked", source_group=source_group)
            return

        channel_chat_id = data.get("jid")
        name = data.get("name")
        if channel_chat_id and name:
            assert isinstance(channel_chat_id, str)
            assert isinstance(name, str)

            coworker = await deps.get_coworker_by_folder(tenant_id, source_group)
            if not coworker:
                logger.warning("Coworker not found for register_group", source_group=source_group)
                return

            # Determine channel type
            channel_type = "telegram"
            if channel_chat_id.startswith("slack:"):
                channel_type = "slack"

            binding = await deps.get_channel_binding_for_coworker(coworker.id, channel_type)
            if not binding:
                logger.warning("No channel binding", coworker_id=coworker.id, channel_type=channel_type)
                return

            # Strip prefix for chat ID
            chat_id = channel_chat_id
            if chat_id.startswith("tg:"):
                chat_id = chat_id[3:]
            elif chat_id.startswith("slack:"):
                chat_id = chat_id[6:]

            await deps.register_conversation(
                tenant_id=tenant_id,
                coworker_id=coworker.id,
                channel_binding_id=binding.id,
                channel_chat_id=chat_id,
                name=name,
            )
            logger.info("Conversation registered via legacy register_group", channel_chat_id=chat_id)
        else:
            logger.warning("Invalid register_group request - missing required fields", data=data)

    elif task_type == "refresh_groups":
        # Legacy: redirect to refresh_conversations
        if is_main:
            logger.info("Legacy refresh_groups redirected to refresh_conversations")
            await deps.sync_groups(True)
            available_groups = await deps.get_available_groups()
            deps.write_groups_snapshot(
                tenant_id,
                source_group,
                True,
                available_groups,
                set(),
            )
        else:
            logger.warning("Unauthorized refresh_groups attempt blocked", source_group=source_group)

    else:
        logger.warning("Unknown IPC task type", type=task_type)
