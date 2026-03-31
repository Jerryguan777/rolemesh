"""Telegram channel implementation.

Uses python-telegram-bot (PTB) for bot polling.
Self-registers via registerChannel() on import.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from telegram.constants import ChatAction, ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, filters

from rolemesh.channels.registry import ChannelOpts, register_channel
from rolemesh.core.config import ASSISTANT_NAME, TRIGGER_PATTERN
from rolemesh.core.env import read_env_file
from rolemesh.core.logger import get_logger
from rolemesh.core.types import NewMessage

if TYPE_CHECKING:
    from telegram import Bot, Update
    from telegram.ext import ContextTypes

logger = get_logger()

# Telegram message length limit
_MAX_LENGTH = 4096


async def _send_telegram_message(bot: Bot, chat_id: str | int, text: str) -> None:
    """Send with Markdown, falling back to plain text."""
    try:
        await bot.send_message(chat_id, text, parse_mode=ParseMode.MARKDOWN)
    except Exception:  # noqa: BLE001
        await bot.send_message(chat_id, text)


class TelegramChannel:
    """Telegram bot channel using long polling."""

    name: str = "telegram"

    def __init__(self, bot_token: str, opts: ChannelOpts) -> None:
        self._token = bot_token
        self._opts = opts
        self._app: Application | None = None  # type: ignore[type-arg]
        self._bot_username: str | None = None

    async def connect(self) -> None:
        self._app = Application.builder().token(self._token).build()
        app = self._app

        # /chatid command
        async def _cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            if update.effective_chat is None:
                return
            chat = update.effective_chat
            chat_name = chat.title or (update.effective_user.first_name if update.effective_user else "Private")
            await chat.send_message(
                f"Chat ID: `tg:{chat.id}`\nName: {chat_name}\nType: {chat.type}",
                parse_mode=ParseMode.MARKDOWN,
            )

        # /ping command
        async def _cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            if update.effective_chat:
                await update.effective_chat.send_message(f"{ASSISTANT_NAME} is online.")

        app.add_handler(CommandHandler("chatid", _cmd_chatid))
        app.add_handler(CommandHandler("ping", _cmd_ping))

        # Text message handler
        async def _on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            msg = update.effective_message
            chat = update.effective_chat
            user = update.effective_user
            if msg is None or chat is None or msg.text is None:
                return

            # Skip bot commands handled above
            if msg.text.startswith("/"):
                cmd = msg.text.lstrip("/").split()[0].split("@")[0].lower()
                if cmd in ("chatid", "ping"):
                    return

            chat_jid = f"tg:{chat.id}"
            content = msg.text
            timestamp = msg.date.isoformat() if msg.date else ""
            sender_name = user.first_name if user else "Unknown"
            sender = str(user.id) if user else ""
            msg_id = str(msg.message_id)

            chat_name = chat.title if chat.title else sender_name

            # Translate @bot_username mentions to TRIGGER_PATTERN format
            if self._bot_username and msg.entities:
                for entity in msg.entities:
                    if entity.type == "mention":
                        mention_text = content[entity.offset : entity.offset + entity.length].lower()
                        if mention_text == f"@{self._bot_username.lower()}":
                            if not TRIGGER_PATTERN.search(content):
                                content = f"@{ASSISTANT_NAME} {content}"
                            break

            is_group = chat.type in ("group", "supergroup")
            await self._opts.on_chat_metadata(chat_jid, timestamp, chat_name, "telegram", is_group)

            group = self._opts.registered_groups().get(chat_jid)
            if not group:
                logger.debug("Message from unregistered Telegram chat", chat_jid=chat_jid, chat_name=chat_name)
                return

            self._opts.on_message(
                chat_jid,
                NewMessage(
                    id=msg_id,
                    chat_jid=chat_jid,
                    sender=sender,
                    sender_name=sender_name,
                    content=content,
                    timestamp=timestamp,
                ),
            )
            logger.info("Telegram message stored", chat_jid=chat_jid, chat_name=chat_name, sender=sender_name)

        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _on_text))

        # Non-text media handlers
        def _make_media_handler(placeholder: str) -> MessageHandler:  # type: ignore[type-arg]
            async def _handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
                msg = update.effective_message
                chat = update.effective_chat
                user = update.effective_user
                if msg is None or chat is None:
                    return
                chat_jid = f"tg:{chat.id}"
                group = self._opts.registered_groups().get(chat_jid)
                if not group:
                    return
                timestamp = msg.date.isoformat() if msg.date else ""
                sender_name = user.first_name if user else "Unknown"
                caption = f" {msg.caption}" if msg.caption else ""
                is_group = chat.type in ("group", "supergroup")
                await self._opts.on_chat_metadata(chat_jid, timestamp, None, "telegram", is_group)
                self._opts.on_message(
                    chat_jid,
                    NewMessage(
                        id=str(msg.message_id),
                        chat_jid=chat_jid,
                        sender=str(user.id) if user else "",
                        sender_name=sender_name,
                        content=f"{placeholder}{caption}",
                        timestamp=timestamp,
                    ),
                )

            return MessageHandler(filters.ALL, _handler)

        # Register media handlers (order matters — first match wins in PTB)
        # These are added AFTER the text handler so text messages are handled first
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
                chat_jid = f"tg:{chat.id}"
                group = self._opts.registered_groups().get(chat_jid)
                if not group:
                    return
                timestamp = msg.date.isoformat() if msg.date else ""
                sender_name = user.first_name if user else "Unknown"
                caption = f" {msg.caption}" if msg.caption else ""
                is_group = chat.type in ("group", "supergroup")
                await self._opts.on_chat_metadata(chat_jid, timestamp, None, "telegram", is_group)
                self._opts.on_message(
                    chat_jid,
                    NewMessage(
                        id=str(msg.message_id),
                        chat_jid=chat_jid,
                        sender=str(user.id) if user else "",
                        sender_name=sender_name,
                        content=f"{_ph}{caption}",
                        timestamp=timestamp,
                    ),
                )

            app.add_handler(MessageHandler(filt, _media_handler))

        # Error handler
        async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
            logger.error("Telegram bot error", error=str(context.error))

        app.add_error_handler(_on_error)

        # Initialize and start polling
        await app.initialize()
        me = await app.bot.get_me()
        self._bot_username = me.username
        logger.info("Telegram bot connected", username=me.username, bot_id=me.id)

        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)  # type: ignore[union-attr]

    async def send_message(self, jid: str, text: str) -> None:
        if self._app is None:
            logger.warning("Telegram bot not initialized")
            return
        numeric_id = jid.removeprefix("tg:")
        try:
            if len(text) <= _MAX_LENGTH:
                await _send_telegram_message(self._app.bot, numeric_id, text)
            else:
                for i in range(0, len(text), _MAX_LENGTH):
                    await _send_telegram_message(self._app.bot, numeric_id, text[i : i + _MAX_LENGTH])
            logger.info("Telegram message sent", jid=jid, length=len(text))
        except Exception:
            logger.exception("Failed to send Telegram message", jid=jid)

    def is_connected(self) -> bool:
        return self._app is not None

    def owns_jid(self, jid: str) -> bool:
        return jid.startswith("tg:")

    async def disconnect(self) -> None:
        if self._app:
            await self._app.updater.stop()  # type: ignore[union-attr]
            await self._app.stop()
            await self._app.shutdown()
            self._app = None
            logger.info("Telegram bot stopped")

    async def set_typing(self, jid: str, is_typing: bool) -> None:
        if not self._app or not is_typing:
            return
        try:
            numeric_id = jid.removeprefix("tg:")
            await self._app.bot.send_chat_action(numeric_id, ChatAction.TYPING)
        except Exception:  # noqa: BLE001
            logger.debug("Failed to send Telegram typing indicator", jid=jid)


def _telegram_factory(opts: ChannelOpts) -> TelegramChannel | None:
    env_vars = read_env_file(["TELEGRAM_BOT_TOKEN"])
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or env_vars.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        logger.warning("Telegram: TELEGRAM_BOT_TOKEN not set")
        return None
    return TelegramChannel(token, opts)


register_channel("telegram", _telegram_factory)
