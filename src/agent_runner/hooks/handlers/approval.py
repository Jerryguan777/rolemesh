"""Container-side blocking approval hook (HITL S2).

This is the ``block-and-await`` half of the HITL tool-approval feature
(docs/21-hitl-approval-plan.md §6 / §9 R1). It is a unified
``HookHandler`` registered on the container's ``HookRegistry``; its
``on_pre_tool_use`` runs inside the agent's own ReAct loop:

* Non-``mcp__*`` tools return ``None`` immediately (allow) — §2 scope
  redline: HITL gates external MCP tools only.
* When a tenant ``ApprovalPolicy`` matches the call, the handler
  **publishes** ``agent.{job_id}.approval_request`` and then **blocks in
  place**, awaiting an ``asyncio.Future`` resolved by the orchestrator's
  ``agent.{job_id}.approval_decision`` relay (routed in via
  :meth:`ApprovalHookHandler.resolve_decision`). On ``approve`` it returns
  ``None`` and the tool executes in the same container, same turn — so the
  agent receives the real tool result and keeps reasoning. On ``reject`` or
  on ``APPROVAL_TIMEOUT`` it returns ``ToolCallVerdict(block=True, reason=…)``,
  which the backend surfaces to the model as a denied call.

The await is a bounded ``asyncio.wait_for`` (the in-band fallback for a
SIGKILLed orchestrator that never answers); the hard bound is
``APPROVAL_TIMEOUT`` (core/config.py §5), which the startup assertion keeps
strictly below the container watchdog floor so the watchdog can never
pre-empt a pending approval.

Concurrency (§6 verified model): a single turn can dispatch multiple
``ToolUseBlock``s concurrently (Claude SDK parallel tool calls; Pi
``asyncio.gather`` over the batch), so ``on_pre_tool_use`` can be re-entered
concurrently on the *same* handler instance. Each call owns a fresh
``request_id`` and its own ``Future`` in ``_pending``; decisions route back
by ``request_id`` so concurrent approvals never cross wires.

Cleanup (§3.3 / §8 three-layer): the ``finally`` deterministically publishes
``agent.{job_id}.approval_cancel`` on every terminal path **except a clean
approve** — i.e. reject, timeout, user Stop (``CancelledError``), and
exception. The orchestrator's resume is an idempotent ``set.discard`` so a
``cancel`` that races the ``decision`` it already processed is a harmless
no-op. The container ``finally`` is layer 2; the orchestrator expiry watcher
(§8) is layer 3 for the case where the container is SIGKILLed and ``finally``
never runs, which also makes the ``cancel`` publish best-effort by design.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from agent_runner.approval.policy import (
    ApprovalPolicy,
    evaluate_condition_explained,
    find_matching_policy,
)

from ..events import ToolCallVerdict

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from ..events import ToolCallEvent

    Publisher = Callable[[str, dict[str, Any]], Awaitable[None]]

_log = logging.getLogger(__name__)

# Prefix every MCP tool name carries: ``mcp__<server>__<tool>``.
_MCP_PREFIX = "mcp__"


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _new_request_id() -> str:
    return str(uuid.uuid4())


def parse_mcp_tool_name(tool_name: str) -> tuple[str, str] | None:
    """Split ``mcp__<server>__<tool>`` into ``(server, tool)``.

    Returns ``None`` for any name that is not a well-formed MCP tool name
    (no ``mcp__`` prefix, or no server/tool components). The tool component
    may itself contain ``__`` and is preserved verbatim; only the first two
    ``__`` separators are structural. An empty server or empty tool is
    treated as not-well-formed so a degenerate ``"mcp__"`` does not match a
    server-wide ``"*"`` policy by accident.
    """
    if not tool_name.startswith(_MCP_PREFIX):
        return None
    rest = tool_name[len(_MCP_PREFIX):]
    server, sep, tool = rest.partition("__")
    if not sep or not server or not tool:
        return None
    return server, tool


class ApprovalHookHandler:
    """PreToolUse handler that blocks an MCP call until a human decides.

    Construction is intentionally decoupled from NATS: the handler is given
    a ``publish`` coroutine ``(subject, payload) -> None`` and is driven by
    :meth:`resolve_decision`. This keeps it unit-testable against a stub
    orchestrator (no broker required) and lets ``agent_runner.main`` own the
    actual subscription/publish wiring.
    """

    def __init__(
        self,
        *,
        publish: Publisher,
        policies: Sequence[ApprovalPolicy],
        job_id: str,
        tenant_id: str,
        coworker_id: str,
        conversation_id: str | None = None,
        user_id: str | None = None,
        timeout_ms: int,
        now: Callable[[], datetime] = _utcnow,
        id_factory: Callable[[], str] = _new_request_id,
    ) -> None:
        self._publish = publish
        self._policies = list(policies)
        self._job_id = job_id
        self._tenant_id = tenant_id
        self._coworker_id = coworker_id
        self._conversation_id = conversation_id
        self._user_id = user_id
        self._timeout_ms = timeout_ms
        self._now = now
        self._id_factory = id_factory
        # request_id -> the Future a decision (or a timeout cancel) resolves.
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}

    # -- hook entrypoint ------------------------------------------------

    async def on_pre_tool_use(
        self, event: ToolCallEvent
    ) -> ToolCallVerdict | None:
        tool_name = event.tool_name or ""
        # §2 redline: only MCP tools enter HITL; everything else allows.
        parsed = parse_mcp_tool_name(tool_name)
        if parsed is None:
            return None
        server, tool = parsed
        params = event.tool_input if isinstance(event.tool_input, dict) else {}

        policy = find_matching_policy(
            self._policies,
            mcp_server_name=server,
            tool_name=tool,
            params=params,
        )
        if policy is None:
            # No tenant policy gates this call — allow.
            return None

        # Distinguish a genuine match from a fail-closed match (the policy's
        # condition couldn't be evaluated — usually a typo'd field name, which
        # otherwise silently gates *every* call). On the latter, carry the
        # reason as the approval's rationale so the card / Telegram / log say
        # WHY, instead of leaving the user with a phantom gate. A genuine match
        # leaves the rationale None (the agent-supplied "why" is not wired yet).
        _, gate_reason = evaluate_condition_explained(policy.condition_expr, params)
        rationale: str | None = None
        if gate_reason is not None:
            _log.warning(
                "approval gated by un-evaluable policy %s (%s.%s): %s",
                policy.id, server, tool, gate_reason,
            )
            rationale = (
                f"⚠ This approval policy's condition couldn't be evaluated "
                f"({gate_reason}); the call was gated as a precaution. Check the "
                f"policy's field names."
            )

        return await self._await_decision(server, tool, params, policy, rationale)

    # -- decision routing (called by the NATS decision subscription) ----

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

    # -- internals ------------------------------------------------------

    async def _await_decision(
        self,
        server: str,
        tool: str,
        params: dict[str, Any],
        policy: ApprovalPolicy,
        rationale: str | None = None,
    ) -> ToolCallVerdict | None:
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
                    "tenant_id": self._tenant_id,
                    "coworker_id": self._coworker_id,
                    "conversation_id": self._conversation_id,
                    # Approver = creator; a null user_id is forwarded as-is so
                    # the orchestrator fails closed on it (§3.1).
                    "user_id": self._user_id,
                    "job_id": self._job_id,
                    "policy_id": policy.id,
                    "mcp_server_name": server,
                    "tool_name": tool,
                    "params": params,
                    "action_summary": _action_summary(server, tool, params),
                    # The approval's "why". The agent-supplied rationale is not
                    # wired yet (always None for a genuine match), but a
                    # fail-closed match (un-evaluable condition) passes its
                    # reason here so the user can see why the call was gated.
                    "rationale": rationale,
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
                    "approval timed out for %s.%s (request %s)",
                    server, tool, request_id,
                )
                return ToolCallVerdict(
                    block=True,
                    reason=(
                        f"Approval request for {server}.{tool} timed out after "
                        f"{self._timeout_ms // 1000}s without a decision; the "
                        "tool call was not executed."
                    ),
                )

            if str(decision.get("decision") or "") == "approve":
                approved = True
                return None

            # Any non-approve decision is a deny (fail-closed): a reject, or a
            # malformed decision we cannot read as an explicit approve.
            note = decision.get("note")
            reason = f"Tool call {server}.{tool} was rejected by the approver."
            if isinstance(note, str) and note:
                reason = f"{reason} Note: {note}"
            return ToolCallVerdict(block=True, reason=reason)
        finally:
            self._pending.pop(request_id, None)
            # §3.3: cancel covers reject / timeout / Stop / exception — every
            # terminal path except a clean approve, where the round continues
            # and the orchestrator already cleared suspend via the decision.
            if not approved:
                await self._safe_publish_cancel(request_id)

    async def _safe_publish_cancel(self, request_id: str) -> None:
        """Publish ``approval_cancel`` best-effort; never mask the caller.

        Deliberately a plain ``await`` (not shielded): the publish must
        record even when this runs while the task is being cancelled (Stop).
        A coroutine that completes without suspending runs to completion even
        under a pending ``CancelledError``; the real NATS publish may instead
        be interrupted, in which case the orchestrator's expiry watcher (§8
        layer 3) is the backstop — hence "best-effort". Broker errors are
        swallowed for the same reason; ``CancelledError`` is allowed to
        propagate so Stop still unwinds the turn.
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


