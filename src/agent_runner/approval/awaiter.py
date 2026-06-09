"""Container-side block-and-await primitive for HITL approvals.

Both the business-policy approval hook (``hooks.handlers.approval``) and the
safety-pipeline ``require_approval`` bridge (``agent_runner.safety``) need the
exact same dance: mint a ``request_id``, publish ``agent.{job_id}.approval_request``,
block in place on an ``asyncio.Future`` resolved by the orchestrator's
``agent.{job_id}.approval_decision`` relay, and emit ``approval_cancel`` on every
terminal path except a clean approve. This module owns that machinery once so the
two call sites only differ in (a) the request body they supply and (b) how they
map the outcome onto their backend-specific verdict type.

It is intentionally decoupled from NATS: callers inject an awaitable ``publish``
coroutine ``(subject, payload) -> None`` and drive resolution via
:meth:`ApprovalAwaiter.resolve_decision`. That keeps it unit-testable against a
stub broker with no real broker and no Postgres.

Concurrency (docs/12-hitl-approval-architecture.md §6): a single turn can dispatch
multiple tool calls concurrently, so :meth:`await_decision` can be re-entered on
the same awaiter. Each call owns a fresh ``request_id`` and its own ``Future`` in
``_pending``; decisions route back by ``request_id`` so concurrent approvals
never cross wires.

Cleanup (§3.3 / §8 three-layer): the ``finally`` deterministically publishes
``approval_cancel`` on reject / timeout / Stop (``CancelledError``) / exception —
every terminal path except a clean approve, where the orchestrator already
cleared its suspend via the decision. The orchestrator's resume is an idempotent
``set.discard`` so a ``cancel`` that races the ``decision`` it already processed
is a harmless no-op.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    Publisher = Callable[[str, dict[str, Any]], Awaitable[None]]

_log = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _new_request_id() -> str:
    return str(uuid.uuid4())


@dataclass(frozen=True)
class ApprovalDecision:
    """The terminal outcome of an :meth:`ApprovalAwaiter.await_decision` call.

    Exactly one of ``approved`` / ``timed_out`` is true, or both are false for
    an explicit (or unreadable, fail-closed) rejection. ``note`` carries the
    approver's free-text note on a rejection. Callers build their own
    backend-specific verdict + reason string from this so the wording stays
    appropriate to the gate type (business policy vs safety rule).
    """

    approved: bool
    timed_out: bool
    note: str | None = None


class ApprovalAwaiter:
    """Publish an approval request and block until decided / timed out.

    One instance per gate type per run; ``_pending`` is keyed by ``request_id``
    so a single instance safely serves concurrent in-flight requests.
    """

    def __init__(
        self,
        *,
        publish: Publisher,
        job_id: str,
        timeout_ms: int,
        now: Callable[[], datetime] = _utcnow,
        id_factory: Callable[[], str] = _new_request_id,
    ) -> None:
        self._publish = publish
        self._job_id = job_id
        self._timeout_ms = timeout_ms
        self._now = now
        self._id_factory = id_factory
        # request_id -> the Future a decision (or a timeout cancel) resolves.
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}

    async def await_decision(
        self, request_body: dict[str, Any]
    ) -> ApprovalDecision:
        """Publish ``request_body`` as an approval request and await the verdict.

        The awaiter mints ``request_id`` and stamps ``job_id`` / ``requested_at``
        / ``expires_at`` onto the published payload; ``request_body`` supplies
        every gate-specific field (tenant, tool, provenance, ...). The bounded
        wait is ``timeout_ms`` — the in-band fallback for a SIGKILLed
        orchestrator that never answers.
        """
        request_id = self._id_factory()
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[request_id] = fut

        requested_at = self._now()
        expires_at = requested_at + timedelta(milliseconds=self._timeout_ms)

        approved = False
        try:
            await self._publish(
                self._subject("approval_request"),
                {
                    "request_id": request_id,
                    "job_id": self._job_id,
                    **request_body,
                    "requested_at": requested_at.isoformat(),
                    "expires_at": expires_at.isoformat(),
                },
            )

            try:
                decision = await asyncio.wait_for(
                    fut, timeout=self._timeout_ms / 1000
                )
            except TimeoutError:
                _log.info(
                    "approval timed out (request %s)", request_id
                )
                return ApprovalDecision(approved=False, timed_out=True)

            if str(decision.get("decision") or "") == "approve":
                approved = True
                return ApprovalDecision(approved=True, timed_out=False)

            # Any non-approve decision is a deny (fail-closed): a reject, or a
            # malformed decision we cannot read as an explicit approve.
            note = decision.get("note")
            return ApprovalDecision(
                approved=False,
                timed_out=False,
                note=note if isinstance(note, str) and note else None,
            )
        finally:
            self._pending.pop(request_id, None)
            # §3.3: cancel covers reject / timeout / Stop / exception — every
            # terminal path except a clean approve, where the round continues
            # and the orchestrator already cleared suspend via the decision.
            if not approved:
                await self._safe_publish_cancel(request_id)

    def resolve_decision(self, payload: dict[str, Any]) -> bool:
        """Route an ``approval_decision`` payload back to its await point.

        First-wins / idempotent: a decision for an unknown ``request_id``
        (already timed out, already resolved, or never ours) is a no-op and
        returns ``False``. A successfully-routed decision returns ``True``.
        """
        request_id = payload.get("request_id")
        if not isinstance(request_id, str):
            return False
        fut = self._pending.get(request_id)
        if fut is None or fut.done():
            return False
        fut.set_result(payload)
        return True

    async def _safe_publish_cancel(self, request_id: str) -> None:
        """Publish ``approval_cancel`` best-effort; never mask the caller.

        Deliberately a plain ``await`` (not shielded): the publish must record
        even when this runs while the task is being cancelled (Stop). A
        coroutine that completes without suspending runs to completion even
        under a pending ``CancelledError``; the real NATS publish may instead be
        interrupted, in which case the orchestrator's expiry watcher (§8 layer
        3) is the backstop — hence "best-effort". Broker errors are swallowed
        for the same reason; ``CancelledError`` is allowed to propagate so Stop
        still unwinds the turn.
        """
        try:
            await self._publish(
                self._subject("approval_cancel"),
                {"request_id": request_id},
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — cancel is best-effort (§8)
            _log.warning(
                "approval_cancel publish failed for request %s: %s",
                request_id, exc,
            )

    def _subject(self, leaf: str) -> str:
        return f"agent.{self._job_id}.{leaf}"


__all__ = ["ApprovalAwaiter", "ApprovalDecision"]
