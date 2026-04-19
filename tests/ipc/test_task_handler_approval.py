"""Tests for the submit_proposal / auto_approval_request task routes.

Isolated from the rest of process_task_ipc: we feed the dispatcher a
dict with type="submit_proposal" (or auto_approval_request) and a
recording stub for IpcDeps, then assert on_proposal / on_auto_intercept
was called exactly once with the same payload. The goal is to pin down
the dispatch contract so a refactor cannot silently rename or drop the
routing keys.
"""

from __future__ import annotations

from typing import Any

from rolemesh.auth.permissions import AgentPermissions
from rolemesh.ipc.task_handler import process_task_ipc


class _RecordingDeps:
    def __init__(self) -> None:
        self.proposals: list[tuple[dict[str, Any], str, str]] = []
        self.auto: list[tuple[dict[str, Any], str, str]] = []
        self.sent_messages: list[tuple[str, str]] = []
        self.tasks_changed_count = 0

    async def send_message(self, jid: str, text: str) -> None:
        self.sent_messages.append((jid, text))

    async def on_tasks_changed(self) -> None:
        self.tasks_changed_count += 1

    async def on_proposal(
        self, data: dict[str, Any], *, tenant_id: str, coworker_id: str
    ) -> None:
        self.proposals.append((data, tenant_id, coworker_id))

    async def on_auto_intercept(
        self, data: dict[str, Any], *, tenant_id: str, coworker_id: str
    ) -> None:
        self.auto.append((data, tenant_id, coworker_id))


async def test_submit_proposal_routes_to_on_proposal() -> None:
    deps = _RecordingDeps()
    payload = {
        "type": "submit_proposal",
        "actions": [{"mcp_server": "erp", "tool_name": "t", "params": {"a": 1}}],
        "rationale": "r",
        "tenantId": "tenant-1",
        "coworkerId": "cw-1",
        "conversationId": "conv-1",
        "jobId": "job-1",
        "userId": "user-1",
    }
    await process_task_ipc(
        data=payload,
        source_group="grp",
        permissions=AgentPermissions(),
        deps=deps,
        tenant_id="tenant-1",
        coworker_id="cw-1",
    )
    assert len(deps.proposals) == 1
    assert deps.proposals[0][0] == payload
    assert deps.proposals[0][1] == "tenant-1"
    assert deps.proposals[0][2] == "cw-1"
    assert deps.auto == []


async def test_auto_approval_request_routes_to_on_auto_intercept() -> None:
    deps = _RecordingDeps()
    payload = {
        "type": "auto_approval_request",
        "mcp_server_name": "erp",
        "tool_name": "refund",
        "tool_params": {"amount": 5000},
        "action_hash": "hash-x",
        "policy_id": "policy-1",
        "tenantId": "tenant-1",
        "coworkerId": "cw-1",
        "conversationId": "conv-1",
        "jobId": "job-1",
        "userId": "user-1",
    }
    await process_task_ipc(
        data=payload,
        source_group="grp",
        permissions=AgentPermissions(),
        deps=deps,
        tenant_id="tenant-1",
        coworker_id="cw-1",
    )
    assert len(deps.auto) == 1
    assert deps.auto[0][0] == payload
    assert deps.auto[0][1] == "tenant-1"
    assert deps.auto[0][2] == "cw-1"
    assert deps.proposals == []


async def test_unknown_types_do_not_route_to_approval() -> None:
    deps = _RecordingDeps()
    await process_task_ipc(
        data={"type": "something_else"},
        source_group="grp",
        permissions=AgentPermissions(),
        deps=deps,
        tenant_id="tenant-1",
        coworker_id="cw-1",
    )
    assert deps.proposals == []
    assert deps.auto == []
