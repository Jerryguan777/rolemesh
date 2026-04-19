"""Tests for the submit_proposal tool.

Adversarial cases the tool must NOT silently pass to the orchestrator:
empty action list, blank rationale, non-list actions. Each of those
would produce an approval request that no human can act on sensibly
(nothing to approve / no context), so the tool rejects them with
isError=True instead of publishing a broken NATS message.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

import pytest

from agent_runner.tools.context import ToolContext
from agent_runner.tools.rolemesh_tools import (
    TOOL_DEFINITIONS,
    TOOL_FUNCTIONS,
    submit_proposal,
)


@dataclass
class CapturedPublish:
    subject: str
    data: dict[str, Any]


class FakeJetStream:
    def __init__(self) -> None:
        self.publishes: list[CapturedPublish] = []

    async def publish(self, subject: str, data: bytes) -> None:
        self.publishes.append(CapturedPublish(subject=subject, data=json.loads(data)))


def _ctx(user_id: str = "user-42") -> tuple[ToolContext, FakeJetStream]:
    js = FakeJetStream()
    return (
        ToolContext(
            js=js,  # type: ignore[arg-type]
            job_id="job-1",
            chat_jid="chat-1",
            group_folder="grp",
            permissions={},
            tenant_id="tenant-1",
            coworker_id="cw-1",
            conversation_id="conv-1",
            user_id=user_id,
        ),
        js,
    )


class TestSubmitProposalValidation:
    async def test_empty_actions_rejected(self) -> None:
        ctx, js = _ctx()
        result = await submit_proposal({"actions": [], "rationale": "x"}, ctx)
        assert result.get("isError") is True
        assert js.publishes == []

    async def test_missing_actions_rejected(self) -> None:
        ctx, js = _ctx()
        result = await submit_proposal({"rationale": "x"}, ctx)
        assert result.get("isError") is True
        assert js.publishes == []

    async def test_actions_not_a_list_rejected(self) -> None:
        ctx, js = _ctx()
        result = await submit_proposal(
            {"actions": "not a list", "rationale": "x"}, ctx
        )
        assert result.get("isError") is True
        assert js.publishes == []

    async def test_blank_rationale_rejected(self) -> None:
        ctx, js = _ctx()
        result = await submit_proposal(
            {
                "actions": [{"mcp_server": "erp", "tool_name": "t", "params": {}}],
                "rationale": "   ",
            },
            ctx,
        )
        assert result.get("isError") is True
        assert js.publishes == []

    async def test_missing_rationale_rejected(self) -> None:
        ctx, js = _ctx()
        result = await submit_proposal(
            {"actions": [{"mcp_server": "erp", "tool_name": "t", "params": {}}]},
            ctx,
        )
        assert result.get("isError") is True
        assert js.publishes == []


class TestSubmitProposalForwarding:
    async def test_publishes_to_correct_subject(self) -> None:
        ctx, js = _ctx(user_id="user-77")
        actions = [{"mcp_server": "erp", "tool_name": "refund", "params": {"a": 1}}]
        result = await submit_proposal(
            {"actions": actions, "rationale": "customer complaint"}, ctx
        )
        assert result.get("isError") is None
        await asyncio.sleep(0.05)
        assert len(js.publishes) == 1
        assert js.publishes[0].subject == "agent.job-1.tasks"

    async def test_published_payload_carries_identity_fields(self) -> None:
        ctx, js = _ctx(user_id="user-77")
        await submit_proposal(
            {
                "actions": [{"mcp_server": "e", "tool_name": "t", "params": {}}],
                "rationale": "r",
            },
            ctx,
        )
        await asyncio.sleep(0.05)
        payload = js.publishes[0].data
        assert payload["type"] == "submit_proposal"
        assert payload["tenantId"] == "tenant-1"
        assert payload["coworkerId"] == "cw-1"
        assert payload["conversationId"] == "conv-1"
        assert payload["jobId"] == "job-1"
        assert payload["userId"] == "user-77"
        assert payload["rationale"] == "r"
        assert payload["actions"] == [
            {"mcp_server": "e", "tool_name": "t", "params": {}}
        ]

    async def test_payload_preserves_action_order(self) -> None:
        ctx, js = _ctx()
        actions = [
            {"mcp_server": "s1", "tool_name": "a", "params": {}},
            {"mcp_server": "s2", "tool_name": "b", "params": {}},
            {"mcp_server": "s3", "tool_name": "c", "params": {}},
        ]
        await submit_proposal({"actions": actions, "rationale": "r"}, ctx)
        await asyncio.sleep(0.05)
        assert [a["tool_name"] for a in js.publishes[0].data["actions"]] == ["a", "b", "c"]


class TestSubmitProposalRegistration:
    def test_tool_registered_in_definitions_and_functions(self) -> None:
        # If this fails, backend adapters will not expose submit_proposal,
        # and the feature effectively doesn't exist.
        names = {d["name"] for d in TOOL_DEFINITIONS}
        assert "submit_proposal" in names
        assert "submit_proposal" in TOOL_FUNCTIONS
        assert TOOL_FUNCTIONS["submit_proposal"] is submit_proposal

    def test_schema_declares_required_fields(self) -> None:
        defn = next(d for d in TOOL_DEFINITIONS if d["name"] == "submit_proposal")
        required = set(defn["parameters"]["required"])
        assert required == {"actions", "rationale"}


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
