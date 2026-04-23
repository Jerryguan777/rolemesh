"""V2 acceptance scenario B — require_approval bridge to approval module.

Exercises the FULL chain from a container-side safety decision to a
row in ``approval_requests`` with ``source='safety_require_approval'``.
Previously this chain was only unit-tested on each side:

  - ``test_action_override.py`` uses a fake approval handler, so
    ``ApprovalEngine.create_from_safety`` never runs.
  - ``test_rest_to_audit.py`` never creates a require_approval rule.
  - The DB CHECK constraint widening (adding 'safety_require_approval'
    to ``approval_requests.source`` allowed values) has no test.

Things this E2E catches that unit tests miss:

  A. A schema-level regression where the source CHECK constraint is
     not actually widened — the INSERT would 23514 at runtime.
  B. Tenant owner fallback — when no policy matches, we fall back to
     tenant owners. A rename of the query or a role-name typo would
     leave resolved_approvers empty and the request would be dropped
     with a WARNING (no approval_request row).
  C. Approval_context passes through: pipeline attaches
     {tool_name, tool_input, mcp_server_name} on PRE_TOOL_CALL →
     subscriber forwards → engine dispatches → ApprovalEngine sees
     the same fields and uses them for ``actions`` JSONB.
  D. The audit row (safety_decisions) AND the approval_requests row
     both land, and they agree on job_id + tenant_id.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest

from agent_runner.hooks.events import ToolCallEvent
from agent_runner.safety.hook_handler import SafetyHookHandler
from agent_runner.safety.registry import build_container_registry
from rolemesh.approval.engine import ApprovalEngine
from rolemesh.approval.notification import NotificationTargetResolver
from rolemesh.auth.permissions import AgentPermissions
from rolemesh.db import pg
from rolemesh.safety.engine import SafetyEngine
from rolemesh.safety.subscriber import (
    SafetyEventsSubscriber,
    TrustedCoworker,
)

pytestmark = pytest.mark.usefixtures("test_db")


@dataclass
class _FakeToolCtx:
    tenant_id: str
    coworker_id: str
    # All defaults point at real UUIDs so the approval-bridge path
    # (which inserts into approval_requests.{user_id,conversation_id}
    # as UUID FKs) does not 22P02 on a phony test string. The
    # container ctx always has real UUIDs in production.
    job_id: str = "job-appr"
    conversation_id: str = ""
    user_id: str = ""
    group_folder: str = ""
    permissions: dict[str, Any] = field(default_factory=dict)
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def publish(self, subject: str, data: dict[str, Any]) -> None:
        self.events.append((subject, dict(data)))

    def get_tool_reversibility(self, _tool_name: str) -> bool:
        return False


@dataclass(frozen=True)
class _TrustedRec:
    tenant_id: str
    id: str


class _NoopPublisher:
    """Stand-in for NatsPublisher — captures publish calls so the
    test can assert approval.decided.* gets emitted if desired. For
    this E2E we only assert the DB row landed; the publish is a
    downstream concern the approval module's own tests already cover.
    """

    def __init__(self) -> None:
        self.published: list[tuple[str, bytes]] = []

    async def publish(self, subject: str, data: bytes) -> Any:
        self.published.append((subject, data))
        return None


class _NoopChannelSender:
    """No-op ChannelSender — approval notifications are best-effort so
    a dev environment without channel bindings should still let
    ``create_from_safety`` succeed."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    async def send_to_conversation(
        self, conversation_id: str, text: str
    ) -> None:
        self.messages.append((conversation_id, text))


async def _fake_convs_for_user_and_cw(
    _user_id: str, _cw_id: str
) -> list[str]:
    return []


async def _fake_get_conv(_conv_id: str) -> Any:
    return None


def _build_approval_engine() -> tuple[ApprovalEngine, _NoopChannelSender]:
    channel = _NoopChannelSender()
    resolver = NotificationTargetResolver(
        get_conversations_for_user_and_coworker=_fake_convs_for_user_and_cw,
        get_conversation=_fake_get_conv,
    )
    engine = ApprovalEngine(
        publisher=_NoopPublisher(),  # type: ignore[arg-type]
        channel_sender=channel,
        resolver=resolver,
    )
    return engine, channel


