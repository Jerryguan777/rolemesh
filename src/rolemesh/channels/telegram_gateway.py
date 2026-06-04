"""Telegram gateway — manages multiple Telegram bots (one per unique token).

When multiple coworkers share the same bot token, only one polling connection
is created. Incoming messages are dispatched to ALL bindings that share the
token, so each coworker's conversation lookup can match independently.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

import asyncpg
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
    update_channel_binding_bot_username,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from telegram import Bot, Update
    from telegram.ext import ContextTypes

    from rolemesh.channels.gateway import MessageCallback
    from rolemesh.core.types import ChannelBinding

    # (request_id, decision, telegram_user_id, chat_id) -> optional toast text.
    # ``decision`` is "approve" | "reject". The handler resolves the approver's
    # RoleMesh identity from (tenant, telegram_user_id) server-side — the
    # callback only ever carries the request_id + verb (IDOR guard, §10 S4).
    ApprovalDecisionCallback = Callable[
        [str, str, str, str], Awaitable[str | None]
    ]

logger = get_logger()

_MAX_LENGTH = 4096
# v6.1 §P1.5: 1:1 only. Includes 'channel' (broadcast) defensively —
# telegram-bot-api rarely delivers normal messages from a channel but
# the type set is what the design specifies.
_GROUP_CHAT_TYPES = ("group", "supergroup", "channel")


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
# HITL approval card (docs/21-hitl-approval-plan.md §10 S4)
# ---------------------------------------------------------------------------
#
# callback_data is "apr:{request_id}" / "rej:{request_id}". A UUID request_id
# keeps the whole token ≈ 40 bytes, within Telegram's 64-byte limit (§6). It
# carries NO approver identity — that is resolved server-side from the
# authenticated Telegram sender (IDOR guard).

_APPROVE_PREFIX = "apr:"
_REJECT_PREFIX = "rej:"


def _approval_keyboard(request_id: str) -> InlineKeyboardMarkup:
    """The two-button ✅/❌ inline keyboard for one pending approval."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Approve", callback_data=f"{_APPROVE_PREFIX}{request_id}"
                ),
                InlineKeyboardButton(
                    "❌ Reject", callback_data=f"{_REJECT_PREFIX}{request_id}"
                ),
            ]
        ]
    )


def parse_approval_callback(data: str | None) -> tuple[str, str] | None:
    """Parse ``callback_data`` into ``(decision, request_id)`` or ``None``.

    ``decision`` is normalised to "approve" / "reject". Anything that is not a
    well-formed approval callback (other buttons, empty id) returns ``None`` so
    the handler ignores it rather than dispatching a bogus decision.
    """
    if not data:
        return None
    if data.startswith(_APPROVE_PREFIX):
        request_id = data[len(_APPROVE_PREFIX):]
        return ("approve", request_id) if request_id else None
    if data.startswith(_REJECT_PREFIX):
        request_id = data[len(_REJECT_PREFIX):]
        return ("reject", request_id) if request_id else None
    return None


async def _handle_approval_callback(
    update: Update, on_decision: ApprovalDecisionCallback | None
) -> None:
    """Dispatch a tapped ✅/❌ button to the orchestrator decision funnel.

    Module-level (not a closure) so the parse + dispatch + answer flow is
    testable with a stub ``Update`` instead of a live ``Application``. The
    Telegram-authenticated ``from_user.id`` is the only identity input; the
    callback payload is never trusted for *who* approved, only *what* request
    and *which* verb.
    """
    query = update.callback_query
    if query is None:
        return
    parsed = parse_approval_callback(query.data)
    if parsed is None:
        # Not ours (or malformed) — still answer so the client spinner clears.
        with contextlib.suppress(Exception):
            await query.answer()
        return
    decision, request_id = parsed
    user = query.from_user
    chat = query.message.chat if query.message is not None else None
    telegram_user_id = str(user.id) if user is not None else ""
    chat_id = str(chat.id) if chat is not None else ""

    toast: str | None = None
    if on_decision is not None:
        try:
            toast = await on_decision(
                request_id, decision, telegram_user_id, chat_id
            )
        except Exception:
            logger.exception(
                "telegram approval callback dispatch failed",
                request_id=request_id,
            )
            toast = "Could not record your decision; please retry."
    with contextlib.suppress(Exception):
        await query.answer(text=toast or None)


