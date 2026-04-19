"""ApprovalEngine — orchestrator-side lifecycle of an approval request.

Responsibilities:
  - Accept proposal / auto-intercept IPC events.
  - Resolve approvers with a fallback chain (policy → assigned users →
    tenant owners) and snapshot the resolved list onto the request so
    later policy edits cannot re-scope approvers of an open request.
  - Persist state transitions atomically via the CRUD layer.
  - Emit audit rows for every state change.
  - Publish ``approval.decided.<id>`` to NATS when an approval succeeds,
    decoupling the REST decision handler from the MCP executor.
  - Notify approvers + originating conversations via an injected
    ChannelSender.

The engine does NOT execute MCP calls — that is the Worker's job
(S8). Decoupling execution keeps the decision REST endpoint under
100ms even for large batches.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

from agent_runner.approval.policy import (
    compute_action_hash,
    find_matching_policies_for_actions,
    find_matching_policy,
)
from rolemesh.core.logger import get_logger
from rolemesh.db import pg

from .notification import (
    ChannelSender,
    NotificationTargetResolver,
    format_approver_request_message,
    format_cancelled_message,
    format_expired_message,
    format_skipped_message,
)

if TYPE_CHECKING:
    from rolemesh.approval.types import ApprovalPolicy, ApprovalRequest

logger = get_logger()


class NatsPublisher(Protocol):
    """Minimal surface the engine needs from the JetStream context."""

    async def publish(self, subject: str, data: bytes) -> Any: ...


class ApprovalEngine:
    def __init__(
        self,
        *,
        publisher: NatsPublisher,
        channel_sender: ChannelSender,
        resolver: NotificationTargetResolver,
    ) -> None:
        self._publisher = publisher
        self._channel = channel_sender
        self._resolver = resolver

    # -- Entry points -----------------------------------------------------

    async def handle_proposal(self, data: dict[str, Any]) -> None:
        """Agent called submit_proposal — possibly multi-action batch."""
        tenant_id = str(data.get("tenantId", ""))
        coworker_id = str(data.get("coworkerId", ""))
        conversation_id = str(data.get("conversationId") or "") or None
        job_id = str(data.get("jobId", ""))
        user_id = str(data.get("userId", ""))
        rationale = str(data.get("rationale") or "")
        actions = data.get("actions") or []
        if not tenant_id or not coworker_id or not user_id or not actions:
            logger.warning(
                "approval: malformed proposal dropped",
                missing_fields=[
                    k
                    for k, v in {
                        "tenantId": tenant_id,
                        "coworkerId": coworker_id,
                        "userId": user_id,
                        "actions": actions,
                    }.items()
                    if not v
                ],
            )
            return

        policies = await pg.get_enabled_policies_for_coworker(tenant_id, coworker_id)
        policy_dicts = [p.to_dict() for p in policies]
        matched = find_matching_policies_for_actions(policy_dicts, actions)

        # Case A: no action matches any policy. Create a request that
        # goes pending -> executed directly and emit an audit trail with
        # both entries, so proposals remain auditable even when they are
        # unconditionally allowed.
        if all(m is None for m in matched):
            await self._create_auto_executed(
                tenant_id=tenant_id,
                coworker_id=coworker_id,
                conversation_id=conversation_id,
                user_id=user_id,
                job_id=job_id,
                actions=actions,
                rationale=rationale,
            )
            return

        # Case B: at least one match. Pick the strictest matching policy
        # across all actions; its approvers + expiry govern the request.
        strict = _pick_strictest(matched)
        policy = next(p for p in policies if p.id == strict["id"])

        approvers = await self._resolve_approvers(tenant_id, coworker_id, policy)
        action_hashes = [
            compute_action_hash(
                str(a.get("tool_name", "")), a.get("params") or {}
            )
            for a in actions
        ]
        mcp_server = str(actions[0].get("mcp_server") or "")

        if not approvers:
            await self._create_skipped(
                tenant_id=tenant_id,
                coworker_id=coworker_id,
                conversation_id=conversation_id,
                user_id=user_id,
                policy=policy,
                job_id=job_id,
                actions=actions,
                action_hashes=action_hashes,
                rationale=rationale,
                source="proposal",
                mcp_server=mcp_server,
            )
            return

        req = await pg.create_approval_request(
            tenant_id=tenant_id,
            coworker_id=coworker_id,
            conversation_id=conversation_id,
            policy_id=policy.id,
            user_id=user_id,
            job_id=job_id,
            mcp_server_name=mcp_server,
            actions=actions,
            action_hashes=action_hashes,
            rationale=rationale,
            source="proposal",
            status="pending",
            resolved_approvers=approvers,
            expires_at=_expiry(policy.auto_expire_minutes),
            post_exec_mode=policy.post_exec_mode,
        )
        await pg.write_approval_audit(
            request_id=req.id, action="created", actor_user_id=user_id
        )
        await self._notify_approvers(req, policy)

    async def handle_auto_intercept(self, data: dict[str, Any]) -> None:
        """PreToolUse hook blocked a call — wrap approval around it."""
        tenant_id = str(data.get("tenantId", ""))
        coworker_id = str(data.get("coworkerId", ""))
        conversation_id = str(data.get("conversationId") or "") or None
        job_id = str(data.get("jobId", ""))
        user_id = str(data.get("userId", ""))
        server = str(data.get("mcp_server_name") or "")
        tool = str(data.get("tool_name") or "")
        params = data.get("tool_params") or {}
        action_hash = str(data.get("action_hash") or "")
        if not tenant_id or not coworker_id or not user_id or not server or not tool:
            logger.warning("approval: malformed auto_approval_request dropped")
            return

        # Dedup: short-circuit if a pending request with the same
        # action_hash exists within the last 5 minutes. Prevents the
        # hook from creating duplicates when the agent retries the
        # blocked call in a tight loop.
        if action_hash:
            existing = await pg.find_pending_request_by_action_hash(
                tenant_id, action_hash, within_minutes=5
            )
            if existing is not None:
                logger.info(
                    "approval: auto-intercept deduped",
                    existing_id=existing.id,
                    action_hash=action_hash,
                )
                return

        # Re-match against the current policy set — the in-container
        # snapshot may be stale if the admin disabled the policy between
        # init and the hook firing.
        policies = await pg.get_enabled_policies_for_coworker(tenant_id, coworker_id)
        policy_dicts = [p.to_dict() for p in policies]
        if not isinstance(params, dict):
            params = {}
        policy_match = find_matching_policy(policy_dicts, server, tool, params)
        if policy_match is None:
            logger.info(
                "approval: policy no longer matches on orchestrator side; "
                "dropping auto_approval_request",
                server=server,
                tool=tool,
            )
            return

        policy = next(p for p in policies if p.id == policy_match["id"])
        approvers = await self._resolve_approvers(tenant_id, coworker_id, policy)

        actions = [{"mcp_server": server, "tool_name": tool, "params": params}]
        final_hash = action_hash or compute_action_hash(tool, params)

        if not approvers:
            await self._create_skipped(
                tenant_id=tenant_id,
                coworker_id=coworker_id,
                conversation_id=conversation_id,
                user_id=user_id,
                policy=policy,
                job_id=job_id,
                actions=actions,
                action_hashes=[final_hash],
                rationale=None,
                source="auto_intercept",
                mcp_server=server,
            )
            return

        req = await pg.create_approval_request(
            tenant_id=tenant_id,
            coworker_id=coworker_id,
            conversation_id=conversation_id,
            policy_id=policy.id,
            user_id=user_id,
            job_id=job_id,
            mcp_server_name=server,
            actions=actions,
            action_hashes=[final_hash],
            rationale=None,
            source="auto_intercept",
            status="pending",
            resolved_approvers=approvers,
            expires_at=_expiry(policy.auto_expire_minutes),
            post_exec_mode=policy.post_exec_mode,
        )
        # actor_user_id = None: auto-intercept is a system transition.
        await pg.write_approval_audit(
            request_id=req.id, action="created", actor_user_id=None
        )
        await self._notify_approvers(req, policy)

    # -- Decision path ----------------------------------------------------

    async def handle_decision(
        self,
        *,
        request_id: str,
        action: str,
        user_id: str,
        note: str | None = None,
    ) -> ApprovalRequest:
        """Process an approve/reject decision.

        Race-safe: decide_approval_request uses a single-statement
        pending→approved|rejected UPDATE and returns None to surface
        both concurrency conflicts and unauthorised approvers as the
        same fall-through; callers map to the right HTTP code.
        """
        if action not in ("approve", "reject"):
            raise ValueError(f"Unknown decision action: {action}")
        new_status = "approved" if action == "approve" else "rejected"

        updated = await pg.decide_approval_request(
            request_id, new_status=new_status, actor_user_id=user_id
        )
        if updated is None:
            # Disambiguate: is the caller unauthorised, or has the
            # request already been resolved? We read back; if still
            # pending, user is not an approver.
            current = await pg.get_approval_request(request_id)
            if current is None:
                raise LookupError("approval request not found")
            if current.status != "pending":
                raise ConflictError(current.status)
            raise ForbiddenError()

        await pg.write_approval_audit(
            request_id=request_id,
            action=new_status,
            actor_user_id=user_id,
            note=note,
        )

        # Publish a decision event regardless of outcome. The Worker
        # (orchestrator process) owns both executing approved actions
        # AND sending rejection notifications to the originating
        # conversation — this split lets WebUI decide endpoints work
        # even though only the orchestrator holds gateway handles.
        #
        # Payload shape:
        #   {"status": "approved"|"rejected", "note": "..." | None}
        body = json.dumps({"status": new_status, "note": note}).encode()
        await self._publisher.publish(
            f"approval.decided.{request_id}", body
        )

        return updated

    # -- Cancellation (Stop cascade) --------------------------------------

    async def cancel_for_job(self, job_id: str) -> list[str]:
        """Cancel every pending approval tied to a stopped turn.

        Approved/executing/executed rows are untouched — Stop does not
        un-approve work the user already greenlit.
        """
        cancelled = await pg.cancel_pending_approvals_for_job(job_id)
        for req_id in cancelled:
            await pg.write_approval_audit(
                request_id=req_id, action="cancelled", actor_user_id=None
            )
            req = await pg.get_approval_request(req_id)
            if req is None:
                continue
            await self._send_to_origin(req, format_cancelled_message(req))
        return cancelled

    # -- Maintenance loops -----------------------------------------------

    async def expire_stale_requests(self) -> int:
        """Transition pending → expired for rows past their deadline."""
        expired = await pg.list_expired_pending_approvals()
        count = 0
        for req in expired:
            # Race: use decide_approval_request's sibling? No — that
            # requires an approver. Use unconditional status set
            # protected by the "pending" filter inside the SQL
            # maintenance query would be ideal, but set_approval_status
            # has no filter and we want to avoid trampling a concurrent
            # decide. Do a second pending check via decide_approval_request
            # equivalent: issue a conditional UPDATE via SQL on status.
            updated = await pg.expire_approval_if_pending(req.id)
            if updated is None:
                continue
            count += 1
            await pg.write_approval_audit(
                request_id=req.id, action="expired", actor_user_id=None
            )
            await self._send_to_origin(req, format_expired_message(req))
        return count

    async def reconcile_stuck_requests(self) -> dict[str, int]:
        """Republish missed approvals and surface wedged executions."""
        republished = 0
        stale = 0
        for req in await pg.list_stuck_approved_approvals(older_than_seconds=60):
            await self._publisher.publish(
                f"approval.decided.{req.id}", b"{}"
            )
            republished += 1
        for req in await pg.list_stuck_executing_approvals(older_than_seconds=300):
            transitioned = await pg.set_approval_status(req.id, "execution_stale")
            if transitioned is None:
                continue
            stale += 1
            await pg.write_approval_audit(
                request_id=req.id, action="execution_stale", actor_user_id=None
            )
        return {"republished": republished, "stale": stale}

    # -- Internal helpers -------------------------------------------------

    async def _resolve_approvers(
        self, tenant_id: str, coworker_id: str, policy: ApprovalPolicy
    ) -> list[str]:
        """Fallback chain: policy → assigned users → tenant owners."""
        if policy.approver_user_ids:
            return list(policy.approver_user_ids)
        assigned = await pg.get_users_for_agent(coworker_id)
        if assigned:
            return [u.id for u in assigned]
        owners = await _tenant_owner_ids(tenant_id)
        return owners

    async def _notify_approvers(
        self, request: ApprovalRequest, policy: ApprovalPolicy
    ) -> None:
        ctx = await self._resolver.resolve_for_approvers(
            request=request, policy=policy
        )
        message = format_approver_request_message(
            request=request, policy=policy, approval_url=ctx.approval_url
        )
        for conv_id in ctx.target_conversation_ids:
            try:
                await self._channel.send_to_conversation(conv_id, message)
            except Exception as exc:  # noqa: BLE001 — notification best-effort
                logger.warning(
                    "approval: notify failed",
                    conversation_id=conv_id,
                    error=str(exc),
                )

    async def _send_to_origin(
        self, request: ApprovalRequest, message: str
    ) -> None:
        if not request.conversation_id:
            return
        try:
            await self._channel.send_to_conversation(
                request.conversation_id, message
            )
        except Exception as exc:  # noqa: BLE001 — notification best-effort
            logger.warning(
                "approval: origin notify failed",
                conversation_id=request.conversation_id,
                error=str(exc),
            )

    async def _create_auto_executed(
        self,
        *,
        tenant_id: str,
        coworker_id: str,
        conversation_id: str | None,
        user_id: str,
        job_id: str,
        actions: list[dict[str, Any]],
        rationale: str,
    ) -> None:
        """Create a proposal that short-circuits to executed.

        Handled by the engine so the execution path is identical to the
        approve-then-execute flow — including the audit entries — even
        when no policy matches. The Worker still runs the actions so
        that an admin rewinding audit history sees a consistent shape
        regardless of whether the proposal was gated.
        """
        # Synthesize a "null" policy row reference: the SQL requires a
        # non-null policy_id. Pick the lowest-priority enabled tenant-wide
        # policy if present; otherwise create a disabled placeholder. We
        # choose the simplest path: require at least one policy in the
        # tenant; otherwise we cannot satisfy the FK, and we log + drop.
        placeholder = await _pick_any_policy_for_tenant(tenant_id, coworker_id)
        if placeholder is None:
            logger.info(
                "approval: proposal with no matching policy and no tenant "
                "policies — dropping, no audit trail possible"
            )
            return
        action_hashes = [
            compute_action_hash(
                str(a.get("tool_name", "")), a.get("params") or {}
            )
            for a in actions
        ]
        mcp_server = str(actions[0].get("mcp_server") or "")
        req = await pg.create_approval_request(
            tenant_id=tenant_id,
            coworker_id=coworker_id,
            conversation_id=conversation_id,
            policy_id=placeholder.id,
            user_id=user_id,
            job_id=job_id,
            mcp_server_name=mcp_server,
            actions=actions,
            action_hashes=action_hashes,
            rationale=rationale,
            source="proposal",
            status="pending",
            resolved_approvers=[],
            expires_at=_expiry(placeholder.auto_expire_minutes),
        )
        await pg.write_approval_audit(
            request_id=req.id, action="created", actor_user_id=user_id
        )
        # Jump to executed by publishing a decided event. The Worker will
        # claim + execute. No decision audit row for executed-because-nothing-
        # -matched: this is a system transition.
        await pg.set_approval_status(req.id, "approved")
        await self._publisher.publish(f"approval.decided.{req.id}", b"{}")

    async def _create_skipped(
        self,
        *,
        tenant_id: str,
        coworker_id: str,
        conversation_id: str | None,
        user_id: str,
        policy: ApprovalPolicy,
        job_id: str,
        actions: list[dict[str, Any]],
        action_hashes: list[str],
        rationale: str | None,
        source: str,
        mcp_server: str,
    ) -> None:
        req = await pg.create_approval_request(
            tenant_id=tenant_id,
            coworker_id=coworker_id,
            conversation_id=conversation_id,
            policy_id=policy.id,
            user_id=user_id,
            job_id=job_id,
            mcp_server_name=mcp_server,
            actions=actions,
            action_hashes=action_hashes,
            rationale=rationale,
            source=source,
            status="skipped",
            resolved_approvers=[],
            expires_at=_expiry(policy.auto_expire_minutes),
        )
        # "created" audit first so the order is proposal-made → skipped.
        actor = user_id if source == "proposal" else None
        await pg.write_approval_audit(
            request_id=req.id, action="created", actor_user_id=actor
        )
        await pg.write_approval_audit(
            request_id=req.id, action="skipped", actor_user_id=None
        )
        await self._send_to_origin(req, format_skipped_message(req))


# ---------------------------------------------------------------------------
# Error types
# ---------------------------------------------------------------------------


class ConflictError(Exception):
    """Raised when the caller tries to decide an already-resolved request."""

    def __init__(self, current_status: str) -> None:
        super().__init__(f"request already {current_status}")
        self.current_status = current_status


class ForbiddenError(Exception):
    """Caller is authenticated but not in resolved_approvers."""


# ---------------------------------------------------------------------------
# Non-engine helpers (kept module-private so they can be refactored without
# a public interface change).
# ---------------------------------------------------------------------------


def _expiry(auto_expire_minutes: int | None) -> datetime:
    minutes = auto_expire_minutes if auto_expire_minutes and auto_expire_minutes > 0 else 60
    return datetime.now(UTC) + timedelta(minutes=minutes)


def _pick_strictest(matched: list[dict[str, Any] | None]) -> dict[str, Any]:
    """Pick the strictest matching policy across non-None entries."""
    from agent_runner.approval.policy import select_strictest_policy

    real = [m for m in matched if m is not None]
    # real is guaranteed non-empty by the caller.
    return select_strictest_policy(real)


async def _tenant_owner_ids(tenant_id: str) -> list[str]:
    """Return user_ids of tenant owners."""
    users = await pg.get_users_for_tenant(tenant_id)
    return [u.id for u in users if u.role == "owner"]


async def _pick_any_policy_for_tenant(
    tenant_id: str, coworker_id: str
) -> ApprovalPolicy | None:
    """Last-ditch placeholder when a proposal has no matching policy.

    Returned policy is used only as the FK target — no approver
    resolution, no expiry semantics — so the audit trail is preserved.
    """
    policies = await pg.get_enabled_policies_for_coworker(tenant_id, coworker_id)
    if policies:
        return policies[0]
    all_for_tenant = await pg.list_approval_policies(tenant_id)
    return all_for_tenant[0] if all_for_tenant else None


__all__ = [
    "ApprovalEngine",
    "ChannelSender",
    "ConflictError",
    "ForbiddenError",
    "NatsPublisher",
]

# Silence ruff's unused-import: json is used by callers importing the
# serialized payload shape. Keep for future use.
_ = json
