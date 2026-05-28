"""Cross-channel admission helpers (v6.1 §P1.5 / §P1.6).

The Phase-1 admission model treats every inbound 1:1 IM message as
needing a linked identity. ``admit_telegram_1on1`` is the one-line
gate the orchestrator runs against a sender's normalised channel_id:

- Hit  → returns the RoleMesh user_id; caller continues processing
         (and, in Checkpoint 4, lazy-fills ``conv.user_id``).
- Miss → the helper sends a guidance reply via the gateway and
         returns ``None``; the caller must drop the message before
         persisting it or enqueueing agent work.

Keeping the gate in its own module instead of inlining into main.py
lets the unit tests exercise it against a stub gateway without
booting the orchestrator process.

Strings here are the single source of truth for admission wire text;
``telegram_gateway._handle_start_command`` already imports its own
constants but the *content* of "please link in Web" is the same
sentence — copy edits land in one diff.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rolemesh.core.logger import get_logger
from rolemesh.db import resolve_user_from_channel_sender

if TYPE_CHECKING:
    from rolemesh.channels.gateway import ChannelGateway

logger = get_logger()


# Unified guidance text. Used when an unlinked Telegram sender either
# DMs the bot directly or sends ``/start`` with no token. The
# telegram_gateway side reuses this same constant in both spots so
# UX copy is identical across entry points (design §P1.5 "引导文本
# 统一一处").
ADMISSION_GUIDE_TEXT = (
    "I cannot start a chat with you because your Telegram account is "
    "not linked to a RoleMesh user. Open RoleMesh Web → Settings → "
    "Connected channels to link your account, then send /start with "
    "the token shown there."
)


GROUP_NOT_SUPPORTED_TEXT = (
    "This bot only supports 1:1 private chats. Group conversations "
    "are not supported."
)


async def admit_telegram_1on1(
    tenant_id: str,
    sender_channel_id: str,
    *,
    gateway: ChannelGateway,
    binding_id: str,
    chat_id: str,
) -> str | None:
    """Resolve a Telegram sender to a RoleMesh user_id; deny otherwise.

    ``sender_channel_id`` MUST be the normalised form
    (``str(update.effective_user.id)``); the lookup is keyed against
    a TEXT column so the int/str distinction would silently miss.

    On a miss this sends ``ADMISSION_GUIDE_TEXT`` via the gateway and
    emits an INFO-level structured log line so an operator watching
    onboarding traffic can spot a Telegram user who's hit the bot
    without ever completing the link. Returns ``None`` so the caller
    can short-circuit before storing the message.

    The send is best-effort — a transient gateway send failure logs
    and still denies; admission posture is fail-close regardless.
    """
    user_id = await resolve_user_from_channel_sender(
        tenant_id, "telegram", sender_channel_id
    )
    if user_id is not None:
        return user_id
    try:
        await gateway.send_message(binding_id, chat_id, ADMISSION_GUIDE_TEXT)
    except Exception:  # noqa: BLE001
        logger.exception(
            "admission_guidance_send_failed",
            binding_id=binding_id,
            chat_id=chat_id,
        )
    logger.info(
        "im_admission_denied",
        platform="telegram",
        tenant_id=tenant_id,
        sender=sender_channel_id,
    )
    return None