class _BotInstance:
    """A single Telegram bot instance for one unique token.

    May serve multiple bindings (coworkers) that share the same token.
    """

    def __init__(
        self,
        token: str,
        on_message: MessageCallback,
        on_approval_decision: ApprovalDecisionCallback | None = None,
    ) -> None:
        self._token = token
        self._on_message = on_message
        self._on_approval_decision = on_approval_decision
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
            # before any binding sees the message — the product does
            # not support group chat.
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
                await self._on_message(bid, chat_id, sender, sender_name, bid_content, timestamp, msg_id)

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
                for bid in self._binding_ids:
                    await self._on_message(
                        bid,
                        chat_id,
                        sender,
                        sender_name,
                        f"{_ph}{caption}",
                        timestamp,
                        str(msg.message_id),
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

        # HITL approval: ✅/❌ inline-button taps. Restricted to our
        # "apr:"/"rej:" callback_data so it never swallows callbacks from a
        # future unrelated keyboard sharing this bot.
        async def _on_approval(
            update: Update, context: ContextTypes.DEFAULT_TYPE
        ) -> None:
            await _handle_approval_callback(update, self._on_approval_decision)

        app.add_handler(
            CallbackQueryHandler(
                _on_approval, pattern=rf"^({_APPROVE_PREFIX}|{_REJECT_PREFIX})"
            )
        )

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

    async def set_typing(self, chat_id: str, is_typing: bool) -> None:
        """Send typing indicator."""
        if not self._app or not is_typing:
            return
        with contextlib.suppress(Exception):
            await self._app.bot.send_chat_action(chat_id, ChatAction.TYPING)

    async def send_approval_card(
        self, chat_id: str, request_id: str, text: str
    ) -> int | None:
        """Send the ✅/❌ approval card; return its ``message_id`` for later edit.

        ``text`` is the fully-rendered plain-text body (built upstream by
        ``approval_notify.pending_card_text`` so the Telegram card mirrors the
        web chat card). Sent with no ``parse_mode`` so user-supplied param
        values need no MarkdownV2 escaping.
        """
        if self._app is None:
            return None
        try:
            sent = await self._app.bot.send_message(
                chat_id, text, reply_markup=_approval_keyboard(request_id)
            )
        except Exception:
            logger.exception("Failed to send Telegram approval card", chat_id=chat_id)
            return None
        return int(sent.message_id)

    async def edit_approval_card(
        self, chat_id: str, message_id: int, text: str
    ) -> None:
        """Edit a delivered card to its terminal state, dropping the buttons."""
        if self._app is None:
            return
        with contextlib.suppress(Exception):
            await self._app.bot.edit_message_text(
                text, chat_id=chat_id, message_id=message_id, reply_markup=None
            )


class TelegramGateway:
    """Manages Telegram bots, deduplicating by token.

    Multiple coworkers sharing the same bot token get a single polling
    connection. Messages are dispatched to all matching bindings.
    """

    def __init__(self, on_message: MessageCallback) -> None:
        self._on_message = on_message
        self._on_approval_decision: ApprovalDecisionCallback | None = None
        # binding_id -> token (for reverse lookup)
        self._binding_to_token: dict[str, str] = {}
        # token -> _BotInstance (deduplicated)
        self._bots_by_token: dict[str, _BotInstance] = {}

    @property
    def channel_type(self) -> str:
        return "telegram"

    def set_on_approval_decision(self, fn: ApprovalDecisionCallback) -> None:
        """Register the HITL approval-decision callback (orchestrator funnel).

        Set before bindings are added so every bot instance is built with it.
        ``fn(request_id, decision, telegram_user_id, chat_id)`` resolves the
        approver server-side and returns optional toast text for the tap.
        """
        self._on_approval_decision = fn

    def _bot_for_binding(self, binding_id: str) -> _BotInstance | None:
        token = self._binding_to_token.get(binding_id)
        if token is None:
            return None
        return self._bots_by_token.get(token)

    async def send_approval_card(
        self, binding_id: str, chat_id: str, request_id: str, text: str
    ) -> int | None:
        """Resolve the binding's bot and send an approval card."""
        bot = self._bot_for_binding(binding_id)
        if bot is None:
            logger.warning("No telegram bot for binding", binding_id=binding_id)
            return None
        return await bot.send_approval_card(chat_id, request_id, text)

    async def edit_approval_card(
        self, binding_id: str, chat_id: str, message_id: int, text: str
    ) -> None:
        """Resolve the binding's bot and edit a delivered card."""
        bot = self._bot_for_binding(binding_id)
        if bot is not None:
            await bot.edit_approval_card(chat_id, message_id, text)

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
        bot = _BotInstance(token, self._on_message, self._on_approval_decision)
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
