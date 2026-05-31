"""Human-facing delivery for HITL approvals (docs/21-hitl-approval-plan.md §10 S4).

The :class:`ApprovalCoordinator` (S3) drives the state machine but owns no
channel I/O — it calls two hooks the moment an approval becomes pending or goes
terminal:

* ``notify_status(req)`` — the **soft** "⏳ waiting" signal: deliver the
  approve/reject card to whichever channel the request's conversation is bound
  to (Telegram inline keyboard / a web ``approval.requested`` event).
* ``notify_hard(req, kind)`` — the **hard** result: deterministically edit that
  same card to "❌ Rejected" / "⏰ Expired" with no LLM in the loop.

This module is that delivery layer. It is decoupled from ``rolemesh.main``
globals and from NATS/DB by injected callables, so the target-resolution and
card-lifecycle logic is unit-testable against fakes (no broker, no Postgres,
no live Telegram).

Card-location cache: editing a Telegram card needs ``(chat_id, message_id)``
and editing either channel needs to know which channel the card went to, so
``notify_status`` records a :class:`_CardRef` keyed by ``request_id`` and
``notify_hard`` / :meth:`mark_outcome` consume it. The cache is in-memory; an
orchestrator restart loses it, after which a hard edit is best-effort (the
``approval_requests`` row stays authoritative — same restart degradation the
S3 coordinator documents).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from rolemesh.core.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from rolemesh.core.types import ChannelBinding, Conversation
    from rolemesh.db.approval import ApprovalRequest

logger = get_logger()

__all__ = ["ApprovalNotifier", "card_text_for_outcome", "pending_card_text"]


# Terminal-card text. English per repo convention (code-facing strings are
# English; the CN docs translation lives in docs/*-cn.md). ``approved`` is
# included so the decision funnel can close the loop on a winning approve —
# ``notify_hard`` itself only ever fires for reject/expired.
_OUTCOME_TEXT = {
    "approved": "✅ Approved",
    "rejected": "❌ Rejected",
    "expired": "⏰ Expired (no decision in time)",
}


def card_text_for_outcome(outcome: str) -> str:
    """Deterministic terminal-card text for a resolved approval."""
    return _OUTCOME_TEXT.get(outcome, f"Resolved: {outcome}")


def pending_card_text(action_summary: str | None) -> str:
    """Body text for the pending approve/reject card."""
    summary = action_summary or "an MCP tool call"
    return f"⏳ Approval required for {summary}.\nApprove or reject this tool call."


@dataclass
class _CardRef:
    """Where a pending approval's card landed, so it can be edited on resolve."""

    request_id: str
    channel_type: str
    binding_id: str
    chat_id: str
    conversation_id: str | None
    tenant_id: str
    telegram_message_id: int | None = None


@dataclass(frozen=True)
class _Target:
    channel_type: str
    binding_id: str
    chat_id: str
    conversation_id: str
    tenant_id: str


def _conv_recency_key(conv: Conversation) -> str:
    """Sort key for the scheduled-task fallback (most recent first).

    ``last_agent_invocation`` is the truest "recently active" signal; fall back
    to ``created_at`` for a conversation the agent never replied in. Both are
    ISO-8601 strings so a lexical compare is chronological.
    """
    return conv.last_agent_invocation or conv.created_at or ""


