"""Orchestrator-side HITL approval coordinator (docs/12-hitl-approval-architecture.md §8).

The container blocks an MCP tool call in place and publishes
``agent.{job_id}.approval_request``; the orchestrator must, for the lifetime
of that bounded wait:

* **Suspend reaping** so the held container is not idle-killed (all three
  reaping paths, §8) — done via :class:`GroupQueue`.
* **Persist** the request as the source of truth (the in-memory map is a cache
  rebuilt on restart).
* **Resume + relay** when a decision arrives (forward it to the container over
  ``agent.{job_id}.approval_decision``) or when the container cancels (S2 sends
  a cancel on reject / timeout / Stop / exception, §3.3).
* **Expire** a request whose deadline passes with no answer — the SIGKILLed-
  container backstop where the container's own ``finally`` never ran.
* **Recover** on restart: ``_groups`` is in-memory and empty after a restart,
  so pending rows must be re-adopted, re-suspended, and re-armed (R2).

This module owns none of the NATS plumbing or DB session management — it takes
a :class:`GroupQueue`, an :class:`ApprovalPersistence` bundle, a tenant
resolver, and a decision publisher. That keeps the race-prone state machine
unit-testable against a real ``GroupQueue`` + an in-memory store, with no
broker and no Postgres.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from rolemesh.core.config import APPROVAL_TIMEOUT
from rolemesh.core.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from rolemesh.container.scheduler import GroupQueue
    from rolemesh.db.approval import ApprovalRequest

logger = get_logger()

__all__ = [
    "ApprovalCoordinator",
    "ApprovalPersistence",
    "approval_queue_key",
    "db_persistence",
]


def approval_queue_key(conversation_id: str | None, coworker_id: str) -> str:
    """The §5 queue key: ``conversation_id`` if bound, else ``coworker_id``.

    Must match ``task_scheduler._compute_queue_key`` and the messaging side so
    the container and its approval suspend state land on the same
    ``_GroupState`` entry. Do not reinvent this rule.
    """
    return conversation_id or coworker_id


def _parse_dt(value: Any) -> datetime | None:
    """Parse an ISO-8601 string (or pass a datetime through), else ``None``."""
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return None
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    return None


@dataclass(frozen=True)
class ApprovalPersistence:
    """The three DB operations the coordinator needs, as injectable callables.

    Bundled so tests can substitute an in-memory store that faithfully mirrors
    the first-wins idempotency of ``resolve_approval_request`` (its
    ``WHERE status='pending'`` guard) without standing up Postgres.
    """

    create_request: Callable[..., Awaitable[ApprovalRequest]]
    resolve_request: Callable[..., Awaitable[ApprovalRequest | None]]
    list_pending_all: Callable[[], Awaitable[list[ApprovalRequest]]]


def db_persistence() -> ApprovalPersistence:
    """Production :class:`ApprovalPersistence` backed by ``rolemesh.db.approval``."""
    from rolemesh.db.approval import (
        create_approval_request,
        list_pending_requests_all_tenants,
        resolve_approval_request,
    )

    return ApprovalPersistence(
        create_request=create_approval_request,
        resolve_request=resolve_approval_request,
        list_pending_all=list_pending_requests_all_tenants,
    )


@dataclass
class _PendingApproval:
    """In-memory cache row for one pending approval (rebuilt on restart)."""

    request_id: str
    job_id: str
    key: str
    tenant_id: str
    coworker_id: str
    conversation_id: str | None
    expires_at: datetime
    adopted: bool = False
    expiry_handle: asyncio.TimerHandle | None = None


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


class ApprovalCoordinator:
    """Drives suspend / resume / expiry / restart-recovery for HITL approvals."""

    def __init__(
        self,
        *,
        queue: GroupQueue,
        persistence: ApprovalPersistence,
        resolve_tenant: Callable[[str], str | None],
        publish_decision: Callable[[str, dict[str, Any]], Awaitable[None]],
        now: Callable[[], datetime] = _utcnow,
        notify_status: Callable[[ApprovalRequest], Awaitable[None]] | None = None,
        notify_hard: Callable[[ApprovalRequest, str], Awaitable[None]] | None = None,
    ) -> None:
        self._queue = queue
        self._persistence = persistence
        self._resolve_tenant = resolve_tenant
        self._publish_decision = publish_decision
        self._now = now
        # Soft "⏳ waiting" signal (web status) and hard-channel card
        # (reject/expired) are wired by S4; the coordinator only invokes them.
        self._notify_status = notify_status
        self._notify_hard = notify_hard
        self._pending: dict[str, _PendingApproval] = {}
        self._bg: set[asyncio.Task[None]] = set()

    # -- inbound from the container --------------------------------------

    async def on_approval_request(self, payload: dict[str, Any]) -> None:
        """Handle ``agent.*.approval_request``: persist + suspend reaping (§8)."""
        if not isinstance(payload, dict):
            return
        try:
            request_id = str(payload["request_id"])
            job_id = str(payload["job_id"])
            coworker_id = str(payload["coworker_id"])
            mcp_server_name = str(payload["mcp_server_name"])
            tool_name = str(payload["tool_name"])
        except (KeyError, TypeError):
            logger.warning("approval_request missing required fields; dropping")
            return

        if request_id in self._pending:
            # JetStream redelivery of a request we already suspended. Idempotent:
            # keep the existing suspend / expiry; do not double-persist.
            return

        conversation_id = payload.get("conversation_id") or None
        user_id = payload.get("user_id") or None
        policy_id = payload.get("policy_id") or None
        raw_params = payload.get("params")
        params = raw_params if isinstance(raw_params, dict) else {}
        action_summary = payload.get("action_summary")
        raw_rationale = payload.get("rationale")
        rationale = raw_rationale if isinstance(raw_rationale, str) else None
        # Safety-rule provenance (§3.10): present only when the container's
        # safety hook raised the request; a business-policy approval omits it.
        raw_triggered_by = payload.get("triggered_by")
        triggered_by = raw_triggered_by if isinstance(raw_triggered_by, dict) else None
        key = approval_queue_key(conversation_id, coworker_id)

        # Server-side truth for the tenant; the payload value is only a fallback
        # for a coworker not yet in memory (mirrors the _handle_tasks pattern).
        tenant_id = self._resolve_tenant(coworker_id)
        if not tenant_id and payload.get("tenant_id"):
            tenant_id = str(payload["tenant_id"])
        if not tenant_id:
            logger.warning(
                "approval_request: cannot resolve tenant; failing closed",
                request_id=request_id,
                coworker_id=coworker_id,
            )
            # No tenant ⇒ no RLS-scoped persistence possible. Tell the container
            # to block. Nothing was suspended, so nothing to resume.
            await self._safe_publish_decision(
                job_id, request_id, "reject", None, "Unresolvable tenant (fail-closed)"
            )
            return

        expires_at = _parse_dt(payload.get("expires_at")) or (
            self._now() + timedelta(milliseconds=APPROVAL_TIMEOUT)
        )

        # Suspend BEFORE the persist await so an idle timer already counting down
        # cannot reap the held container in the gap.
        self._queue.suspend_for_approval(key, request_id)
        pending = _PendingApproval(
            request_id=request_id,
            job_id=job_id,
            key=key,
            tenant_id=tenant_id,
            coworker_id=coworker_id,
            conversation_id=conversation_id,
            expires_at=expires_at,
        )
        self._pending[request_id] = pending
        self._arm_expiry(pending)

        try:
            req = await self._persistence.create_request(
                tenant_id=tenant_id,
                coworker_id=coworker_id,
                job_id=job_id,
                mcp_server_name=mcp_server_name,
                action={"tool_name": tool_name, "params": params},
                expires_at=expires_at,
                conversation_id=conversation_id,
                policy_id=policy_id,
                user_id=user_id,
                action_summary=action_summary,
                rationale=rationale,
                triggered_by=triggered_by,
                request_id=request_id,
            )
        except Exception:
            logger.exception(
                "approval_request: persist failed; resuming", request_id=request_id
            )
            self._resolve_local(request_id)
            return

        # Fail-closed on a null approver: there is no one who may decide, so
        # reject immediately rather than holding the container for the full
        # timeout (§3.1 / S2 handoff).
        if user_id is None:
            logger.info(
                "approval_request has no approver; rejecting (fail-closed)",
                request_id=request_id,
            )
            await self.decide(
                request_id, decision="reject", decided_by=None,
                note="No approver (fail-closed)",
            )
            return

        if self._notify_status is not None:
            with contextlib.suppress(Exception):
                await self._notify_status(req)

    async def on_approval_cancel(self, payload: dict[str, Any]) -> None:
        """Handle ``agent.*.approval_cancel``: resume reaping, idempotently.

        S2 emits a cancel on reject too (not only timeout/Stop), so a cancel may
        arrive for a request a decision already resolved. ``_pending`` absence
        makes that a clean no-op — no double resume, no mis-clear.
        """
        if not isinstance(payload, dict):
            return
        request_id = payload.get("request_id")
        if not isinstance(request_id, str):
            return
        pending = self._pending.get(request_id)
        if pending is None:
            return
        # The container initiated this; mark the row cancelled (first-wins) but
        # do NOT publish a decision back.
        row: ApprovalRequest | None = None
        with contextlib.suppress(Exception):
            row = await self._persistence.resolve_request(
                request_id, tenant_id=pending.tenant_id, status="cancelled"
            )
        self._resolve_local(request_id)
        # Flip the card in place for the user (same hard-channel path as
        # reject/expired). Only when *this* cancel won the pending→terminal
        # transition — a cancel racing a decision/expiry resolves zero rows and
        # the winner already (or will) emit its own terminal event.
        if row is not None and self._notify_hard is not None:
            with contextlib.suppress(Exception):
                await self._notify_hard(row, "cancelled")

    # -- decision intake (S4 channels call this; also used internally) ----

    async def decide(
        self,
        request_id: str,
        *,
        decision: str,
        decided_by: str | None,
        note: str | None = None,
        expected_tenant_id: str | None = None,
        expected_conversation_id: str | None = None,
    ) -> bool:
        """Apply a human decision: persist, relay to the container, resume.

        Returns ``True`` only when this call won the pending→terminal
        transition. If the request already went terminal (expired / cancelled /
        a racing duplicate), the resolve writes zero rows and we deliberately do
        NOT forward an ``approve`` — running a tool the user never authorised for
        this round is the failure mode we are guarding against (§8 decision
        race).

        IDOR guard (S4): ``request_id`` is a bare UUID on the wire (Telegram
        ``callback_data`` / a web decision frame). A channel that authenticated
        an approver passes ``expected_tenant_id`` (and, when it knows it, the
        ``expected_conversation_id`` the approver owns); a request whose pending
        row does not match is refused **before** any DB write or relay, so a
        guessed/forged ``request_id`` can never decide another tenant's — or
        another conversation's — approval. Omitting the guards (internal
        callers: fail-closed reject, expiry) keeps the legacy behaviour.
        """
        pending = self._pending.get(request_id)
        if pending is None:
            return False
        if expected_tenant_id is not None and pending.tenant_id != expected_tenant_id:
            logger.warning(
                "approval decide: tenant mismatch — refusing (IDOR guard)",
                request_id=request_id,
            )
            return False
        if (
            expected_conversation_id is not None
            and pending.conversation_id != expected_conversation_id
        ):
            logger.warning(
                "approval decide: conversation mismatch — refusing (IDOR guard)",
                request_id=request_id,
            )
            return False
        status = "approved" if decision == "approve" else "rejected"
        row: ApprovalRequest | None = None
        try:
            row = await self._persistence.resolve_request(
                request_id, tenant_id=pending.tenant_id, status=status,
                decided_by=decided_by, note=note,
            )
        except Exception:
            logger.exception("approval decide: resolve failed", request_id=request_id)
        if row is not None:
            await self._safe_publish_decision(
                pending.job_id, request_id, decision, decided_by, note
            )
        self._resolve_local(request_id)
        if row is not None and status == "rejected" and self._notify_hard is not None:
            with contextlib.suppress(Exception):
                await self._notify_hard(row, "rejected")
        return row is not None

    # -- expiry watcher (SIGKILLed-container backstop) --------------------

    def _arm_expiry(self, pending: _PendingApproval) -> None:
        delay = max(0.0, (pending.expires_at - self._now()).total_seconds())
        loop = asyncio.get_running_loop()
        pending.expiry_handle = loop.call_later(delay, self._fire_expiry, pending.request_id)

    def _fire_expiry(self, request_id: str) -> None:
        task = asyncio.ensure_future(self._handle_expiry(request_id))
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    async def _handle_expiry(self, request_id: str) -> None:
        pending = self._pending.get(request_id)
        if pending is None:
            return
        row: ApprovalRequest | None = None
        try:
            row = await self._persistence.resolve_request(
                request_id, tenant_id=pending.tenant_id, status="expired"
            )
        except Exception:
            logger.exception("approval expiry: resolve failed", request_id=request_id)
        # Resume regardless: even if a decision/cancel already won the row, our
        # local suspend must be released. No decision is relayed on expiry — a
        # live container's own bounded wait fires independently and blocks.
        self._resolve_local(request_id)
        if row is not None and self._notify_hard is not None:
            with contextlib.suppress(Exception):
                await self._notify_hard(row, "expired")

    # -- restart recovery (R2) -------------------------------------------

    async def recover_pending(self) -> None:
        """Rebuild suspend state for pending rows after a restart (R2 / §8).

        ``_groups`` is empty after a restart but a container blocked on a
        pending approval may still be alive. For each not-yet-expired row:
        re-adopt the container into the queue, replay the suspend, restore the
        decision route (``job_id`` lives on the row), and re-arm the expiry. A
        row only reloaded *without* rebuilding the suspend would let the
        recovered container be reaped immediately. Past-deadline rows are marked
        expired + hard-notified.

        Operational note: the default Docker runtime force-removes agent
        containers via ``cleanup_orphans`` at startup, so in that deployment the
        re-adopted container is already gone — recovery then degrades safely
        (the expiry watcher fires, the row is marked expired, and the adopted
        state is torn down so the conversation is not wedged). Keeping
        approval-held containers alive across a restart is an ops change tracked
        for S4 and out of S3 scope.
        """
        try:
            rows = await self._persistence.list_pending_all()
        except Exception:
            logger.exception("approval restart recovery: scan failed")
            return
        now = self._now()
        recovered = 0
        expired = 0
        for row in rows:
            if row.id in self._pending:
                continue
            if row.expires_at <= now:
                with contextlib.suppress(Exception):
                    resolved = await self._persistence.resolve_request(
                        row.id, tenant_id=row.tenant_id, status="expired"
                    )
                    if resolved is not None and self._notify_hard is not None:
                        await self._notify_hard(resolved, "expired")
                expired += 1
                continue
            key = approval_queue_key(row.conversation_id, row.coworker_id)
            self._queue.adopt_orphan_container(
                key, job_id=row.job_id, tenant_id=row.tenant_id,
                coworker_id=row.coworker_id,
            )
            self._queue.suspend_for_approval(key, row.id)
            pending = _PendingApproval(
                request_id=row.id,
                job_id=row.job_id,
                key=key,
                tenant_id=row.tenant_id,
                coworker_id=row.coworker_id,
                conversation_id=row.conversation_id,
                expires_at=row.expires_at,
                adopted=True,
            )
            self._pending[row.id] = pending
            self._arm_expiry(pending)
            recovered += 1
        if recovered or expired:
            logger.info(
                "approval restart recovery complete",
                recovered=recovered, expired=expired,
            )

    # -- internals -------------------------------------------------------

    def _resolve_local(self, request_id: str) -> None:
        """Drop the cache row, cancel its expiry, and resume queue reaping."""
        pending = self._pending.pop(request_id, None)
        if pending is None:
            return
        if pending.expiry_handle is not None:
            pending.expiry_handle.cancel()
            pending.expiry_handle = None
        self._queue.resume_from_approval(pending.key, request_id)

    async def _safe_publish_decision(
        self,
        job_id: str,
        request_id: str,
        decision: str,
        decided_by: str | None,
        note: str | None,
    ) -> None:
        payload = {
            "request_id": request_id,
            "decision": decision,
            "decided_by": decided_by,
            "note": note,
        }
        try:
            await self._publish_decision(job_id, payload)
        except Exception:
            logger.exception(
                "failed to publish approval_decision",
                request_id=request_id, job_id=job_id,
            )