class TestRequireApprovalFullLoop:
    @pytest.mark.asyncio
    async def test_safety_require_approval_writes_approval_request(
        self,
    ) -> None:
        """Container fires require_approval verdict → audit event →
        subscriber trust-check → SafetyEngine dispatches to
        ApprovalEngine.create_from_safety → row lands in
        approval_requests with source='safety_require_approval'.
        """
        tenant = await pg.create_tenant(
            name="T", slug=f"t-{uuid.uuid4().hex[:8]}"
        )
        # Tenant owner — approver fallback chain lands here when no
        # policy matches. Without this user the create_from_safety
        # path logs "no tenant owners" and returns None.
        owner = await pg.create_user(
            tenant_id=tenant.id,
            name="Owner",
            email=f"owner-{uuid.uuid4().hex[:6]}@example.com",
            role="owner",
        )
        cw = await pg.create_coworker(
            tenant_id=tenant.id,
            name="cw",
            folder=f"cw-{uuid.uuid4().hex[:8]}",
            permissions=AgentPermissions.for_role("agent"),
        )
        rule = await pg.create_safety_rule(
            tenant_id=tenant.id,
            stage="pre_tool_call",
            check_id="pii.regex",
            config={
                "patterns": {"SSN": True},
                "action_override": "require_approval",
            },
            description="gate SSN tool calls on human approval",
        )

        # Container side: run the snapshot + hook handler with an SSN
        # payload. The override rewrites the check's natural block
        # verdict into require_approval.
        rules = await pg.list_safety_rules_for_coworker(tenant.id, cw.id)
        assert len(rules) == 1
        snapshot = [r.to_snapshot_dict() for r in rules]

        tool_ctx = _FakeToolCtx(
            tenant_id=tenant.id, coworker_id=cw.id, user_id=owner.id
        )
        handler = SafetyHookHandler(
            rules=snapshot,
            registry=build_container_registry(),
            tool_ctx=tool_ctx,  # type: ignore[arg-type]
        )
        event = ToolCallEvent(
            tool_name="mcp__github__create_issue",
            tool_input={"title": "breach", "body": "SSN 123-45-6789"},
        )
        verdict = await handler.on_pre_tool_use(event)
        # Container hook translates require_approval → block for the
        # agent (the tool call is refused this turn) but preserves
        # the distinct verdict_action in the audit payload. The
        # reason field carries the underlying check's explanation
        # (pipeline replaces only the action string on override, not
        # the reason) — this gives operators the real "why" in logs
        # rather than a generic "awaiting approval".
        assert verdict is not None
        assert verdict.block is True
        assert verdict.reason and "PII.SSN" in verdict.reason
        assert tool_ctx.events, "expected audit publish"
        _, audit_payload = tool_ctx.events[0]
        assert audit_payload["verdict_action"] == "require_approval"
        # approval_context attached because stage is pre_tool_call.
        assert "approval_context" in audit_payload
        ap_ctx = audit_payload["approval_context"]
        assert ap_ctx["tool_name"] == "mcp__github__create_issue"
        assert ap_ctx["tool_input"]["title"] == "breach"
        assert ap_ctx["mcp_server_name"] == "github"

        # Orchestrator side: wire real ApprovalEngine into SafetyEngine
        # and feed the audit event through the real subscriber.
        approval_engine, channel = _build_approval_engine()
        safety_engine = SafetyEngine(approval_handler=approval_engine)

        def _lookup(cid: str) -> TrustedCoworker | None:
            if cid == cw.id:
                return _TrustedRec(tenant_id=tenant.id, id=cw.id)
            return None

        subscriber = SafetyEventsSubscriber(
            engine=safety_engine, coworker_lookup=_lookup
        )
        await subscriber.on_message_bytes(
            json.dumps(audit_payload).encode()
        )

        # Audit row lands in safety_decisions with approval_context
        # populated — proves the schema migration (approval_context
        # column) round-trips through the engine.
        decisions = await pg.list_safety_decisions(tenant.id)
        assert len(decisions) == 1
        d = decisions[0]
        assert d["verdict_action"] == "require_approval"
        assert d["stage"] == "pre_tool_call"
        assert d["triggered_rule_ids"] == [rule.id]

        # Approval request row lands with source='safety_require_approval'.
        # This is the load-bearing assertion: without the DB CHECK
        # widening (migration), this INSERT would have raised
        # 23514 CheckViolation.
        pool = pg._get_pool()  # type: ignore[attr-defined]
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, source, status, mcp_server_name, actions, "
                "resolved_approvers, policy_id "
                "FROM approval_requests "
                "WHERE tenant_id = $1::uuid",
                tenant.id,
            )
        assert len(rows) == 1
        ar = rows[0]
        assert ar["source"] == "safety_require_approval"
        assert ar["status"] == "pending"
        assert ar["mcp_server_name"] == "github"
        # policy_id IS NULL for safety-driven requests (design: no
        # approval policy matched, safety rule drove the creation).
        assert ar["policy_id"] is None
        # resolved_approvers contains the tenant owner — fallback
        # chain.
        resolved = [str(u) for u in (ar["resolved_approvers"] or [])]
        assert owner.id in resolved, (
            f"tenant owner {owner.id!r} must appear in approver "
            f"fallback, got {resolved}"
        )
        # actions JSONB carries the original tool_input so the UI
        # can show "approve calling github.create_issue with these
        # exact params?".
        actions = (
            json.loads(ar["actions"])
            if isinstance(ar["actions"], str)
            else ar["actions"]
        )
        assert len(actions) == 1
        assert actions[0]["mcp_server"] == "github"
        assert actions[0]["tool_name"] == "mcp__github__create_issue"
        assert actions[0]["params"]["title"] == "breach"

        # The noop channel sender's messages field proves the path
        # reaches the notification step — useful for a future check
        # if notification resolution changes. We don't assert a
        # message because the resolver has no tenant-owner
        # notification surface yet (design: resolve_for_safety_approvers
        # AttributeError path is the current behavior), so the list
        # should be empty.
        assert channel.messages == []

    @pytest.mark.asyncio
    async def test_dedup_window_prevents_duplicate_approval_requests(
        self,
    ) -> None:
        """Review fix P1-2: an agent retry loop must NOT flood
        approval_requests with duplicates for the same action_hash
        within the 5-minute dedup window.
        """
        tenant = await pg.create_tenant(
            name="T", slug=f"t-{uuid.uuid4().hex[:8]}"
        )
        owner = await pg.create_user(
            tenant_id=tenant.id,
            name="Owner",
            email=f"owner-{uuid.uuid4().hex[:6]}@example.com",
            role="owner",
        )
        cw = await pg.create_coworker(
            tenant_id=tenant.id,
            name="cw",
            folder=f"cw-{uuid.uuid4().hex[:8]}",
        )
        await pg.create_safety_rule(
            tenant_id=tenant.id,
            stage="pre_tool_call",
            check_id="pii.regex",
            config={
                "patterns": {"SSN": True},
                "action_override": "require_approval",
            },
        )
        rules = await pg.list_safety_rules_for_coworker(tenant.id, cw.id)
        snapshot = [r.to_snapshot_dict() for r in rules]

        tool_ctx = _FakeToolCtx(
            tenant_id=tenant.id, coworker_id=cw.id, user_id=owner.id
        )
        handler = SafetyHookHandler(
            rules=snapshot,
            registry=build_container_registry(),
            tool_ctx=tool_ctx,  # type: ignore[arg-type]
        )
        # Simulate the same blocked tool call firing three times —
        # agent retry loop.
        for _ in range(3):
            await handler.on_pre_tool_use(
                ToolCallEvent(
                    tool_name="mcp__github__create_issue",
                    tool_input={"title": "breach", "body": "SSN 123-45-6789"},
                )
            )
        assert len(tool_ctx.events) == 3, "three audit events expected"

        approval_engine, _ = _build_approval_engine()
        safety_engine = SafetyEngine(approval_handler=approval_engine)

        def _lookup(cid: str) -> TrustedCoworker | None:
            if cid == cw.id:
                return _TrustedRec(tenant_id=tenant.id, id=cw.id)
            return None

        subscriber = SafetyEventsSubscriber(
            engine=safety_engine, coworker_lookup=_lookup
        )
        for _, payload in tool_ctx.events:
            await subscriber.on_message_bytes(
                json.dumps(payload).encode()
            )

        # Three audit rows — one per container-side evaluation — OK.
        decisions = await pg.list_safety_decisions(tenant.id)
        assert len(decisions) == 3

        # But ONLY ONE approval_request row — the dedup window should
        # have collapsed the other two into no-ops. Without the fix
        # we'd see 3 pending rows here.
        pool = pg._get_pool()  # type: ignore[attr-defined]
        async with pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM approval_requests "
                "WHERE tenant_id = $1::uuid",
                tenant.id,
            )
        assert count == 1, (
            f"dedup must collapse retry-loop duplicates; got {count} "
            "approval rows"
        )

    @pytest.mark.asyncio
    async def test_no_tenant_owner_logs_and_skips_without_crash(
        self,
    ) -> None:
        """When a tenant has no owner users, create_from_safety logs
        and returns None. The audit row still lands, the approval
        request does NOT — degraded but no cascade failure.
        """
        tenant = await pg.create_tenant(
            name="NoOwner", slug=f"t-{uuid.uuid4().hex[:8]}"
        )
        cw = await pg.create_coworker(
            tenant_id=tenant.id, name="cw",
            folder=f"cw-{uuid.uuid4().hex[:8]}",
        )
        await pg.create_safety_rule(
            tenant_id=tenant.id,
            stage="pre_tool_call",
            check_id="pii.regex",
            config={
                "patterns": {"SSN": True},
                "action_override": "require_approval",
            },
        )
        rules = await pg.list_safety_rules_for_coworker(tenant.id, cw.id)
        snapshot = [r.to_snapshot_dict() for r in rules]

        tool_ctx = _FakeToolCtx(tenant_id=tenant.id, coworker_id=cw.id)
        handler = SafetyHookHandler(
            rules=snapshot,
            registry=build_container_registry(),
            tool_ctx=tool_ctx,  # type: ignore[arg-type]
        )
        await handler.on_pre_tool_use(
            ToolCallEvent(
                tool_name="x",
                tool_input={"body": "SSN 123-45-6789"},
            )
        )
        _, audit_payload = tool_ctx.events[0]

        approval_engine, _ = _build_approval_engine()
        safety_engine = SafetyEngine(approval_handler=approval_engine)

        def _lookup(cid: str) -> TrustedCoworker | None:
            if cid == cw.id:
                return _TrustedRec(tenant_id=tenant.id, id=cw.id)
            return None

        subscriber = SafetyEventsSubscriber(
            engine=safety_engine, coworker_lookup=_lookup
        )
        # Must not raise even though no owner exists.
        await subscriber.on_message_bytes(
            json.dumps(audit_payload).encode()
        )

        # Audit row landed.
        decisions = await pg.list_safety_decisions(tenant.id)
        assert len(decisions) == 1

        # Approval request did NOT land — no approvers, skipped.
        pool = pg._get_pool()  # type: ignore[attr-defined]
        async with pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM approval_requests "
                "WHERE tenant_id = $1::uuid",
                tenant.id,
            )
        assert count == 0

    @pytest.mark.asyncio
    async def test_non_pretool_require_approval_skips_bridge(self) -> None:
        """INPUT_PROMPT require_approval verdicts don't carry an
        approval_context (pipeline only attaches on PRE_TOOL_CALL).
        The bridge must handle the missing context gracefully —
        audit row lands, no approval_request, no crash. Regression
        guard: earlier design mistakenly tried to create an
        approval_request with empty tool_name, which 23514'd against
        a NOT NULL constraint.
        """
        tenant = await pg.create_tenant(
            name="T", slug=f"t-{uuid.uuid4().hex[:8]}"
        )
        await pg.create_user(
            tenant_id=tenant.id,
            name="Owner",
            email=f"owner-{uuid.uuid4().hex[:6]}@example.com",
            role="owner",
        )
        cw = await pg.create_coworker(
            tenant_id=tenant.id, name="cw",
            folder=f"cw-{uuid.uuid4().hex[:8]}",
        )
        await pg.create_safety_rule(
            tenant_id=tenant.id,
            stage="input_prompt",
            check_id="pii.regex",
            config={
                "patterns": {"SSN": True},
                "action_override": "require_approval",
            },
        )
        rules = await pg.list_safety_rules_for_coworker(tenant.id, cw.id)
        snapshot = [r.to_snapshot_dict() for r in rules]

        from agent_runner.hooks.events import UserPromptEvent

        tool_ctx = _FakeToolCtx(tenant_id=tenant.id, coworker_id=cw.id)
        handler = SafetyHookHandler(
            rules=snapshot,
            registry=build_container_registry(),
            tool_ctx=tool_ctx,  # type: ignore[arg-type]
        )
        await handler.on_user_prompt_submit(
            UserPromptEvent(prompt="SSN 123-45-6789")
        )
        _, audit_payload = tool_ctx.events[0]
        assert audit_payload["verdict_action"] == "require_approval"
        # INPUT_PROMPT must NOT attach approval_context — design doc
        # §6.1 scopes it to pre_tool_call only.
        assert "approval_context" not in audit_payload

        approval_engine, _ = _build_approval_engine()
        safety_engine = SafetyEngine(approval_handler=approval_engine)

        def _lookup(cid: str) -> TrustedCoworker | None:
            if cid == cw.id:
                return _TrustedRec(tenant_id=tenant.id, id=cw.id)
            return None

        subscriber = SafetyEventsSubscriber(
            engine=safety_engine, coworker_lookup=_lookup
        )
        # Must not raise (no approval_context = bridge short-circuits).
        await subscriber.on_message_bytes(
            json.dumps(audit_payload).encode()
        )

        # Approval request NOT created — there's no tool_input to
        # surface for human decision on an INPUT_PROMPT event.
        pool = pg._get_pool()  # type: ignore[attr-defined]
        async with pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM approval_requests "
                "WHERE tenant_id = $1::uuid",
                tenant.id,
            )
        assert count == 0