class ApprovalNotifier:
    """Deliver + resolve approval cards across Telegram and the web UI."""

    def __init__(
        self,
        *,
        get_conversation: Callable[[str], Awaitable[Conversation | None]],
        get_binding: Callable[[str], Awaitable[ChannelBinding | None]],
        list_conversations_for_coworker: Callable[
            [str, str], Awaitable[list[Conversation]]
        ],
        send_telegram_card: Callable[[str, str, str, str], Awaitable[int | None]],
        edit_telegram_card: Callable[[str, str, int, str], Awaitable[None]],
        publish_web_event: Callable[[str, str, dict[str, Any]], Awaitable[None]],
    ) -> None:
        # DB / channel resolvers (admin-scoped: the orchestrator has no tenant
        # context and the request row already carries the authoritative ids).
        self._get_conversation = get_conversation
        self._get_binding = get_binding
        self._list_conversations_for_coworker = list_conversations_for_coworker
        # Outermost channel boundaries (the only things tests stub).
        self._send_telegram_card = send_telegram_card
        self._edit_telegram_card = edit_telegram_card
        self._publish_web_event = publish_web_event
        self._cards: dict[str, _CardRef] = {}

    # -- coordinator hooks ------------------------------------------------

    async def notify_status(self, req: ApprovalRequest) -> None:
        """Deliver the approve/reject card for a freshly-pending request."""
        target = await self._resolve_target(req)
        if target is None:
            logger.warning(
                "approval notify: no deliverable target; card not sent",
                request_id=req.id,
                conversation_id=req.conversation_id,
                coworker_id=req.coworker_id,
            )
            return
        ref = _CardRef(
            request_id=req.id,
            channel_type=target.channel_type,
            binding_id=target.binding_id,
            chat_id=target.chat_id,
            conversation_id=target.conversation_id,
            tenant_id=target.tenant_id,
        )
        self._cards[req.id] = ref
        if target.channel_type == "telegram":
            try:
                message_id = await self._send_telegram_card(
                    target.binding_id,
                    target.chat_id,
                    req.id,
                    req.action_summary or "an MCP tool call",
                )
            except Exception:
                logger.exception("approval notify: telegram card send failed",
                                  request_id=req.id)
                message_id = None
            ref.telegram_message_id = message_id
        elif target.channel_type == "web":
            await self._safe_publish_web(
                target.binding_id,
                target.chat_id,
                {
                    "type": "approval.requested",
                    "request_id": req.id,
                    "action_summary": req.action_summary,
                    "expires_at": req.expires_at.isoformat(),
                },
            )
        else:
            # Slack et al. have no card affordance yet; the soft block reason
            # still reaches the agent, the user just gets no interactive card.
            logger.info(
                "approval notify: channel has no card surface; soft path only",
                request_id=req.id,
                channel_type=target.channel_type,
            )

    async def notify_hard(self, req: ApprovalRequest, kind: str) -> None:
        """Hard channel: edit the card to its terminal state (reject/expired)."""
        await self.mark_outcome(req.id, kind)

    # -- decision-funnel entrypoint (approve closes the loop here) --------

    async def mark_outcome(self, request_id: str, outcome: str) -> None:
        """Edit the card to ``approved`` / ``rejected`` / ``expired``.

        Terminal, so the cache entry is popped — a second resolve (e.g. an
        expiry that races a decision) finds nothing and no-ops cleanly.
        """
        ref = self._cards.pop(request_id, None)
        if ref is None:
            # No live card (delivery failed, or lost across a restart). The
            # row status is the source of truth; the visual edit is skipped.
            return
        text = card_text_for_outcome(outcome)
        if ref.channel_type == "telegram":
            if ref.telegram_message_id is not None:
                try:
                    await self._edit_telegram_card(
                        ref.binding_id, ref.chat_id, ref.telegram_message_id, text
                    )
                except Exception:
                    logger.exception(
                        "approval notify: telegram card edit failed",
                        request_id=request_id,
                    )
        elif ref.channel_type == "web":
            await self._safe_publish_web(
                ref.binding_id,
                ref.chat_id,
                {
                    "type": "approval.resolved",
                    "request_id": request_id,
                    "outcome": outcome,
                },
            )

    # -- internals --------------------------------------------------------

    async def _safe_publish_web(
        self, binding_id: str, chat_id: str, payload: dict[str, Any]
    ) -> None:
        try:
            await self._publish_web_event(binding_id, chat_id, payload)
        except Exception:
            logger.exception(
                "approval notify: web event publish failed",
                request_id=payload.get("request_id"),
            )

    async def _resolve_target(self, req: ApprovalRequest) -> _Target | None:
        """conversation → channel_bindings → channel_chat_id (§10 S4).

        A request bound to a conversation resolves directly. A scheduled-task
        request (``conversation_id is None``) has no active conversation, so it
        falls back to the coworker's most recently active conversation.
        """
        conv: Conversation | None = None
        if req.conversation_id:
            conv = await self._get_conversation(req.conversation_id)
        else:
            convs = await self._list_conversations_for_coworker(
                req.coworker_id, req.tenant_id
            )
            if convs:
                conv = max(convs, key=_conv_recency_key)
        if conv is None:
            return None
        binding = await self._get_binding(conv.channel_binding_id)
        if binding is None:
            logger.warning(
                "approval notify: conversation has no binding row",
                request_id=req.id,
                binding_id=conv.channel_binding_id,
            )
            return None
        return _Target(
            channel_type=binding.channel_type,
            binding_id=conv.channel_binding_id,
            chat_id=conv.channel_chat_id,
            conversation_id=conv.id,
            tenant_id=req.tenant_id,
        )

    # -- IDOR helper for the decision funnel ------------------------------

    def card_ref(self, request_id: str) -> _CardRef | None:
        """The recorded card location for a pending request, if still live.

        The Telegram ``CallbackQueryHandler`` carries only ``request_id`` in
        ``callback_data`` (the 64-byte limit, §6); it resolves the authoritative
        ``(tenant_id, conversation_id, chat_id)`` it must authorise against from
        this cache rather than trusting anything client-supplied.
        """
        return self._cards.get(request_id)
