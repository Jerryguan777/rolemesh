"""Telegram gateway — manages multiple Telegram bots (one per unique token).

When multiple coworkers share the same bot token, only one polling connection
is created. Incoming messages are dispatched to ALL bindings that share the
token, so each coworker's conversation lookup can match independently.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from telegram.constants import ChatAction, ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, filters

from rolemesh.core.logger import get_logger

if TYPE_CHECKING:
    from telegram import Bot, Update
    from telegram.ext import ContextTypes

    from rolemesh.channels.gateway import MessageCallback
    from rolemesh.core.types import ChannelBinding

logger = get_logger()

_MAX_LENGTH = 4096


async def _send_telegram_message(bot: Bot, chat_id: str | int, text: str) -> None:
    """Send with Markdown, falling back to plain text."""
    try:
        await bot.send_message(chat_id, text, parse_mode=ParseMode.MARKDOWN)
    except Exception:  # noqa: BLE001
        await bot.send_message(chat_id, text)


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

        async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
            logger.error("Telegram bot error", error=str(context.error))

        app.add_error_handler(_on_error)

        await app.initialize()
        me = await app.bot.get_me()
        self._bot_username = me.username
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
