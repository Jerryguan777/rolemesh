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
        nc=AsyncMock(),  # type: ignore[arg-type]
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
    def test_creates_all_tools_when_all_flags_enabled(self) -> None:
        # With every flag True the adapter must produce exactly one
        # AgentTool per TOOL_DEFINITIONS entry. We assert against the
        # live definitions rather than a hardcoded number so new tools
        # added to ``TOOL_DEFINITIONS`` are detected by this test.
        ctx = _make_ctx()
        tools = create_rolemesh_tools(
            ctx,
            register_send_message=True,
            register_delegation=True,
            register_task_management=True,
        )
        assert len(tools) == len(TOOL_DEFINITIONS)

    def test_default_omits_gated_tools(self) -> None:
        # With every flag False (a typical specialist agent without
        # delegate / task / scheduled-task permissions), the adapter
        # must drop all four categories: send_message, the delegation
        # pair (delegate_to_agent / list_agents), and the six task
        # lifecycle tools. Only ungated tools (currently:
        # ``submit_proposal``) remain. Pinning this protects the v1.5
        # sub-chip display contract — a specialist must not surface
        # ``delegate_to_agent`` events on the parent UI.
        ctx = _make_ctx()
        tools = create_rolemesh_tools(ctx)
        tool_names = {t.name for t in tools}
        assert "send_message" not in tool_names
        assert "delegate_to_agent" not in tool_names
        assert "list_agents" not in tool_names
        assert "schedule_task" not in tool_names
        assert "list_tasks" not in tool_names
        assert "pause_task" not in tool_names
        assert "resume_task" not in tool_names
        assert "cancel_task" not in tool_names
        assert "update_task" not in tool_names
        # 1 ungated tool currently: submit_proposal. If a new ungated
        # tool lands, this assertion fails and forces a deliberate
        # decision about gating.
        assert tool_names == {"submit_proposal"}

    def test_register_delegation_only_adds_delegation_pair(self) -> None:
        # Frontdesks (agent_delegate=True, task perms also True) must
        # see both delegate_to_agent AND list_agents; a config that
        # turned on agent_delegate without registering list_agents
        # would leave the LLM unable to refresh a stale catalog.
        ctx = _make_ctx()
        tools = create_rolemesh_tools(ctx, register_delegation=True)
        tool_names = {t.name for t in tools}
        assert "delegate_to_agent" in tool_names
        assert "list_agents" in tool_names
        # Should NOT add task tools or send_message
        assert "schedule_task" not in tool_names
        assert "send_message" not in tool_names

    def test_register_task_management_adds_six_task_tools(self) -> None:
        # The six task lifecycle tools must move together — partial
        # registration would let an LLM call schedule_task but not
        # cancel it, stranding scheduled work.
        ctx = _make_ctx()
        tools = create_rolemesh_tools(ctx, register_task_management=True)
        tool_names = {t.name for t in tools}
        for required in ("schedule_task", "list_tasks", "pause_task",
                          "resume_task", "cancel_task", "update_task"):
            assert required in tool_names, f"missing {required}"
        # Should NOT add delegation tools or send_message
        assert "delegate_to_agent" not in tool_names
        assert "send_message" not in tool_names

    def test_flags_are_independent(self) -> None:
        # No flag should leak across categories. Enabling only
        # task_management must not enable delegation, and vice versa.
        ctx = _make_ctx()
        delegation_only = {
            t.name for t in create_rolemesh_tools(ctx, register_delegation=True)
        }
        task_only = {
            t.name for t in create_rolemesh_tools(ctx, register_task_management=True)
        }
        # Their intersection is only the ungated tools
        ungated = {"submit_proposal"}
        assert delegation_only & task_only == ungated

    def test_tool_names_match_definitions(self) -> None:
        ctx = _make_ctx()
        tools = create_rolemesh_tools(
            ctx,
            register_send_message=True,
            register_delegation=True,
            register_task_management=True,
        )
        tool_names = {t.name for t in tools}
        expected = {d["name"] for d in TOOL_DEFINITIONS}
        assert tool_names == expected

    def test_tools_have_descriptions(self) -> None:
        ctx = _make_ctx()
        tools = create_rolemesh_tools(
            ctx,
            register_send_message=True,
            register_delegation=True,
            register_task_management=True,
        )
        for tool in tools:
            assert tool.description, f"Tool '{tool.name}' has no description"

    def test_tools_have_json_schema_parameters(self) -> None:
        ctx = _make_ctx()
        tools = create_rolemesh_tools(
            ctx,
            register_send_message=True,
            register_delegation=True,
            register_task_management=True,
        )
        for tool in tools:
            assert tool.parameters.get("type") == "object", (
                f"Tool '{tool.name}' parameters should be a JSON Schema object"
            )


class TestScheduleTaskViaPiAdapter:
    """Integration: call schedule_task through the Pi adapter to verify
    the full path from AgentTool.execute → rolemesh_tools.schedule_task → result."""

    async def test_permission_denied_propagated_as_error(self) -> None:
        """Defence-in-depth: even when the tool is registered
        (e.g. a misconfig forces ``register_task_management=True``
        without ``task_schedule``), the tool function itself still
        rejects the call with a permission error. Registration-time
        filtering is the first line of defence; this is the second."""
        from agent_runner.tools.context import ToolContext

        ctx = ToolContext(
            js=FakeJetStream(),  # type: ignore[arg-type]
            nc=AsyncMock(),  # type: ignore[arg-type]
            job_id="j",
            chat_jid="c",
            group_folder="g",
            permissions={},  # no task_schedule
            tenant_id="t",
            coworker_id="cw",
            conversation_id="cv",
        )
        # Force registration to bypass the first defence layer; we're
        # exercising the runtime check.
        tools = create_rolemesh_tools(ctx, register_task_management=True)
        schedule_tool = next(t for t in tools if t.name == "schedule_task")

        result = await schedule_tool.execute(
            "call-1",
            {"prompt": "do thing", "schedule_type": "cron", "schedule_value": "* * * * *"},
        )
        assert "[Error]" in result.content[0].text
        assert result.details == {"isError": True}