def _action_summary(server: str, tool: str, params: dict[str, Any]) -> str:
    """One-line, human-readable summary for the approval card (§3.1).

    Kept short and deterministic — a couple of param keys are appended so the
    approver can tell two calls to the same tool apart, but values are not
    inlined (they may be large or sensitive; the full ``params`` travel in
    their own field for a detail view).
    """
    base = f"{server}.{tool}"
    if not params:
        return base
    keys = ", ".join(sorted(params)[:5])
    return f"{base}({keys})"


# Epoch sentinel for an unparseable ``updated_at`` — keeps the tiebreak in
# find_matching_policy total-orderable (all aware datetimes) while making a
# malformed row lose every recency tiebreak rather than crash the matcher.
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _parse_updated_at(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return _EPOCH
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    return _EPOCH


def policies_from_snapshot(
    raw: Sequence[dict[str, Any]] | None,
) -> list[ApprovalPolicy]:
    """Build ``ApprovalPolicy`` value objects from the init snapshot (§S2).

    Fail-closed on a per-field basis: a missing/odd ``condition_expr`` becomes
    ``{}`` (which the matcher evaluates as a fail-closed match → gate), a
    missing ``enabled`` defaults to ``True``, and an unparseable
    ``updated_at`` falls back to the epoch. Only a row we cannot turn into a
    policy at all (not a dict, or a hard construction error) is dropped — such
    a row carries no usable ``mcp_server_name`` and so could not have gated a
    specific call anyway.
    """
    out: list[ApprovalPolicy] = []
    for item in raw or []:
        if not isinstance(item, dict):
            _log.warning("approval policy snapshot entry is not a dict; skipping")
            continue
        try:
            cond = item.get("condition_expr")
            out.append(
                ApprovalPolicy(
                    id=str(item.get("id", "")),
                    tenant_id=str(item.get("tenant_id", "")),
                    mcp_server_name=str(item.get("mcp_server_name", "")),
                    tool_name=str(item.get("tool_name", "")),
                    condition_expr=cond if isinstance(cond, dict) else {},
                    enabled=bool(item.get("enabled", True)),
                    priority=int(item.get("priority", 0) or 0),
                    updated_at=_parse_updated_at(item.get("updated_at")),
                )
            )
        except Exception as exc:  # noqa: BLE001 — drop only truly unusable rows
            _log.warning("dropping unparseable approval policy snapshot row: %s", exc)
    return out

