"""Tests for RoleMesh IPC tools — the 7 shared tool functions.

These tests use a fake JetStream that captures publishes, so we verify
the NATS messages each tool produces. Focus on boundary conditions and
the validation logic that was refactored during extraction.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock

import pytest

from agent_runner.tools.context import ToolContext
from agent_runner.tools.rolemesh_tools import (
    cancel_task,
    list_tasks,
    pause_task,
    resume_task,
    schedule_task,
    send_message,
    update_task,
)


@dataclass
class CapturedPublish:
    subject: str
    data: dict[str, Any]


class FakeJetStream:
    """Captures JetStream publishes for assertion."""

    publishes: list[CapturedPublish] = field(default_factory=list)

    def __init__(self) -> None:
        self.publishes = []

    async def publish(self, subject: str, data: bytes) -> None:
        self.publishes.append(CapturedPublish(subject=subject, data=json.loads(data)))

    async def key_value(self, bucket: str) -> Any:
        raise RuntimeError(f"KV bucket '{bucket}' not available in test")


def _make_ctx(
    *,
    can_schedule: bool = True,
    has_tenant_scope: bool = False,
    js: FakeJetStream | None = None,
) -> tuple[ToolContext, FakeJetStream]:
    fake_js = js or FakeJetStream()
    permissions: dict[str, object] = {}
    if can_schedule:
        permissions["task_schedule"] = True
    if has_tenant_scope:
        permissions["data_scope"] = "tenant"
    ctx = ToolContext(
        js=fake_js,  # type: ignore[arg-type]
        job_id="job-123",
        chat_jid="chat-abc",
        group_folder="test-group",
        permissions=permissions,
        tenant_id="tenant-1",
        coworker_id="cw-1",
        conversation_id="conv-1",
    )
    return ctx, fake_js


# ---------------------------------------------------------------------------
# send_message
# ---------------------------------------------------------------------------


class TestSendMessage:
    async def test_basic_send(self) -> None:
        ctx, js = _make_ctx()
        result = await send_message({"text": "hello"}, ctx)
        assert result["content"][0]["text"] == "Message sent."
        assert not result.get("isError")

        # Drain the fire-and-forget task
        import asyncio
        await asyncio.sleep(0.05)

        assert len(js.publishes) == 1
        pub = js.publishes[0]
        assert pub.subject == "agent.job-123.messages"
        assert pub.data["type"] == "message"
        assert pub.data["text"] == "hello"
        assert pub.data["chatJid"] == "chat-abc"
        assert pub.data["tenantId"] == "tenant-1"

    async def test_send_with_sender_override(self) -> None:
        ctx, js = _make_ctx()
        await send_message({"text": "hi", "sender": "Bot"}, ctx)
        import asyncio
        await asyncio.sleep(0.05)

        assert js.publishes[0].data["sender"] == "Bot"

    async def test_send_without_sender(self) -> None:
        ctx, js = _make_ctx()
        await send_message({"text": "hi"}, ctx)
        import asyncio
        await asyncio.sleep(0.05)

        assert "sender" not in js.publishes[0].data


# ---------------------------------------------------------------------------
# schedule_task — validation edge cases
# ---------------------------------------------------------------------------


class TestScheduleTask:
    async def test_permission_denied(self) -> None:
        ctx, _ = _make_ctx(can_schedule=False)
        result = await schedule_task(
            {"prompt": "x", "schedule_type": "cron", "schedule_value": "* * * * *"},
            ctx,
        )
        assert result.get("isError") is True
        assert "Permission denied" in result["content"][0]["text"]

    async def test_invalid_schedule_type(self) -> None:
        ctx, _ = _make_ctx()
        result = await schedule_task(
            {"prompt": "x", "schedule_type": "weekly", "schedule_value": "monday"},
            ctx,
        )
        assert result.get("isError") is True
        assert "Invalid schedule_type" in result["content"][0]["text"]

    async def test_invalid_cron(self) -> None:
        ctx, _ = _make_ctx()
        result = await schedule_task(
            {"prompt": "x", "schedule_type": "cron", "schedule_value": "not-a-cron"},
            ctx,
        )
        assert result.get("isError") is True
        assert "Invalid cron" in result["content"][0]["text"]

    async def test_valid_cron(self) -> None:
        ctx, js = _make_ctx()
        result = await schedule_task(
            {"prompt": "daily check", "schedule_type": "cron", "schedule_value": "0 9 * * *"},
            ctx,
        )
        assert "isError" not in result
        text = result["content"][0]["text"]
        assert "scheduled" in text
        assert "cron" in text

    async def test_interval_zero_rejected(self) -> None:
        ctx, _ = _make_ctx()
        result = await schedule_task(
            {"prompt": "x", "schedule_type": "interval", "schedule_value": "0"},
            ctx,
        )
        assert result.get("isError") is True

    async def test_interval_negative_rejected(self) -> None:
        ctx, _ = _make_ctx()
        result = await schedule_task(
            {"prompt": "x", "schedule_type": "interval", "schedule_value": "-5000"},
            ctx,
        )
        assert result.get("isError") is True

    async def test_interval_non_numeric_rejected(self) -> None:
        ctx, _ = _make_ctx()
        result = await schedule_task(
            {"prompt": "x", "schedule_type": "interval", "schedule_value": "five"},
            ctx,
        )
        assert result.get("isError") is True

    async def test_once_with_z_suffix_rejected(self) -> None:
        ctx, _ = _make_ctx()
        result = await schedule_task(
            {"prompt": "x", "schedule_type": "once", "schedule_value": "2026-01-01T12:00:00Z"},
            ctx,
        )
        assert result.get("isError") is True
        assert "without Z" in result["content"][0]["text"]

    async def test_once_with_timezone_offset_rejected(self) -> None:
        ctx, _ = _make_ctx()
        result = await schedule_task(
            {"prompt": "x", "schedule_type": "once", "schedule_value": "2026-01-01T12:00:00+08:00"},
            ctx,
        )
        assert result.get("isError") is True

    async def test_once_valid_local_time(self) -> None:
        ctx, _ = _make_ctx()
        result = await schedule_task(
            {"prompt": "one-off", "schedule_type": "once", "schedule_value": "2026-06-01T15:30:00"},
            ctx,
        )
        assert "isError" not in result

    async def test_once_invalid_timestamp(self) -> None:
        ctx, _ = _make_ctx()
        result = await schedule_task(
            {"prompt": "x", "schedule_type": "once", "schedule_value": "not-a-date"},
            ctx,
        )
        assert result.get("isError") is True


# ---------------------------------------------------------------------------
# update_task — the cron validation bug fix
# ---------------------------------------------------------------------------


class TestUpdateTask:
    async def test_update_prompt_only(self) -> None:
        """Updating only prompt should not trigger schedule validation."""
        ctx, js = _make_ctx()
        result = await update_task({"task_id": "t-1", "prompt": "new prompt"}, ctx)
        assert "isError" not in result
        assert "update requested" in result["content"][0]["text"]

    async def test_update_schedule_value_without_type_no_false_cron_validation(self) -> None:
        """Bug regression: updating schedule_value alone (no schedule_type)
        should NOT validate as cron. The old code had:
            if (stype == "cron" or (not stype and sval)) and sval:
        which incorrectly treated any sval as cron when stype was None.
        """
        ctx, _ = _make_ctx()
        # This is an interval value, but schedule_type is not provided
        result = await update_task(
            {"task_id": "t-1", "schedule_value": "60000"},
            ctx,
        )
        # Should succeed — no validation should fire without schedule_type
        assert "isError" not in result

    async def test_update_cron_with_invalid_value(self) -> None:
        ctx, _ = _make_ctx()
        result = await update_task(
            {"task_id": "t-1", "schedule_type": "cron", "schedule_value": "bad"},
            ctx,
        )
        assert result.get("isError") is True

    async def test_update_interval_with_negative(self) -> None:
        ctx, _ = _make_ctx()
        result = await update_task(
            {"task_id": "t-1", "schedule_type": "interval", "schedule_value": "-1"},
            ctx,
        )
        assert result.get("isError") is True

    async def test_update_publishes_only_provided_fields(self) -> None:
        ctx, js = _make_ctx()
        await update_task(
            {"task_id": "t-1", "prompt": "updated"},
            ctx,
        )
        import asyncio
        await asyncio.sleep(0.05)

        assert len(js.publishes) == 1
        data = js.publishes[0].data
        assert data["prompt"] == "updated"
        assert "schedule_type" not in data
        assert "schedule_value" not in data


# ---------------------------------------------------------------------------
# list_tasks — KV read behavior
# ---------------------------------------------------------------------------


class TestPauseTask:
    async def test_publishes_correct_type_and_subject(self) -> None:
        ctx, js = _make_ctx()
        result = await pause_task({"task_id": "t-99"}, ctx)
        assert "pause requested" in result["content"][0]["text"]
        import asyncio
        await asyncio.sleep(0.05)

        assert len(js.publishes) == 1
        pub = js.publishes[0]
        assert pub.subject == "agent.job-123.tasks"
        assert pub.data["type"] == "pause_task"
        assert pub.data["taskId"] == "t-99"
        assert pub.data["tenantId"] == "tenant-1"
        assert pub.data["coworkerId"] == "cw-1"
        assert pub.data["groupFolder"] == "test-group"
        assert "timestamp" in pub.data


class TestResumeTask:
    async def test_publishes_correct_type_and_subject(self) -> None:
        ctx, js = _make_ctx()
        result = await resume_task({"task_id": "t-42"}, ctx)
        assert "resume requested" in result["content"][0]["text"]
        import asyncio
        await asyncio.sleep(0.05)

        assert len(js.publishes) == 1
        pub = js.publishes[0]
        assert pub.subject == "agent.job-123.tasks"
        assert pub.data["type"] == "resume_task"
        assert pub.data["taskId"] == "t-42"
        assert pub.data["tenantId"] == "tenant-1"


class TestCancelTask:
    async def test_publishes_correct_type_and_subject(self) -> None:
        ctx, js = _make_ctx()
        result = await cancel_task({"task_id": "t-7"}, ctx)
        assert "cancellation requested" in result["content"][0]["text"]
        import asyncio
        await asyncio.sleep(0.05)

        assert len(js.publishes) == 1
        pub = js.publishes[0]
        assert pub.subject == "agent.job-123.tasks"
        assert pub.data["type"] == "cancel_task"
        assert pub.data["taskId"] == "t-7"
        assert pub.data["tenantId"] == "tenant-1"


# ---------------------------------------------------------------------------
# list_tasks — KV read behavior
# ---------------------------------------------------------------------------


class FakeKV:
    """Fake NATS KV bucket that returns canned data."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    async def get(self, key: str) -> Any:
        return type("Entry", (), {"value": self._data})()


