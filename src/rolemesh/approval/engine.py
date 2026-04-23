"""ApprovalEngine — orchestrator-side lifecycle of an approval request.

Responsibilities are split across this file:

- ``ApprovalEngine`` owns state transitions and talks to NATS.
- ``ApprovalRequestBuilder`` owns request creation + approver resolution
  and calls into the audit layer via the DB trigger (see
  ``_approval_write_audit_from_trigger`` in ``src/rolemesh/db/pg.py``).
- ``_resolve_approvers`` implements the fallback chain
  (policy → assigned users → tenant owners).

The engine does NOT execute MCP calls — that is the Worker's job
(see ``src/rolemesh/approval/executor.py``). Decoupling execution
keeps the decision REST endpoint under ~100ms even for large batches.

The audit trail is written by a Postgres trigger fired on every INSERT
and status-change UPDATE to ``approval_requests``. CRUD functions
(``create_approval_request``, ``decide_approval_request_full``,
``set_approval_status``, …) set the audit context — actor, note,
metadata — through transaction-local GUCs before the DML, so the
trigger and the state change land in the same transaction. Engines
therefore never call ``write_approval_audit`` directly for regular
transitions; they pass ``actor_user_id`` / ``note`` / ``metadata`` to
the CRUD layer.
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
    format_execution_stale_message,
    format_expired_message,
    format_skipped_message,
)

if TYPE_CHECKING:
    from rolemesh.approval.types import ApprovalPolicy, ApprovalRequest

logger = get_logger()


# ---------------------------------------------------------------------------
# Module constants (tunable via env later; kept here so tests can import).
# ---------------------------------------------------------------------------

# How far back to look for a duplicate auto-intercept request. Prevents the
# hook from creating N pending rows if the agent retries the blocked call
# in a tight loop.
_DEDUP_WINDOW_MINUTES = 5

# Default auto-expire when a policy declares none. Chosen so admins with a
# malformed policy still see a bounded pending row.
_DEFAULT_EXPIRE_MINUTES = 60

# Reconcile thresholds — how long a row must have been stuck before the
# maintenance loop acts on it.
_RECONCILE_APPROVED_GRACE_S = 60
_RECONCILE_EXECUTING_GRACE_S = 300


class NatsPublisher(Protocol):
    """Minimal surface the engine needs from the JetStream context."""

    async def publish(self, subject: str, data: bytes) -> Any: ...


# ---------------------------------------------------------------------------
# Error types (translate to REST codes at the API boundary)
# ---------------------------------------------------------------------------


class ConflictError(Exception):
    """Raised when the caller tries to decide an already-resolved request."""

    def __init__(self, current_status: str) -> None:
        super().__init__(f"request already {current_status}")
        self.current_status = current_status


class ForbiddenError(Exception):
    """Caller is authenticated but not in resolved_approvers."""


# ---------------------------------------------------------------------------
# Request builder (SRP: owns the proposal→row translation + notifications)
# ---------------------------------------------------------------------------


class ApprovalRequestBuilder:
    """Creates approval_requests rows and delivers the initial notifications.

    Factored out of ApprovalEngine so state-machine concerns (decide,
    cancel, expire, reconcile) stay separate from "how do we spin up a
    new request from a proposal / intercept." Keeps each class under
    200 lines and makes the dependencies one-way (builder → engine
    holds builder, builder does not know about decisions).
    """

    def __init__(
        self,
        *,
        channel_sender: ChannelSender,
        resolver: NotificationTargetResolver,
    ) -> None:
        self._channel = channel_sender
        self._resolver = resolver

    async def create_from_proposal(
        self,
        *,
        tenant_id: str,
        coworker_id: str,
        conversation_id: str | None,
        user_id: str,
        job_id: str,
        actions: list[dict[str, Any]],
        action_hashes: list[str],
        rationale: str,
        policy: ApprovalPolicy | None,
        approvers: list[str],
    ) -> ApprovalRequest:
        """Create a pending proposal row and notify approvers.

        Callers that know ``approvers == []`` must route to
        ``create_skipped`` instead. ``policy`` may be None for
        proposals that matched no policy (the auto-executed path);
        the engine sets state to 'approved' afterwards.
        """
        mcp_server = str(actions[0].get("mcp_server") or "") if actions else ""
        expiry = _expiry(policy.auto_expire_minutes if policy else None)
        post_exec = policy.post_exec_mode if policy else "report"
        req = await pg.create_approval_request(
            tenant_id=tenant_id,
            coworker_id=coworker_id,
            conversation_id=conversation_id,
            policy_id=policy.id if policy else None,
            user_id=user_id,
            job_id=job_id,
            mcp_server_name=mcp_server,
            actions=actions,
            action_hashes=action_hashes,
            rationale=rationale,
            source="proposal",
            status="pending",
            resolved_approvers=approvers,
            expires_at=expiry,
            post_exec_mode=post_exec,
            actor_user_id=user_id,
        )
        if policy is not None:
            await self._notify_approvers(req, policy)
        return req

    async def create_from_auto_intercept(
        self,
        *,
        tenant_id: str,
        coworker_id: str,
        conversation_id: str | None,
        user_id: str,
        job_id: str,
        server: str,
        tool: str,
        params: dict[str, Any],
        action_hash: str,
        policy: ApprovalPolicy,
        approvers: list[str],
    ) -> ApprovalRequest:
        actions = [{"mcp_server": server, "tool_name": tool, "params": params}]
        req = await pg.create_approval_request(
            tenant_id=tenant_id,
            coworker_id=coworker_id,
            conversation_id=conversation_id,
            policy_id=policy.id,
            user_id=user_id,
            job_id=job_id,
            mcp_server_name=server,
            actions=actions,
            action_hashes=[action_hash],
            rationale=None,
            source="auto_intercept",
            status="pending",
            resolved_approvers=approvers,
            expires_at=_expiry(policy.auto_expire_minutes),
            post_exec_mode=policy.post_exec_mode,
            # actor=None: auto-intercept is a system transition; the
            # trigger records the 'created' audit with NULL actor.
            actor_user_id=None,
        )
        await self._notify_approvers(req, policy)
        return req

    async def create_skipped(
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
    ) -> ApprovalRequest:
        """Create a row that jumps straight to ``skipped`` (no approver).

        The DB audit trigger writes two rows for initial-non-pending
        inserts: 'created' (attributed to the proposer for proposals,
        NULL for auto-intercepts) and 'skipped' (always NULL actor).
        """
        actor = user_id if source == "proposal" else None
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
            actor_user_id=actor,
        )
        await self._send_to_origin(req, format_skipped_message(req))
        return req

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


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


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
        self._builder = ApprovalRequestBuilder(
            channel_sender=channel_sender, resolver=resolver
        )

    # -- Entry points -----------------------------------------------------

    async def handle_proposal(
        self,
        data: dict[str, Any],
        *,
        tenant_id: str,
        coworker_id: str,
    ) -> None:
        """Agent called submit_proposal — possibly multi-action batch.

        ``tenant_id`` / ``coworker_id`` come from the caller's trusted
        lookup (in the IPC dispatcher, from source_cw.config), NOT from
        the NATS payload. Container-provided ``tenantId`` is ignored
        except for a consistency check; a mismatch is logged and the
        message is dropped.
        """
        if not _tenant_matches(data, tenant_id, coworker_id, "proposal"):
            return

        conversation_id = str(data.get("conversationId") or "") or None
        job_id = str(data.get("jobId", ""))
        user_id = str(data.get("userId", ""))
        rationale = str(data.get("rationale") or "")
        actions = data.get("actions") or []
        if not user_id or not actions:
            logger.warning(
                "approval: malformed proposal dropped",
                missing_fields=[
                    k
                    for k, v in {"userId": user_id, "actions": actions}.items()
                    if not v
                ],
            )
            return

        policies = await pg.get_enabled_policies_for_coworker(tenant_id, coworker_id)
        policy_dicts = [p.to_dict() for p in policies]
        matched = find_matching_policies_for_actions(policy_dicts, actions)
        action_hashes = [
            compute_action_hash(
                str(a.get("tool_name", "")), a.get("params") or {}
            )
            for a in actions
        ]

        # Case A: no action matches any policy. Behaviour is governed
        # by the tenant's ``approval_default_mode``:
        #   auto_execute     — legacy: create pending+approved, publish
        #                      decided so the Worker executes.
        #   require_approval — create the row as skipped so admins see
        #                      it; actions never run without an
        #                      explicit policy.
        #   deny             — create the row as rejected (system note);
        #                      Worker just delivers the rejection
        #                      notification.
        if all(m is None for m in matched):
            tenant = await pg.get_tenant(tenant_id)
            default_mode = (
                tenant.approval_default_mode if tenant else "auto_execute"
            )
            req = await self._builder.create_from_proposal(
                tenant_id=tenant_id,
                coworker_id=coworker_id,
                conversation_id=conversation_id,
                user_id=user_id,
                job_id=job_id,
                actions=actions,
                action_hashes=action_hashes,
                rationale=rationale,
                policy=None,
                approvers=[],
            )
            if default_mode == "auto_execute":
                await pg.set_approval_status(req.id, "approved")
                await self._publish_decided(req.id, status="approved", note=None)
            elif default_mode == "require_approval":
                # Move pending → skipped (system transition). The
                # originating conversation is notified; admins must
                # create a policy or decide manually via a side channel.
                await pg.set_approval_status(req.id, "skipped")
                await self._send_to_origin(
                    (await pg.get_approval_request(req.id)) or req,
                    format_skipped_message(req),
                )
            else:  # "deny"
                # Treat as a system rejection. Publish so the Worker
                # delivers a uniform rejection notification; the note
                # explains why.
                note = (
                    "No matching approval policy; this tenant is "
                    "configured for deny-by-default."
                )
                await pg.set_approval_status(
                    req.id, "rejected", note=note
                )
                await self._publish_decided(
                    req.id, status="rejected", note=note
                )
            return

        # Case B: at least one match — approval required.
        strict = _pick_strictest(matched)
        policy = next(p for p in policies if p.id == strict["id"])
        approvers = await self._resolve_approvers(tenant_id, coworker_id, policy)
        mcp_server = str(actions[0].get("mcp_server") or "")

        if not approvers:
            await self._builder.create_skipped(
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

        await self._builder.create_from_proposal(
            tenant_id=tenant_id,
            coworker_id=coworker_id,
            conversation_id=conversation_id,
            user_id=user_id,
            job_id=job_id,
            actions=actions,
            action_hashes=action_hashes,
            rationale=rationale,
            policy=policy,
            approvers=approvers,
        )

    async def handle_auto_intercept(
        self,
        data: dict[str, Any],
        *,
        tenant_id: str,
        coworker_id: str,
    ) -> None:
        """PreToolUse hook blocked a call — wrap approval around it."""
        if not _tenant_matches(data, tenant_id, coworker_id, "auto_intercept"):
            return

        conversation_id = str(data.get("conversationId") or "") or None
        job_id = str(data.get("jobId", ""))
        user_id = str(data.get("userId", ""))
        server = str(data.get("mcp_server_name") or "")
        tool = str(data.get("tool_name") or "")
        params = data.get("tool_params") or {}
        action_hash = str(data.get("action_hash") or "")
        if not user_id or not server or not tool:
            logger.warning("approval: malformed auto_approval_request dropped")
            return

        # Dedup: short-circuit if a pending request with the same
        # action_hash exists within the last DEDUP_WINDOW minutes.
        if action_hash:
            existing = await pg.find_pending_request_by_action_hash(
                tenant_id, action_hash, within_minutes=_DEDUP_WINDOW_MINUTES
            )
            if existing is not None:
                logger.info(
                    "approval: auto-intercept deduped",
                    existing_id=existing.id,
                    action_hash=action_hash,
                )
                return

        # Re-match against the current policy set — the container snapshot
        # may be stale if the admin disabled the policy between init and
        # the hook firing.
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
            await self._builder.create_skipped(
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

        await self._builder.create_from_auto_intercept(
            tenant_id=tenant_id,
            coworker_id=coworker_id,
            conversation_id=conversation_id,
            user_id=user_id,
            job_id=job_id,
            server=server,
            tool=tool,
            params=params,
            action_hash=final_hash,
            policy=policy,
            approvers=approvers,
        )

    # -- Safety framework bridge (V2 P1.1) -------------------------------

    async def create_from_safety(
        self,
        *,
        tenant_id: str,
        coworker_id: str,
        conversation_id: str | None,
        job_id: str,
        user_id: str,
        tool_name: str,
        tool_input: dict[str, Any],
        mcp_server_name: str,
    ) -> ApprovalRequest | None:
        """Create an approval request driven by a safety require_approval
        verdict. Returns the request or None when no approvers resolve
        (the event is logged and skipped — operators see the audit row
        in safety_decisions and can configure tenant owners).

        No policy is associated with this request (``policy_id`` stays
        NULL) because the decision came from a safety rule, not an
        approval policy. Expiry defaults to the module-wide
        ``_DEFAULT_EXPIRE_MINUTES``; approvers fall back to tenant
        owners which matches the chain tail in ``_resolve_approvers``.

        ``user_id`` is required (approval_requests.user_id is NOT NULL
        FK): it identifies the user whose turn triggered the safety
        gate. If the safety event lacks a user_id, we fall back to
        the first tenant owner (approver) — better to attribute the
        request to *some* real user than to 23502 on the insert.
        Callers that want to skip creation when user_id is missing
        can pre-check and short-circuit before calling.
        """
        approvers = await _tenant_owner_ids(tenant_id)
        if not approvers:
            logger.warning(
                "approval: safety require_approval — no tenant owners; "
                "skipping approval creation",
                tenant_id=tenant_id,
                coworker_id=coworker_id,
            )
            return None

        # NOT NULL FK means empty string is a hard error at INSERT
        # time. Fall back to the first approver so the row still
        # lands — the attribution is slightly off but the operator
        # can always see actions/approvers to understand context.
        # V2 P2 review fix: log WARN when fallback fires so operators
        # know approval_requests.user_id != the real requester.
        if user_id:
            requester_id = user_id
        else:
            requester_id = approvers[0]
            logger.warning(
                "approval: safety require_approval without user_id — "
                "attributing to first approver as fallback",
                tenant_id=tenant_id,
                coworker_id=coworker_id,
                fallback_requester=requester_id,
            )

        action = {
            "mcp_server": mcp_server_name,
            "tool_name": tool_name,
            "params": tool_input,
        }
        action_hash = compute_action_hash(tool_name, tool_input)

        # V2 P1 review fix: dedup same as auto-intercept path. An agent
        # in a retry loop can bang the same blocked tool_input back at
        # the pipeline many times per second; without this check we'd
        # spawn one pending approval_request per retry. 5-minute window
        # matches _DEDUP_WINDOW_MINUTES so the two paths behave
        # consistently to operators reading the audit log.
        existing = await pg.find_pending_request_by_action_hash(
            tenant_id, action_hash, within_minutes=_DEDUP_WINDOW_MINUTES
        )
        if existing is not None:
            logger.info(
                "approval: safety require_approval deduped",
                existing_id=existing.id,
                action_hash=action_hash,
                tenant_id=tenant_id,
            )
            return existing

        req = await pg.create_approval_request(
            tenant_id=tenant_id,
            coworker_id=coworker_id,
            conversation_id=conversation_id,
            policy_id=None,
            user_id=requester_id,
            job_id=job_id,
            mcp_server_name=mcp_server_name,
            actions=[action],
            action_hashes=[action_hash],
            rationale=None,
            source="safety_require_approval",
            status="pending",
            resolved_approvers=approvers,
            expires_at=_expiry(None),
            post_exec_mode="report",
            actor_user_id=None,
        )
        # Notification: reuse the approver-request template. Without
        # a policy, we construct a minimal notification that names
        # the tool + coworker. ``resolve_for_safety_approvers`` is
        # the typed surface; previously we reached through a private
        # attribute and caught AttributeError, which also swallowed
        # real bugs inside the resolver.
        note_ctx = await self._resolver.resolve_for_safety_approvers(
            request=req, approver_user_ids=approvers
        )
        if note_ctx.target_conversation_ids:
            message = (
                f"Safety policy requires approval for "
                f"{tool_name or 'tool call'}."
            )
            for conv_id in note_ctx.target_conversation_ids:
                try:
                    await self._channel.send_to_conversation(
                        conv_id, message
                    )
                except Exception as exc:  # noqa: BLE001 — notification best-effort
                    logger.warning(
                        "approval: safety notify failed",
                        conversation_id=conv_id,
                        error=str(exc),
                    )
        return req

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

        Single-query CTE disambiguates 403 (not approver) vs 409 (already
        resolved) vs 200 (success) without a second round-trip. The audit
        row is written atomically inside the same transaction by the DB
        trigger — no "ghost decision" window.
        """
        if action not in ("approve", "reject"):
            raise ValueError(f"Unknown decision action: {action}")
        new_status = "approved" if action == "approve" else "rejected"

        outcome = await pg.decide_approval_request_full(
            request_id,
            new_status=new_status,
            actor_user_id=user_id,
            note=note,
        )
        if outcome.kind == "missing":
            raise LookupError("approval request not found")
        if outcome.kind == "forbidden":
            raise ForbiddenError()
        if outcome.kind == "conflict":
            assert outcome.current_status is not None
            raise ConflictError(outcome.current_status)
        assert outcome.kind == "updated" and outcome.request is not None

        # Fan out to the Worker regardless of outcome. The Worker claims
        # approved rows + executes; for rejected it just delivers the
        # rejection notification. Splitting this way lets the WebUI REST
        # process decide without gateway access.
        await self._publish_decided(request_id, status=new_status, note=note)
        return outcome.request

    async def _publish_decided(
        self, request_id: str, *, status: str, note: str | None
    ) -> None:
        body = json.dumps({"status": status, "note": note}).encode()
        await self._publisher.publish(f"approval.decided.{request_id}", body)

    # -- Cancellation (Stop cascade) --------------------------------------

    async def cancel_for_job(self, job_id: str) -> list[str]:
        """Cancel every pending approval tied to a stopped turn.

        Approved/executing/executed rows are untouched — Stop does not
        un-approve work the user already greenlit. The trigger writes
        one 'cancelled' audit row per cancelled request with NULL actor
        (system transition).
        """
        cancelled = await pg.cancel_pending_approvals_for_job(job_id)
        for req_id in cancelled:
            req = await pg.get_approval_request(req_id)
            if req is None:
                continue
            await self._send_to_origin(req, format_cancelled_message(req))
        return cancelled

    # -- Maintenance loops -----------------------------------------------

    async def expire_stale_requests(self) -> int:
        """Transition pending → expired for rows past their deadline.

        The pending→expired CAS lives in ``pg.expire_approval_if_pending``;
        the trigger writes the 'expired' audit row.
        """
        expired = await pg.list_expired_pending_approvals()
        count = 0
        for req in expired:
            updated = await pg.expire_approval_if_pending(req.id)
            if updated is None:
                # A concurrent decide won the CAS — skip silently.
                continue
            count += 1
            await self._send_to_origin(req, format_expired_message(req))
        return count

    async def reconcile_stuck_requests(self) -> dict[str, int]:
        """Republish missed approvals and surface wedged executions."""
        republished = 0
        stale = 0
        for req in await pg.list_stuck_approved_approvals(
            older_than_seconds=_RECONCILE_APPROVED_GRACE_S
        ):
            # Republish with explicit status so the Worker's default-
            # to-approved behavior is not load-bearing here either.
            await self._publish_decided(req.id, status="approved", note=None)
            republished += 1
        for req in await pg.list_stuck_executing_approvals(
            older_than_seconds=_RECONCILE_EXECUTING_GRACE_S
        ):
            transitioned = await pg.set_approval_status(req.id, "execution_stale")
            if transitioned is None:
                continue
            stale += 1
            # Notify origin with a conservative "may have partially
            # executed" warning. v1 does not persist per-action
            # progress; if batch-level forensics becomes load-bearing,
            # add execution_progress later.
            await self._send_to_origin(
                req, format_execution_stale_message(req)
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
        return await _tenant_owner_ids(tenant_id)

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


# ---------------------------------------------------------------------------
# Non-engine helpers
# ---------------------------------------------------------------------------


def _expiry(auto_expire_minutes: int | None) -> datetime:
    # ``expires_at`` is compared against Postgres ``now()`` at read time
    # (see ``list_expired_pending_approvals``), so Python/DB wallclock
    # drift does not affect correctness; it only shifts the deadline by
    # a few seconds.
    minutes = (
        auto_expire_minutes
        if auto_expire_minutes and auto_expire_minutes > 0
        else _DEFAULT_EXPIRE_MINUTES
    )
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


def _tenant_matches(
    data: dict[str, Any],
    trusted_tenant_id: str,
    trusted_coworker_id: str,
    source: str,
) -> bool:
    """Drop messages whose declared tenant/coworker disagrees with the
    orchestrator's trusted lookup.

    Containers ship (tenantId, coworkerId) in the NATS payload, but the
    orchestrator's outer IPC dispatcher has already resolved those
    authoritatively from its in-memory coworker table. A mismatch
    usually means a buggy container, but in a pathological case it
    could indicate a container trying to submit on another tenant's
    behalf. We log loud and drop — never silently accept.
    """
    claimed_tenant = str(data.get("tenantId", ""))
    claimed_cw = str(data.get("coworkerId", ""))
    if trusted_tenant_id and claimed_tenant and claimed_tenant != trusted_tenant_id:
        logger.warning(
            "approval: dropping message with mismatched tenantId",
            source=source,
            claimed=claimed_tenant,
            trusted=trusted_tenant_id,
        )
        return False
    if trusted_coworker_id and claimed_cw and claimed_cw != trusted_coworker_id:
        logger.warning(
            "approval: dropping message with mismatched coworkerId",
            source=source,
            claimed=claimed_cw,
            trusted=trusted_coworker_id,
        )
        return False
    if not trusted_tenant_id or not trusted_coworker_id:
        logger.warning(
            "approval: dropping message — orchestrator could not determine "
            "trusted tenant/coworker for the sender",
            source=source,
        )
        return False
    return True


__all__ = [
    "ApprovalEngine",
    "ApprovalRequestBuilder",
    "ChannelSender",
    "ConflictError",
    "ForbiddenError",
    "NatsPublisher",
]
