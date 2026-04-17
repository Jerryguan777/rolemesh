"""Tests for the Pi tool adapter — verifies AgentTool wrapping and isError propagation.

The pi_adapter wraps shared rolemesh_tools functions as Pi AgentTool instances.
Key concern: the isError flag must be correctly propagated so the LLM sees
tool failures as errors, not successes.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from agent_runner.tools.pi_adapter import RoleMeshAgentTool, create_rolemesh_tools
from agent_runner.tools.rolemesh_tools import TOOL_DEFINITIONS, TOOL_FUNCTIONS


class FakeJetStream:
    async def publish(self, subject: str, data: bytes) -> None:
        pass

    async def key_value(self, bucket: str) -> Any:
        raise RuntimeError("not available")


def _make_ctx() -> Any:
    from agent_runner.tools.context import ToolContext

    return ToolContext(
        js=FakeJetStream(),  # type: ignore[arg-type]
        job_id="j-1",
        chat_jid="c-1",
        group_folder="grp",
        permissions={"task_schedule": True},
        tenant_id="t-1",
        coworker_id="cw-1",
        conversation_id="conv-1",
    )


class TestRoleMeshAgentTool:
    async def test_success_result_has_text(self) -> None:
        """Normal tool result — text is passed through."""

        async def fake_fn(args: dict, ctx: Any) -> dict:
            return {"content": [{"type": "text", "text": "ok"}]}

        tool = RoleMeshAgentTool("test", "desc", {"type": "object"}, fake_fn, _make_ctx())
        result = await tool.execute("call-1", {})
        assert result.content[0].text == "ok"
        assert result.details is None

    async def test_error_result_prefixed_and_flagged(self) -> None:
        """Tool returns isError=True — text should be prefixed with [Error]."""

        async def fake_fn(args: dict, ctx: Any) -> dict:
            return {"content": [{"type": "text", "text": "Permission denied"}], "isError": True}

        tool = RoleMeshAgentTool("test", "desc", {"type": "object"}, fake_fn, _make_ctx())
        result = await tool.execute("call-1", {})
        assert result.content[0].text == "[Error] Permission denied"
        assert result.details == {"isError": True}

    async def test_missing_isError_treated_as_success(self) -> None:
        """No isError key — should be treated as success (no prefix)."""

        async def fake_fn(args: dict, ctx: Any) -> dict:
            return {"content": [{"type": "text", "text": "data"}]}

        tool = RoleMeshAgentTool("test", "desc", {"type": "object"}, fake_fn, _make_ctx())
        result = await tool.execute("call-1", {})
        assert result.content[0].text == "data"
        assert result.details is None

    async def test_empty_content_list(self) -> None:
        """Edge case: tool returns empty content list."""

        async def fake_fn(args: dict, ctx: Any) -> dict:
            return {"content": []}

        tool = RoleMeshAgentTool("test", "desc", {"type": "object"}, fake_fn, _make_ctx())
        result = await tool.execute("call-1", {})
        # Should not crash — returns empty text
        assert result.content[0].text == ""


class TestCreateRoleMeshTools:
    def test_creates_all_seven_tools(self) -> None:
        ctx = _make_ctx()
        tools = create_rolemesh_tools(ctx)
        assert len(tools) == 7

    def test_tool_names_match_definitions(self) -> None:
        ctx = _make_ctx()
        tools = create_rolemesh_tools(ctx)
        tool_names = {t.name for t in tools}
        expected = {d["name"] for d in TOOL_DEFINITIONS}
        assert tool_names == expected

    def test_tools_have_descriptions(self) -> None:
        ctx = _make_ctx()
        tools = create_rolemesh_tools(ctx)
        for tool in tools:
            assert tool.description, f"Tool '{tool.name}' has no description"

    def test_tools_have_json_schema_parameters(self) -> None:
        ctx = _make_ctx()
        tools = create_rolemesh_tools(ctx)
        for tool in tools:
            assert tool.parameters.get("type") == "object", (
                f"Tool '{tool.name}' parameters should be a JSON Schema object"
            )


class TestScheduleTaskViaPiAdapter:
    """Integration: call schedule_task through the Pi adapter to verify
    the full path from AgentTool.execute → rolemesh_tools.schedule_task → result."""

    async def test_permission_denied_propagated_as_error(self) -> None:
        """Agent without task_schedule permission gets isError response."""
        from agent_runner.tools.context import ToolContext

        ctx = ToolContext(
            js=FakeJetStream(),  # type: ignore[arg-type]
            job_id="j",
            chat_jid="c",
            group_folder="g",
            permissions={},  # no task_schedule
            tenant_id="t",
            coworker_id="cw",
            conversation_id="cv",
        )
        tools = create_rolemesh_tools(ctx)
        schedule_tool = next(t for t in tools if t.name == "schedule_task")

        result = await schedule_tool.execute(
            "call-1",
            {"prompt": "do thing", "schedule_type": "cron", "schedule_value": "* * * * *"},
        )
        assert "[Error]" in result.content[0].text
        assert result.details == {"isError": True}
