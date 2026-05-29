"""Telegram gateway — manages multiple Telegram bots (one per unique token).

When multiple coworkers share the same bot token, only one polling connection
is created. Incoming messages are dispatched to ALL bindings that share the
token, so each coworker's conversation lookup can match independently.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

import asyncpg
import telegram.error
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from rolemesh.channels.admission import (
    GROUP_NOT_SUPPORTED_TEXT,
    LINK_ALREADY_BOUND_TEXT,
    LINK_MISSING_TOKEN_TEXT,
    LINK_REJECTED_TEXT,
    LINK_SUCCESS_PREFIX,
)
from rolemesh.core.logger import get_logger
from rolemesh.db import (
    consume_link_token,
    create_channel_identity,
    get_channel_binding_for_bot_token,
    resolve_user_from_channel_sender,
    update_channel_binding_bot_username,
)

if TYPE_CHECKING:
    from telegram import Bot, Update
    from telegram.ext import ContextTypes

    from rolemesh.approval.notification import ApprovalCardPayload
    from rolemesh.channels.gateway import MessageCallback
    from rolemesh.core.types import ChannelBinding

logger = get_logger()

_MAX_LENGTH = 4096
# v6.1 §P1.5: 1:1 only. Includes 'channel' (broadcast) defensively —
# telegram-bot-api rarely delivers normal messages from a channel but
# the type set is what the design specifies.
_GROUP_CHAT_TYPES = ("group", "supergroup", "channel")

# v6.1 §P2b.1 — InlineKeyboardButton callback_data prefixes for the
# approval card. Each callback_data is ``"apr:<uuid>"`` or
# ``"rej:<uuid>"`` and is therefore 40 bytes — well under Telegram's
# 64-byte limit. Keep the prefixes 4-byte-fixed so the inbound
# handler can slice ``data[4:]`` without re-parsing.
_APPROVE_CALLBACK_PREFIX = "apr:"
_REJECT_CALLBACK_PREFIX = "rej:"
_APPROVE_BUTTON_LABEL = "✅ Approve"
_REJECT_BUTTON_LABEL = "❌ Reject"


async def _short_circuit_group(update: Update) -> bool:
    """If ``update`` is from a group/supergroup/channel, reply with
    the not-supported guidance and return True so the caller drops
    the message without dispatching to ``on_message``.

    Module-level so the unit tests can drive it with a stub Update
    instead of spinning up a real Application.
    """
    chat = update.effective_chat
    if chat is None:
        return False
    if chat.type not in _GROUP_CHAT_TYPES:
        return False
    try:
        await chat.send_message(GROUP_NOT_SUPPORTED_TEXT)
    except Exception:
        # Sending the guidance reply is best-effort — the short-
        # circuit must still drop the message even if Telegram is
        # transiently flaky.
        logger.exception(
            "telegram_group_guidance_send_failed",
            chat_id=getattr(chat, "id", None),
        )
    return True


# v6.1 §P1.4 link guidance — wire strings live in
# ``rolemesh.channels.admission`` so admission + link flows share a
# single source of truth (see F2 / "引导文本统一一处").


async def _handle_start_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """``/start [<token>]`` — Telegram side of the WebUI link flow.

    Registered as a ``CommandHandler`` at the gateway so unlinked
    senders can complete the binding *before* admission (which would
    otherwise deny them). The token carries its own (user_id,
    tenant_id), so no per-bot tenant lookup is needed here.

    Module-level (not a closure) so the link logic is testable
    without spinning up a live Telegram ``Application``.
    """
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or user is None:
        return
    args = context.args or []
    if not args:
        await chat.send_message(LINK_MISSING_TOKEN_TEXT)
        return
    token = args[0]
    channel_id = str(user.id)
    consumed = await consume_link_token(token)
    if consumed is None:
        # Atomic UPDATE returned no row → token was already used,
        # expired, or unknown. We deliberately collapse the three so
        # a leaked-and-replayed token cannot leak its prior owner via
        # a distinguishing error message.
        await chat.send_message(LINK_REJECTED_TEXT)
        logger.info("telegram_link_token_rejected", channel_id=channel_id)
        return
    user_id, tenant_id, _ = consumed
    try:
        await create_channel_identity(
            tenant_id, "telegram", channel_id, user_id
        )
    except asyncpg.UniqueViolationError:
        # Token is already marked used by the atomic UPDATE above —
        # the correct outcome, since the user must unbind in Web and
        # restart the flow with a fresh token.
        await chat.send_message(LINK_ALREADY_BOUND_TEXT)
        logger.info(
            "telegram_link_unique_violation",
            channel_id=channel_id,
            tenant_id=tenant_id,
        )
        return
    display = user.first_name or user.username or channel_id
    await chat.send_message(f"✅ {LINK_SUCCESS_PREFIX} ({display}).")
    logger.info(
        "telegram_link_bound",
        tenant_id=tenant_id,
        user_id=user_id,
        channel_id=channel_id,
    )


async def _send_telegram_message(bot: Bot, chat_id: str | int, text: str) -> None:
    """Send with Markdown, falling back to plain text."""
    try:
        await bot.send_message(chat_id, text, parse_mode=ParseMode.MARKDOWN)
    except Exception:  # noqa: BLE001
        await bot.send_message(chat_id, text)


# ---------------------------------------------------------------------------
# v6.1 §P2b.1 — CallbackQuery → engine.handle_decision plumbing
# ---------------------------------------------------------------------------

# Approver-facing edit text. Kept short because Telegram replaces the
# original card body, so the user sees the new text where the buttons
# used to be — we want it to read as the outcome at a glance.
_DECISION_APPROVED_TEXT = "✅ Approved"
_DECISION_REJECTED_TEXT = "❌ Rejected"
_DECISION_ALREADY_TEXT = "This approval has already been decided."
_DECISION_NOT_LINKED_TEXT = (
    "⚠️ Your Telegram account is not linked to RoleMesh. "
    "Open the Web app → Settings → Connected channels to link, "
    "then retry."
)
_DECISION_FORBIDDEN_TEXT = (
    "⚠️ You are not authorised to decide this approval."
)
_DECISION_UNKNOWN_TENANT_TEXT = (
    "⚠️ This bot is not configured for any tenant — please reach out "
    "to your administrator."
)
_DECISION_NO_ENGINE_TEXT = (
    "⚠️ The approval engine is not available right now. Please retry "
    "from the Web inbox."
)
_DECISION_FAILED_TEXT = (
    "⚠️ Could not record this decision. Please retry from the Web inbox."
)


class ApprovalDecisionRouter(Protocol):
    """Subset of :class:`rolemesh.approval.engine.ApprovalEngine` that the
    Telegram CallbackQuery path actually needs.

    Kept narrow so the gateway does not pin the entire engine surface;
    a future "routes by tenant to a sharded engine" wrapper can satisfy
    the same Protocol without leaking gateway concerns into the engine.
    """

    async def handle_decision(
        self,
        *,
        request_id: str,
        tenant_id: str,
        outcome: str,
        user_id: str,
        note: str | None = None,
    ) -> Any: ...


# Module-level registry for the engine. The Telegram gateway is
# constructed during main.py startup BEFORE the approval engine is
# instantiated (the engine depends on the channel sender adapter that
# in turn fans out via the gateway). The CallbackQueryHandler is
# registered when ``_BotInstance.start()`` runs, also before the
# engine exists. We therefore resolve the engine lazily inside the
# handler via this registry, which the main bootstrap sets via
# :func:`set_approval_decision_router` once construction completes.
#
# Holding the engine on a module-level slot rather than a class attr
# keeps the gateway free of an "engine" field that almost every
# constructor call doesn't need, and makes the lookup uniform across
# multiple ``_BotInstance`` objects (one per Telegram token).
_decision_router: ApprovalDecisionRouter | None = None


def set_approval_decision_router(
    router: ApprovalDecisionRouter | None,
) -> None:
    """Bind the approval engine to the Telegram callback path.

    Called from :mod:`rolemesh.main` after the :class:`ApprovalEngine`
    is constructed. Passing ``None`` clears the binding — used by tests
    that need to assert the no-engine fallback message.
    """
    global _decision_router
    _decision_router = router


def _parse_callback_data(data: str) -> tuple[str, str] | None:
    """Parse ``apr:<request_id>`` / ``rej:<request_id>``.

    Returns ``(outcome, request_id)`` where outcome is one of
    ``"approved"`` / ``"rejected"`` (the engine outcome string —
    :func:`ApprovalEngine.handle_decision` validates exactly these).
    Any other payload returns ``None`` so the handler ignores it
    silently (Telegram delivers callback queries for any button the
    bot has ever sent in any chat; we must not error on unknown
    payloads or we'd spam the audit log).
    """
    if data.startswith(_APPROVE_CALLBACK_PREFIX):
        return ("approved", data[len(_APPROVE_CALLBACK_PREFIX):])
    if data.startswith(_REJECT_CALLBACK_PREFIX):
        return ("rejected", data[len(_REJECT_CALLBACK_PREFIX):])
    return None


@dataclass(frozen=True)
class _DecisionDispatchResult:
    """Result of routing one CallbackQuery through the engine.

    Carries the wire text the handler will edit back into the original
    card (or send as a new message if the edit fails) plus a ``kind``
    label that tests assert on without coupling to the user-facing
    string.
    """

    kind: str
    edit_text: str


async def dispatch_telegram_callback_decision(
    *,
    bot_token: str,
    sender_id: str,
    callback_data: str,
) -> _DecisionDispatchResult:
    """Route a Telegram CallbackQuery to the approval engine.

    All policy decisions for the callback path live here so the
    PTB-bound handler stays a thin adapter and the cross-tenant
    invariant (S5: tenant must come from the bot, not the sender) is
    testable without a live Telegram Application.

    Tenant resolution order (DO NOT reorder):

    1. ``bot_token`` → ``channel_bindings`` → ``tenant_id``. This is
       the bot's own credential; it cannot be spoofed by the
       attacker. If multiple coworkers share the token, any of their
       bindings yields the same tenant.
    2. ``(tenant_id, "telegram", sender_id)`` → ``user_id``. The
       Phase 1 reverse lookup, **scoped to the tenant we just
       resolved**. A sender_id linked in a different tenant must not
       be accepted here — that would let a user in tenant A decide
       approvals in tenant B simply by linking the same Telegram
       account to both.

    The function never raises; the handler edits the returned text
    and we leave engine state untouched on any failure.
    """
    parsed = _parse_callback_data(callback_data)
    if parsed is None:
        # Unknown payload — leave the card visible. The handler
        # already acknowledged the click, so the user sees no further
        # message; this is the right behaviour for stray clicks the
        # gateway cannot interpret.
        return _DecisionDispatchResult(kind="ignored", edit_text="")
    outcome, request_id = parsed

    binding = await get_channel_binding_for_bot_token(bot_token)
    if binding is None:
        logger.warning(
            "telegram_callback_unknown_bot_token",
            request_id=request_id,
        )
        return _DecisionDispatchResult(
            kind="no_tenant", edit_text=_DECISION_UNKNOWN_TENANT_TEXT
        )
    tenant_id = binding.tenant_id

    user_id = await resolve_user_from_channel_sender(
        tenant_id, "telegram", sender_id
    )
    if not user_id:
        logger.info(
            "telegram_callback_unlinked_sender",
            tenant_id=tenant_id,
            sender_id=sender_id,
        )
        return _DecisionDispatchResult(
            kind="not_linked", edit_text=_DECISION_NOT_LINKED_TEXT
        )

    router = _decision_router
    if router is None:
        # Race window: orchestrator started polling before the engine
        # was wired. Falls through to "retry on Web" so the user has
        # an out, and we log so an oncall can see what's missing.
        logger.error(
            "telegram_callback_engine_unwired",
            tenant_id=tenant_id,
            request_id=request_id,
        )
        return _DecisionDispatchResult(
            kind="no_engine", edit_text=_DECISION_NO_ENGINE_TEXT
        )

    try:
        await router.handle_decision(
            request_id=request_id,
            tenant_id=tenant_id,
            outcome=outcome,
            user_id=user_id,
        )
    except Exception as exc:  # noqa: BLE001 — engine errors surface as wire text
        # The engine maps "already decided" / "not approver" to typed
        # exceptions; we identify them by class name so the gateway
        # does not need to import ConflictError / ForbiddenError
        # (which would create a module-import cycle gateway →
        # approval.engine → notification → gateway).
        name = type(exc).__name__
        if name == "ConflictError":
            return _DecisionDispatchResult(
                kind="conflict", edit_text=_DECISION_ALREADY_TEXT
            )
        if name == "ForbiddenError":
            return _DecisionDispatchResult(
                kind="forbidden", edit_text=_DECISION_FORBIDDEN_TEXT
            )
        logger.warning(
            "telegram_callback_decide_failed",
            request_id=request_id,
            tenant_id=tenant_id,
            error=str(exc),
            exc_type=name,
        )
        return _DecisionDispatchResult(
            kind="failed", edit_text=_DECISION_FAILED_TEXT
        )

    return _DecisionDispatchResult(
        kind=outcome,
        edit_text=_DECISION_APPROVED_TEXT
        if outcome == "approved"
        else _DECISION_REJECTED_TEXT,
    )


async def _handle_approval_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """PTB :class:`CallbackQueryHandler` for the approval card buttons.

    Stays a thin adapter over :func:`dispatch_telegram_callback_decision`
    so the decision-routing logic is testable without a live PTB
    Application:

    1. Ack the query within Telegram's 10s window — non-ack means the
       button keeps spinning forever on the user's client.
    2. Edit the card to show the outcome. On
       :class:`telegram.error.BadRequest` (old message / permissions
       changed / identical text) we fall back to a fresh message so
       the user still hears the result.
    """
    query = update.callback_query
    if query is None:
        return
    # Always ack so the spinner stops, regardless of whether we end up
    # editing or sending fresh. Telegram silently times out the
    # callback if we wait past ~10s, which manifests as a stuck button
    # on the client — even ignoring unknown payloads must ack.
    with contextlib.suppress(Exception):
        await query.answer()

    data = query.data or ""
    sender_id = (
        str(query.from_user.id) if query.from_user is not None else ""
    )

    bot_token = context.bot.token if context.bot is not None else ""
    result = await dispatch_telegram_callback_decision(
        bot_token=bot_token,
        sender_id=sender_id,
        callback_data=data,
    )
    if result.kind == "ignored" or not result.edit_text:
        # Unknown callback payload — already ack'd; nothing more to do.
        return

    message = query.message
    edited = False
    if message is not None:
        try:
            await query.edit_message_text(result.edit_text)
            edited = True
        except telegram.error.BadRequest as exc:
            logger.info(
                "telegram_callback_edit_fallback",
                reason=str(exc),
                kind=result.kind,
            )
    if not edited and message is not None:
        # ``message.chat`` is populated for both ``Message`` and the
        # ``InaccessibleMessage`` variant of ``MaybeInaccessibleMessage``
        # (only ``Message.chat_id`` is type-narrowed, so reaching for
        # ``.chat.id`` instead keeps the narrowing-tolerant fallback in
        # both branches).
        try:
            await context.bot.send_message(
                chat_id=message.chat.id, text=result.edit_text
            )
        except Exception:
            logger.exception(
                "telegram_callback_fallback_send_failed",
                kind=result.kind,
            )


class _BotInstance:
    """A single Telegram bot instance for one unique token.

    May serve multiple bindings (coworkers) that share the same token.
    """

    def __init__(self, token: str, on_message: MessageCallback) -> None:
        self._token = token
        self._on_message = on_message
        self._app: Application | None = None  # type: ignore[type-arg]
        self._bot_username: str | None = None
        # All binding IDs served by this bot instance
        self._binding_ids: list[str] = []
        # binding_id -> display name (coworker name) for @mention translation
        self._display_names: dict[str, str] = {}

    def add_binding_id(self, binding_id: str, display_name: str | None = None) -> None:
        if binding_id not in self._binding_ids:
            self._binding_ids.append(binding_id)
        if display_name:
            self._display_names[binding_id] = display_name

    def remove_binding_id(self, binding_id: str) -> None:
        if binding_id in self._binding_ids:
            self._binding_ids.remove(binding_id)

    @property
    def has_bindings(self) -> bool:
        return len(self._binding_ids) > 0

    async def start(self) -> None:
        """Initialize and start polling."""
        self._app = Application.builder().token(self._token).build()
        app = self._app

        async def _on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            msg = update.effective_message
            chat = update.effective_chat
            user = update.effective_user
            if msg is None or chat is None or msg.text is None:
                return
            # v6.1 §P1.5: 1:1 only. Replies the guidance and drops
            # before any binding sees the message. Group support is
            # paused (not removed) so the requires_trigger machinery
            # in main.py stays available for a future opt-in revival.
            if await _short_circuit_group(update):
                return
            if msg.text.startswith("/"):
                cmd = msg.text.lstrip("/").split()[0].split("@")[0].lower()
                if cmd in ("chatid", "ping"):
                    return

            chat_id = str(chat.id)
            content = msg.text
            timestamp = msg.date.isoformat() if msg.date else ""
            sender_name = user.first_name if user else "Unknown"
            sender = str(user.id) if user else ""
            msg_id = str(msg.message_id)
            is_group = chat.type in ("group", "supergroup")

            # Translate @bot_username mentions to @display_name for trigger matching
            has_bot_mention = False
            if self._bot_username and msg.entities:
                for entity in msg.entities:
                    if entity.type == "mention":
                        mention_text = content[entity.offset : entity.offset + entity.length].lower()
                        if mention_text == f"@{self._bot_username.lower()}":
                            has_bot_mention = True
                            break

            # Dispatch to ALL bindings sharing this token
            for bid in self._binding_ids:
                # Per-binding content: translate @bot_username to @coworker_name
                bid_content = content
                if has_bot_mention:
                    display_name = self._display_names.get(bid, self._bot_username or "")
                    bid_content = f"@{display_name} {content}"
                await self._on_message(bid, chat_id, sender, sender_name, bid_content, timestamp, msg_id, is_group)

        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_text))

        # Media handlers
        for filt, ph in [
            (filters.PHOTO, "[Photo]"),
            (filters.VIDEO, "[Video]"),
            (filters.VOICE, "[Voice message]"),
            (filters.AUDIO, "[Audio]"),
            (filters.Document.ALL, "[Document]"),
            (filters.Sticker.ALL, "[Sticker]"),
            (filters.LOCATION, "[Location]"),
            (filters.CONTACT, "[Contact]"),
        ]:

            async def _media_handler(
                update: Update,
                context: ContextTypes.DEFAULT_TYPE,
                _ph: str = ph,
            ) -> None:
                msg = update.effective_message
                chat = update.effective_chat
                user = update.effective_user
                if msg is None or chat is None:
                    return
                if await _short_circuit_group(update):
                    return
                chat_id = str(chat.id)
                timestamp = msg.date.isoformat() if msg.date else ""
                sender_name = user.first_name if user else "Unknown"
                sender = str(user.id) if user else ""
                caption = f" {msg.caption}" if msg.caption else ""
                is_group = chat.type in ("group", "supergroup")
                for bid in self._binding_ids:
                    await self._on_message(
                        bid,
                        chat_id,
                        sender,
                        sender_name,
                        f"{_ph}{caption}",
                        timestamp,
                        str(msg.message_id),
                        is_group,
                    )

            app.add_handler(MessageHandler(filt, _media_handler))

        # Commands
        # v6.1 §P1.4: ``/start <token>`` MUST be registered before the
        # generic TEXT MessageHandler so an unlinked sender can
        # complete the link without being admission-denied. python-
        # telegram-bot dispatches CommandHandler before MessageHandler
        # within the same group; registration order here keeps the
        # intent explicit even if PTB internals change.
        app.add_handler(CommandHandler("start", _handle_start_command))

        async def _cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            if update.effective_chat is None:
                return
            chat = update.effective_chat
            chat_name = chat.title or (update.effective_user.first_name if update.effective_user else "Private")
            await chat.send_message(
                f"Chat ID: `{chat.id}`\nName: {chat_name}\nType: {chat.type}",
                parse_mode=ParseMode.MARKDOWN,
            )

        async def _cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            if update.effective_chat:
                name = self._bot_username or "Bot"
                await update.effective_chat.send_message(f"{name} is online.")

        app.add_handler(CommandHandler("chatid", _cmd_chatid))
        app.add_handler(CommandHandler("ping", _cmd_ping))

        # v6.1 §P2b.1: approval button decisions arrive via Telegram
        # CallbackQuery. The handler is module-level (not a closure)
        # so it can be exercised without a live Application, and it
        # resolves the approval engine lazily through
        # ``_decision_router`` because the engine is constructed
        # *after* the gateway during orchestrator startup.
        app.add_handler(CallbackQueryHandler(_handle_approval_callback))

        async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
            logger.error("Telegram bot error", error=str(context.error))

        app.add_error_handler(_on_error)

        await app.initialize()
        me = await app.bot.get_me()
        self._bot_username = me.username
        # v6.1 §P1.4: persist the @handle so the WebUI can synthesise
        # ``t.me/<bot>?start=<token>`` deep-links without an extra hop
        # to Telegram. Every binding currently served by this bot
        # token shares the same @username (multiple coworkers can pool
        # one bot token). Best-effort — gateway start must not fail if
        # the DB is briefly unavailable; the value re-syncs on the
        # next reconnect.
        if me.username:
            for bid in self._binding_ids:
                try:
                    await update_channel_binding_bot_username(bid, me.username)
                except Exception:
                    logger.exception(
                        "Failed to persist bot_username", binding_id=bid
                    )
        logger.info(
            "Telegram bot connected",
            username=me.username,
            bot_id=me.id,
            binding_count=len(self._binding_ids),
        )

        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)  # type: ignore[union-attr]

    async def stop(self) -> None:
        """Stop the bot."""
        if self._app:
            await self._app.updater.stop()  # type: ignore[union-attr]
            await self._app.stop()
            await self._app.shutdown()
            self._app = None

    async def send_message(self, chat_id: str, text: str) -> None:
        """Send a message via this bot."""
        if self._app is None:
            return
        try:
            if len(text) <= _MAX_LENGTH:
                await _send_telegram_message(self._app.bot, chat_id, text)
            else:
                for i in range(0, len(text), _MAX_LENGTH):
                    await _send_telegram_message(self._app.bot, chat_id, text[i : i + _MAX_LENGTH])
        except Exception:
            logger.exception("Failed to send Telegram message", chat_id=chat_id)

    async def send_approval_card(
        self, chat_id: str, card: ApprovalCardPayload
    ) -> None:
        """Send an approval card as a single Telegram message with two
        InlineKeyboardButtons (v6.1 §P2b.1).

        Body text uses ``card.text_fallback`` so non-button channels
        (Slack, plain text) share the same summary and the renderer is
        consistent across surfaces. We deliberately do NOT set
        ``parse_mode`` here: Markdown rendering on attacker-controlled
        rationales (which flow into the summary) could be abused to
        smuggle hidden URLs into the button label area; plain text is
        safer and the buttons themselves carry the action affordance.
        """
        if self._app is None:
            return
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        _APPROVE_BUTTON_LABEL,
                        callback_data=f"{_APPROVE_CALLBACK_PREFIX}{card.request_id}",
                    ),
                    InlineKeyboardButton(
                        _REJECT_BUTTON_LABEL,
                        callback_data=f"{_REJECT_CALLBACK_PREFIX}{card.request_id}",
                    ),
                ]
            ]
        )
        body = card.text_fallback
        if card.approval_url and card.approval_url not in body:
            body = f"{body}\n  review: {card.approval_url}"
        # Telegram rejects message bodies > 4096 chars. Approval cards
        # carry a one-line summary plus a URL so they are nowhere near
        # the limit in practice, but we still clip defensively rather
        # than throw away the buttons by splitting the message.
        if len(body) > _MAX_LENGTH:
            body = body[: _MAX_LENGTH - 1] + "…"
        try:
            await self._app.bot.send_message(
                chat_id, body, reply_markup=keyboard
            )
        except Exception:
            logger.exception(
                "Failed to send Telegram approval card",
                chat_id=chat_id,
                request_id=card.request_id,
            )

    async def set_typing(self, chat_id: str, is_typing: bool) -> None:
        """Send typing indicator."""
        if not self._app or not is_typing:
            return
        with contextlib.suppress(Exception):
            await self._app.bot.send_chat_action(chat_id, ChatAction.TYPING)


class TelegramGateway:
    """Manages Telegram bots, deduplicating by token.

    Multiple coworkers sharing the same bot token get a single polling
    connection. Messages are dispatched to all matching bindings.
    """

    def __init__(self, on_message: MessageCallback) -> None:
        self._on_message = on_message
        # binding_id -> token (for reverse lookup)
        self._binding_to_token: dict[str, str] = {}
        # token -> _BotInstance (deduplicated)
        self._bots_by_token: dict[str, _BotInstance] = {}

    @property
    def channel_type(self) -> str:
        return "telegram"

    async def add_binding(self, binding: ChannelBinding) -> None:
        """Add a binding. If the token is already active, reuse the existing bot."""
        token = binding.credentials.get("bot_token", "")
        if not token:
            logger.warning("Telegram binding has no bot_token", binding_id=binding.id)
            return

        self._binding_to_token[binding.id] = token

        existing_bot = self._bots_by_token.get(token)
        if existing_bot is not None:
            # Reuse existing bot instance — just register this binding_id
            existing_bot.add_binding_id(binding.id, binding.bot_display_name)
            # v6.1 §P1.4: the new binding inherits the live bot's
            # @handle so the WebUI can build a deep-link the moment
            # the binding is created (instead of waiting for the next
            # gateway restart).
            if existing_bot._bot_username:
                try:
                    await update_channel_binding_bot_username(
                        binding.id, existing_bot._bot_username
                    )
                except Exception:
                    logger.exception(
                        "Failed to persist bot_username on binding add",
                        binding_id=binding.id,
                    )
            logger.info(
                "Telegram binding added to existing bot",
                binding_id=binding.id,
                bot_username=existing_bot._bot_username,
            )
            return

        # New token — create new bot instance
        bot = _BotInstance(token, self._on_message)
        bot.add_binding_id(binding.id, binding.bot_display_name)
        self._bots_by_token[token] = bot
        await bot.start()

    async def remove_binding(self, binding_id: str) -> None:
        """Remove a binding. Stop the bot only if no more bindings use its token."""
        token = self._binding_to_token.pop(binding_id, None)
        if token is None:
            return
        bot = self._bots_by_token.get(token)
        if bot is None:
            return
        bot.remove_binding_id(binding_id)
        if not bot.has_bindings:
            del self._bots_by_token[token]
            await bot.stop()

    async def send_message(self, binding_id: str, chat_id: str, text: str) -> None:
        """Send a message. Resolves binding_id to the correct bot via token."""
        token = self._binding_to_token.get(binding_id)
        if token is None:
            logger.warning("No token for binding", binding_id=binding_id)
            return
        bot = self._bots_by_token.get(token)
        if bot:
            await bot.send_message(chat_id, text)

    async def send_approval_card(
        self, binding_id: str, chat_id: str, card: ApprovalCardPayload
    ) -> None:
        """Send an approval card (v6.1 §P2b.1) via the binding's bot.

        Routes by ``binding_id`` so coworkers that share a Telegram
        token still attribute the card to the right bot conversation.
        """
        token = self._binding_to_token.get(binding_id)
        if token is None:
            logger.warning(
                "No token for binding (approval card)", binding_id=binding_id
            )
            return
        bot = self._bots_by_token.get(token)
        if bot:
            await bot.send_approval_card(chat_id, card)

    async def set_typing(self, binding_id: str, chat_id: str, is_typing: bool) -> None:
        """Send typing indicator via the correct bot."""
        token = self._binding_to_token.get(binding_id)
        if token is None:
            return
        bot = self._bots_by_token.get(token)
        if bot:
            await bot.set_typing(chat_id, is_typing)

    async def shutdown(self) -> None:
        """Stop all bots."""
        for bot in list(self._bots_by_token.values()):
            await bot.stop()
        self._bots_by_token.clear()
        self._binding_to_token.clear()
