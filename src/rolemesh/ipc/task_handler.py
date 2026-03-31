"""IPC task processing logic.

Handles task and message IPC payloads from agents, regardless of transport.
Authorization: main group can manage any task; non-main groups are restricted
to their own group folder.
"""

from __future__ import annotations

import random
import string
import time
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from croniter import croniter

from rolemesh.core.group_folder import is_valid_group_folder
from rolemesh.core.logger import get_logger
from rolemesh.core.types import RegisteredGroup, ScheduledTask
from rolemesh.db.pg import create_task, delete_task, get_task_by_id, update_task

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from rolemesh.container.runner import AvailableGroup

logger = get_logger()


class IpcDeps(Protocol):
    """Dependencies injected into IPC handlers."""

    def send_message(self, jid: str, text: str) -> Awaitable[None]: ...
    def registered_groups(self) -> dict[str, RegisteredGroup]: ...
    def register_group(self, jid: str, group: RegisteredGroup) -> Awaitable[None]: ...
    def sync_groups(self, force: bool) -> Awaitable[None]: ...
    def get_available_groups(self) -> Awaitable[list[AvailableGroup]]: ...
    def write_groups_snapshot(
        self,
        group_folder: str,
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
) -> None:
    """Handle a single IPC task request.

    Authorization: main group can manage any task; non-main groups
    are restricted to their own group folder.
    """
    registered_groups = deps.registered_groups()
    task_type = data.get("type")

    if task_type == "schedule_task":
        prompt = data.get("prompt")
        schedule_type = data.get("schedule_type")
        schedule_value = data.get("schedule_value")
        target_jid = data.get("targetJid")

        if not (prompt and schedule_type and schedule_value and target_jid):
            return

        assert isinstance(target_jid, str)
        assert isinstance(prompt, str)
        assert isinstance(schedule_type, str)
        assert isinstance(schedule_value, str)

        target_group_entry = registered_groups.get(target_jid)
        if target_group_entry is None:
            logger.warning("Cannot schedule task: target group not registered", target_jid=target_jid)
            return

        target_folder = target_group_entry.folder

        # Authorization: non-main groups can only schedule for themselves
        if not is_main and target_folder != source_group:
            logger.warning(
                "Unauthorized schedule_task attempt blocked", source_group=source_group, target_folder=target_folder
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

        await create_task(
            ScheduledTask(
                id=task_id,
                group_folder=target_folder,
                chat_jid=target_jid,
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
            target_folder=target_folder,
            context_mode=context_mode,
        )
        await deps.on_tasks_changed()

    elif task_type == "pause_task":
        task_id_val = data.get("taskId")
        if task_id_val:
            assert isinstance(task_id_val, str)
            task = await get_task_by_id(task_id_val)
            if task and (is_main or task.group_folder == source_group):
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
            if task and (is_main or task.group_folder == source_group):
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
            if task and (is_main or task.group_folder == source_group):
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
        if not is_main and task.group_folder != source_group:
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

    elif task_type == "refresh_groups":
        if is_main:
            logger.info("Group metadata refresh requested via IPC", source_group=source_group)
            await deps.sync_groups(True)
            available_groups = await deps.get_available_groups()
            deps.write_groups_snapshot(
                source_group,
                True,
                available_groups,
                set(registered_groups.keys()),
            )
        else:
            logger.warning("Unauthorized refresh_groups attempt blocked", source_group=source_group)

    elif task_type == "register_group":
        if not is_main:
            logger.warning("Unauthorized register_group attempt blocked", source_group=source_group)
            return

        jid = data.get("jid")
        name = data.get("name")
        folder = data.get("folder")
        trigger = data.get("trigger")

        if jid and name and folder and trigger:
            assert isinstance(jid, str)
            assert isinstance(name, str)
            assert isinstance(folder, str)
            assert isinstance(trigger, str)

            if not is_valid_group_folder(folder):
                logger.warning(
                    "Invalid register_group request - unsafe folder name",
                    source_group=source_group,
                    folder=folder,
                )
                return

            requires_trigger = data.get("requiresTrigger")
            # Defense in depth: agent cannot set isMain via IPC
            await deps.register_group(
                jid,
                RegisteredGroup(
                    name=name,
                    folder=folder,
                    trigger=trigger,
                    added_at=datetime.now(UTC).isoformat(),
                    requires_trigger=bool(requires_trigger) if requires_trigger is not None else True,
                ),
            )
        else:
            logger.warning("Invalid register_group request - missing required fields", data=data)

    else:
        logger.warning("Unknown IPC task type", type=task_type)
