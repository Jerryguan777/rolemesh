"""Tests for rolemesh.ipc task handler (extracted from file_transport)."""

from __future__ import annotations

import pytest

from rolemesh.auth.permissions import AgentPermissions
from rolemesh.ipc.task_handler import process_task_ipc

pytestmark = pytest.mark.usefixtures("test_db")


async def test_schedule_task_missing_fields() -> None:
    """schedule_task with missing fields should not create a task."""

    class FakeDeps:
        async def send_message(self, jid: str, text: str) -> None:
            pass

        async def on_tasks_changed(self) -> None:
            pass

    deps = FakeDeps()
    await process_task_ipc(
        {"type": "schedule_task"},  # Missing required fields
        "test-group",
        AgentPermissions.for_role("super_agent"),
        deps,  # type: ignore[arg-type]
    )
    # Should not raise, just silently skip


async def test_unknown_task_type() -> None:
    """Unknown task type should be logged and ignored."""

    class FakeDeps:
        async def send_message(self, jid: str, text: str) -> None:
            pass

        async def on_tasks_changed(self) -> None:
            pass

    deps = FakeDeps()
    await process_task_ipc(
        {"type": "unknown_type"},
        "test-group",
        AgentPermissions.for_role("super_agent"),
        deps,  # type: ignore[arg-type]
    )
