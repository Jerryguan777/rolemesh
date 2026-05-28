"""Notification target resolution + formatting for approval events.

Keep this module narrow: it does NOT own the sending transport, only
the "who gets the message and what does it say" logic. The caller
(ApprovalEngine) uses a ChannelSender protocol to actually push the
text to Telegram/Slack/WebUI. That split keeps the engine mockable.

Target resolution order (first non-empty wins):
  1. policy.notify_conversation_id — operator explicitly configured
     a conversation for this policy's approvals.
  2. Approver's most recent conversation with this coworker — we reach
     the approver where they already talk to the coworker, so the
     channel is already wired up.
  3. The originating conversation (last-ditch fallback).

The v1 contract deliberately does NOT include editing/updating prior
messages — cancellation and expiry send NEW messages. Pinning that in
a comment so the next pass doesn't silently regress by trying to track
per-approver message IDs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from rolemesh.core.logger import get_logger

if TYPE_CHECKING:
    from rolemesh.approval.types import ApprovalPolicy, ApprovalRequest

logger = get_logger()


@dataclass(frozen=True)
class ApprovalCardPayload:
    """Structured approval card surfaced to channel gateways.

    v6.1 §P2.7 — channels that can render interactive buttons (Web,
    Telegram in Phase 2b) ship the card as native UI. Channels that
    cannot (Slack-without-bot, plain email, etc.) degrade gracefully
    via :func:`deliver_approval_card_or_text` to a text body that
    includes the Web deep-link.
    """

    request_id: str
    title: str
    summary: str
    text_fallback: str
    approval_url: str | None = None


class ChannelSender(Protocol):
    """How the engine reaches a conversation.

    Kept as a Protocol so the orchestrator can hand in its
    channel-gateway fan-out (which resolves binding_id + chat_id from
    a conversation_id) without this module knowing about gateways.

    Implementations may optionally provide :meth:`send_approval_card`
    to render a native approval UI (buttons + summary). Callers
    must go through :func:`deliver_approval_card_or_text` which
    falls back to plain text via :meth:`send_to_conversation` when
    the method is missing.
    """

    async def send_to_conversation(
        self, conversation_id: str, text: str
    ) -> None: ...


@dataclass(frozen=True)
class NotificationContext:
    """Pre-resolved targets for one approval notification fan-out."""

    target_conversation_ids: list[str]
    approval_url: str | None = None


class NotificationTargetResolver:
    """Resolves conversation IDs for a given approval event.

    The resolver is stateless — all lookups go through injected async
    callables so tests can wire deterministic fakes without hitting PG.
    """

    def __init__(
        self,
        *,
        get_conversations_for_user_and_coworker: GetConversationsFn,
        get_conversation: GetConversationFn,
        webui_base_url: str | None = None,
    ) -> None:
        self._get_conversations_for_user_and_coworker = (
            get_conversations_for_user_and_coworker
        )
        self._get_conversation = get_conversation
        self._webui_base_url = (webui_base_url or "").rstrip("/") or None

    async def resolve_for_approvers(
        self,
        *,
        request: ApprovalRequest,
        policy: ApprovalPolicy | None,
    ) -> NotificationContext:
        """Compute notification targets for the approver fan-out.

        Returns conversation IDs in priority order. A single conversation
        is usually enough, but when resolve_for_approvers must notify
        many approvers whose preferred conversations differ, we include
        every unique conversation ID we find.
        """
        candidates: list[str] = []

        # 1. Explicit per-policy override
        if (
            policy
            and policy.notify_conversation_id
            and await self._conv_exists(policy.notify_conversation_id)
        ):
            candidates.append(policy.notify_conversation_id)

        # 2. Each approver's most recent conversation with this coworker
        for approver_id in request.resolved_approvers:
            convs = await self._get_conversations_for_user_and_coworker(
                approver_id, request.coworker_id
            )
            for conv_id in convs:
                if conv_id and conv_id not in candidates:
                    candidates.append(conv_id)

        # 3. Originating conversation — last-ditch fallback so the agent
        # at least tells the user that approval is awaited.
        if (
            request.conversation_id
            and request.conversation_id not in candidates
        ):
            candidates.append(request.conversation_id)

        url = (
            f"{self._webui_base_url}/approvals/{request.id}"
            if self._webui_base_url
            else None
        )
        return NotificationContext(
            target_conversation_ids=candidates, approval_url=url
        )

    async def resolve_for_originating_conversation(
        self, request: ApprovalRequest
    ) -> NotificationContext:
        """Target only the conversation that originated the request.

        Used for user-facing notifications (reject/expire/skip/execute
        report), not for approver notifications.
        """
        targets: list[str] = []
        if request.conversation_id:
            targets.append(request.conversation_id)
        return NotificationContext(target_conversation_ids=targets, approval_url=None)

    async def resolve_for_safety_approvers(
        self,
        *,
        request: ApprovalRequest,
        approver_user_ids: list[str],
    ) -> NotificationContext:
        """Notification targets for a safety-driven approval request.

        Safety requests don't have a policy (policy_id is NULL), so
        the normal policy-override path in ``resolve_for_approvers``
        does not apply. This method walks the approver users' active
        conversations with the target coworker (same "each approver's
        most recent conversation" logic as the approver path), then
        falls back to the originating conversation. The approver set
        typically matches ``_tenant_owner_ids`` upstream, so
        operators receive the notification in whatever
        conversation they last used with this coworker.

        Returns an empty ``target_conversation_ids`` when no
        approver has any recent conversation — the caller logs +
        skips notification but keeps the approval_request so the
        admin UI can still surface it.
        """
        candidates: list[str] = []
        for approver_id in approver_user_ids:
            convs = await self._get_conversations_for_user_and_coworker(
                approver_id, request.coworker_id
            )
            for conv_id in convs:
                if conv_id and conv_id not in candidates:
                    candidates.append(conv_id)
        if (
            request.conversation_id
            and request.conversation_id not in candidates
        ):
            candidates.append(request.conversation_id)
        url = (
            f"{self._webui_base_url}/approvals/{request.id}"
            if self._webui_base_url
            else None
        )
        return NotificationContext(
            target_conversation_ids=candidates, approval_url=url
        )

    async def _conv_exists(self, conversation_id: str) -> bool:
        conv = await self._get_conversation(conversation_id)
        return conv is not None


# ---------------------------------------------------------------------------
# Callable signatures expected by the resolver.
# ---------------------------------------------------------------------------


class GetConversationsFn(Protocol):
    """Returns conversation IDs a user is active in for a coworker.

    Implementations typically consult the `conversations` table sorted
    by last_agent_invocation DESC. Return an empty list when none.
    """

    async def __call__(
        self, user_id: str, coworker_id: str
    ) -> list[str]: ...


class GetConversationFn(Protocol):
    async def __call__(self, conversation_id: str) -> object | None: ...


# ---------------------------------------------------------------------------
# Message shaping helpers
# ---------------------------------------------------------------------------


def format_approver_request_message(
    *,
    request: ApprovalRequest,
    policy: ApprovalPolicy | None,
    approval_url: str | None,
) -> str:
    """Message sent to approvers when a new request lands."""
    short = request.id[:8]
    lines = [
        f"Approval request #{short} is waiting for review.",
        f"  server: {request.mcp_server_name}",
        f"  actions: {len(request.actions)}",
    ]
    if request.rationale:
        lines.append(f"  rationale: {request.rationale}")
    if policy is not None and policy.auto_expire_minutes:
        lines.append(f"  expires in ~{policy.auto_expire_minutes} min")
    if approval_url:
        lines.append(f"  review: {approval_url}")
    return "\n".join(lines)


def format_decision_message(
    *, request: ApprovalRequest, decision: str, note: str | None
) -> str:
    """Message sent to the originating conversation after decide."""
    short = request.id[:8]
    if decision == "approved":
        return f"Approval request #{short} was approved. Executing now."
    suffix = f": {note}" if note else ""
    return f"Approval request #{short} was rejected{suffix}."


def format_skipped_message(request: ApprovalRequest) -> str:
    short = request.id[:8]
    return (
        f"Approval request #{short} could not proceed: no approver is "
        "configured. Please contact an admin to assign an approver."
    )


def format_expired_message(request: ApprovalRequest) -> str:
    short = request.id[:8]
    return f"Approval request #{short} expired before anyone reviewed it."


def format_cancelled_message(request: ApprovalRequest) -> str:
    short = request.id[:8]
    return (
        f"Approval request #{short} was cancelled because the originating "
        "agent turn was stopped."
    )


def format_execution_started(request: ApprovalRequest) -> str:
    """Message sent to the originating conversation right after a
    claim succeeds, before the Worker fires off MCP calls.

    v6.1 §P2.5 — long-running batches were silent between
    "decided" and "executed". For multi-minute MCP calls this read
    as a stalled bot; the started message gives users a deterministic
    "the action is now running" signal so they can stop wondering.
    Cheap to send: best-effort by the caller (catch + log on
    failure), since blocking execution to deliver a status line
    would itself create a new failure mode.
    """
    short = request.id[:8]
    return (
        f"Approval #{short} approved — starting execution now."
    )


def format_execution_stale_message(request: ApprovalRequest) -> str:
    """Notification the maintenance loop emits for a wedged ``executing``
    row. Deliberately terse: v1 does not persist per-action progress,
    so we cannot tell the user which actions completed. What matters
    is that they DO NOT blindly retry — any action in the batch may
    already have taken effect on the MCP side.
    """
    short = request.id[:8]
    return (
        f"Approval request #{short} did not complete cleanly "
        "(execution_stale). Some actions in the batch may have "
        "already taken effect on the downstream MCP server. "
        "Please investigate manually — do NOT blindly re-submit."
    )


async def deliver_approval_card_or_text(
    channel: ChannelSender,
    conversation_id: str,
    card: ApprovalCardPayload,
) -> None:
    """Dispatch an approval card with a graceful text fallback.

    v6.1 §P2.7 — channels can opt in to a structured card by
    implementing ``send_approval_card(conversation_id, card)``.
    Channels that have not implemented it (including the default
    orchestrator sender for now) receive ``text_fallback`` via
    :meth:`ChannelSender.send_to_conversation`, with the Web
    approval URL appended when configured. The helper centralises
    the "buttons or text" decision so callers never need to
    hasattr-check.
    """
    send_card = getattr(channel, "send_approval_card", None)
    if send_card is not None:
        await send_card(conversation_id, card)
        return
    body = card.text_fallback
    if card.approval_url and card.approval_url not in body:
        body = f"{body}\n  review: {card.approval_url}"
    await channel.send_to_conversation(conversation_id, body)


def format_edge_fyi(
    *,
    server: str,
    tool: str,
    coworker_name: str | None = None,
    tenant_name: str | None = None,
) -> str:
    """Owner-FYI text for the v6.1 edge fallback path (§P2.6).

    Sent when a controlled tool call has no resolvable approver — bot-
    chained task, pure system turn, or bootstrap actor. The message
    deliberately does NOT include action buttons: there is no
    ``approval_requests`` row for the owner to decide on; the hook
    already fail-closed the call. Owners are informed for visibility
    only.
    """
    lines = ["⚠️ FYI (controlled action with no responsible user)"]
    if tenant_name:
        lines.append(f"  tenant: {tenant_name}")
    if coworker_name:
        lines.append(f"  coworker: {coworker_name}")
    lines.extend(
        [
            f"  intercepted: {server}/{tool}",
            "  reason: the triggering turn has no identifiable user "
            "(system / chained / bootstrap)",
            "  status: the action was blocked and will not run; see the "
            "Web activity log for context.",
        ]
    )
    return "\n".join(lines)


def format_execution_report(
    *, request: ApprovalRequest, results: list[dict[str, object]], status: str
) -> str:
    short = request.id[:8]
    if status == "executed":
        header = f"Approval #{short} executed:"
    elif status == "execution_failed":
        header = f"Approval #{short} partially executed:"
    else:
        header = f"Approval #{short} finished with status {status}:"
    body_lines: list[str] = []
    for action, res in zip(request.actions, results, strict=False):
        tool = action.get("tool_name", "?") if isinstance(action, dict) else "?"
        if isinstance(res, dict) and res.get("error"):
            body_lines.append(f"  [x] {tool} — {res['error']}")
        else:
            body_lines.append(f"  [ok] {tool}")
    return header + "\n" + "\n".join(body_lines)
