"""Human-facing delivery for HITL approvals (docs/12-hitl-approval-architecture.md §10 S4).

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

import json
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
    "cancelled": "⏰ Cancelled (the coworker withdrew this call)",
}


def card_text_for_outcome(outcome: str) -> str:
    """Deterministic terminal-card text for a resolved approval."""
    return _OUTCOME_TEXT.get(outcome, f"Resolved: {outcome}")


# Card-body sizing. Telegram caps a message at 4096 chars; we aim far lower so
# the card stays scannable on a phone. Each param value is clipped, and the
# whole params block is dropped-with-ellipsis once the running body would blow
# its budget — so even a tool called with hundreds of huge params can't grow
# the card past ``_MAX_CARD_LEN`` (+ the small fixed header/footer).
_MAX_VALUE_LEN = 80
_MAX_RATIONALE_LEN = 240
_MAX_SUMMARY_LEN = 160
_MAX_CARD_LEN = 1500
_ELLIPSIS = "…"


def _clip(text: str, limit: int) -> str:
    """Cut ``text`` to ``limit`` chars, marking the cut with an ellipsis."""
    if len(text) <= limit:
        return text
    if limit <= len(_ELLIPSIS):
        return _ELLIPSIS[:limit]
    return text[: limit - len(_ELLIPSIS)] + _ELLIPSIS


def _render_value(value: Any) -> str:
    """One-line, length-capped rendering of a single param value.

    Strings pass through; everything else is compact-JSON-encoded (falling back
    to ``str`` for non-serialisable values). Newlines are collapsed so one param
    occupies exactly one line, then the result is clipped to ``_MAX_VALUE_LEN``.
    """
    if isinstance(value, str):
        rendered = value
    else:
        try:
            rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            rendered = str(value)
    return _clip(" ".join(rendered.split()), _MAX_VALUE_LEN)


def pending_card_text(req: ApprovalRequest) -> str:
    """Plain-text body for the pending approve/reject card (Telegram).

    Mirrors the web chat card (spec §6): a ``server.tool`` chip, one
    ``key: value`` line per param (each value clipped), an optional ``Why:``
    rationale line (omitted when null/blank), and the approve/reject prompt.

    Plain text — deliberately **not** MarkdownV2 — so user-supplied param values
    never have to be escaped and can't trip Telegram's formatter. The body is
    budgeted well under Telegram's 4096-char limit; params that would overflow
    are dropped and the truncation is flagged with an ellipsis line.
    """
    summary = _clip(req.action_summary or "an MCP tool call", _MAX_SUMMARY_LEN)
    action = req.action if isinstance(req.action, dict) else {}
    tool_name = action.get("tool_name")
    params = action.get("params")

    header = f"⏳ Approval required for {summary}."
    chip = f"{req.mcp_server_name}.{tool_name}" if tool_name else (req.mcp_server_name or "")
    footer = "Approve or reject this tool call."

    rationale = (req.rationale or "").strip()
    rationale_line = (
        f"Why: {_clip(rationale, _MAX_RATIONALE_LEN)}" if rationale else None
    )

    # Everything except the params block is fixed-size and always rendered; the
    # params block gets whatever budget is left under the cap.
    fixed_len = len(header) + len(chip) + len(footer)
    if rationale_line:
        fixed_len += len(rationale_line)

    param_lines: list[str] = []
    if isinstance(params, dict) and params:
        budget = _MAX_CARD_LEN - fixed_len
        truncated = False
        for key, value in params.items():
            line = f"{key}: {_render_value(value)}"
            if budget - (len(line) + 1) < 0:
                truncated = True
                break
            param_lines.append(line)
            budget -= len(line) + 1
        if truncated or len(param_lines) < len(params):
            param_lines.append(_ELLIPSIS)

    # Blocks are separated by a blank line; chip + params form one tight block.
    detail = [chip, *param_lines] if chip else param_lines
    blocks = [header]
    if detail:
        blocks.append("\n".join(detail))
    if rationale_line:
        blocks.append(rationale_line)
    blocks.append(footer)
    return "\n\n".join(blocks)


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
                    pending_card_text(req),
                )
            except Exception:
                logger.exception("approval notify: telegram card send failed",
                                  request_id=req.id)
                message_id = None
            ref.telegram_message_id = message_id
        elif target.channel_type == "web":
            # The decision-relevant payload (§1.1): the SPA renders an informative
            # card from the WS push alone, before any REST read. ``action`` is the
            # {tool_name, params} snapshot persisted on the row.
            action = req.action if isinstance(req.action, dict) else {}
            await self._safe_publish_web(
                target.binding_id,
                target.chat_id,
                {
                    "type": "approval.requested",
                    "request_id": req.id,
                    "mcp_server_name": req.mcp_server_name,
                    "tool_name": action.get("tool_name"),
                    "params": action.get("params"),
                    "coworker_id": req.coworker_id,
                    "conversation_id": req.conversation_id,
                    "requested_at": req.requested_at.isoformat(),
                    "rationale": req.rationale,
                    "action_summary": req.action_summary,
                    # Safety-rule provenance (§3.10); None for a business-policy
                    # approval. The SPA renders the amber "paused by a safety
                    # rule" banner from this.
                    "triggered_by": req.triggered_by,
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
        # Frontdesk v1.2 parent-walk: an approval submitted by a specialist
        # running in a delegation child conversation is attributed to that
        # child, whose binding is the 'internal' channel — no card surface and
        # no WS listener. Walk up to the parent user-facing conversation so the
        # card reaches the channel the user is actually watching; the cached
        # _CardRef then edits land there too on resolve. Top-level convs
        # (parent_conversation_id is None) are unaffected.
        if conv.parent_conversation_id:
            parent = await self._get_conversation(conv.parent_conversation_id)
            if parent is not None:
                conv = parent
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