class FakeJetStreamWithKV(FakeJetStream):
    """FakeJetStream that also supports key_value for list_tasks tests."""

    def __init__(self, kv_data: bytes) -> None:
        super().__init__()
        self._kv_data = kv_data

    async def key_value(self, bucket: str) -> FakeKV:
        return FakeKV(self._kv_data)


class TestListTasks:
    async def test_kv_error_returns_error_text(self) -> None:
        """list_tasks reads from NATS KV; if KV is unavailable, returns error."""
        ctx, _ = _make_ctx()
        result = await list_tasks({}, ctx)
        assert "Error reading tasks" in result["content"][0]["text"]

    async def test_returns_formatted_tasks(self) -> None:
        """list_tasks returns formatted task list from KV data."""
        tasks_data = json.dumps([
            {
                "id": "task-1",
                "prompt": "daily backup",
                "schedule_type": "cron",
                "schedule_value": "0 9 * * *",
                "status": "active",
                "next_run": "2026-04-17T09:00:00",
                "coworkerFolder": "test-group",
            },
            {
                "id": "task-2",
                "prompt": "weekly report",
                "schedule_type": "cron",
                "schedule_value": "0 0 * * 1",
                "status": "paused",
                "next_run": "2026-04-21T00:00:00",
                "coworkerFolder": "test-group",
            },
        ]).encode()
        fake_js = FakeJetStreamWithKV(tasks_data)
        ctx, _ = _make_ctx(js=fake_js)
        result = await list_tasks({}, ctx)
        text = result["content"][0]["text"]
        assert "task-1" in text
        assert "task-2" in text
        assert "daily backup" in text
        assert "weekly report" in text

    async def test_empty_tasks_returns_no_tasks_message(self) -> None:
        fake_js = FakeJetStreamWithKV(b"[]")
        ctx, _ = _make_ctx(js=fake_js)
        result = await list_tasks({}, ctx)
        assert "No scheduled tasks" in result["content"][0]["text"]

    async def test_self_scope_filters_own_tasks(self) -> None:
        """Agent without tenant scope only sees its own tasks."""
        tasks_data = json.dumps([
            {"id": "t-mine", "prompt": "my task", "coworkerFolder": "test-group",
             "schedule_type": "cron", "schedule_value": "* * * * *", "status": "active"},
            {"id": "t-other", "prompt": "other task", "coworkerFolder": "other-group",
             "schedule_type": "cron", "schedule_value": "* * * * *", "status": "active"},
        ]).encode()
        fake_js = FakeJetStreamWithKV(tasks_data)
        ctx, _ = _make_ctx(js=fake_js, has_tenant_scope=False)
        result = await list_tasks({}, ctx)
        text = result["content"][0]["text"]
        assert "t-mine" in text
        assert "t-other" not in text

    async def test_tenant_scope_sees_all_tasks(self) -> None:
        """Agent with tenant scope sees all tasks."""
        tasks_data = json.dumps([
            {"id": "t-mine", "prompt": "my task", "coworkerFolder": "test-group",
             "schedule_type": "cron", "schedule_value": "* * * * *", "status": "active"},
            {"id": "t-other", "prompt": "other task", "coworkerFolder": "other-group",
             "schedule_type": "cron", "schedule_value": "* * * * *", "status": "active"},
        ]).encode()
        fake_js = FakeJetStreamWithKV(tasks_data)
        ctx, _ = _make_ctx(js=fake_js, has_tenant_scope=True)
        result = await list_tasks({}, ctx)
        text = result["content"][0]["text"]
        assert "t-mine" in text
        assert "t-other" in text
