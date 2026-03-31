"""Slack channel implementation.

Uses slack-bolt (Python) with Socket Mode.
Self-registers via registerChannel() on import.
"""

from __future__ import annotations

import os
from typing import Any

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from rolemesh.channels.registry import ChannelOpts, register_channel
from rolemesh.core.config import ASSISTANT_NAME, TRIGGER_PATTERN
from rolemesh.core.env import read_env_file
from rolemesh.core.logger import get_logger
from rolemesh.core.types import NewMessage
from rolemesh.db.pg import update_chat_name

logger = get_logger()

# Slack message length limit
_MAX_MESSAGE_LENGTH = 4000


class SlackChannel:
    """Slack channel using Socket Mode (no public URL needed)."""

    name: str = "slack"

    def __init__(self, opts: ChannelOpts) -> None:
        self._opts = opts
        self._connected = False
        self._bot_user_id: str | None = None
        self._outgoing_queue: list[dict[str, str]] = []
        self._flushing = False
        self._user_name_cache: dict[str, str] = {}
        self._handler: AsyncSocketModeHandler | None = None

        env = read_env_file(["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"])
        bot_token = env.get("SLACK_BOT_TOKEN", "")
        app_token = env.get("SLACK_APP_TOKEN", "")

        if not bot_token or not app_token:
            raise RuntimeError("SLACK_BOT_TOKEN and SLACK_APP_TOKEN must be set in .env")

        self._app = AsyncApp(token=bot_token)
        self._app_token = app_token

        self._setup_event_handlers()

    def _setup_event_handlers(self) -> None:
        @self._app.event("message")
        async def _on_message(event: dict[str, Any], say: Any) -> None:
            subtype = event.get("subtype")
            if subtype and subtype != "bot_message":
                return

            text = event.get("text")
            if not text:
                return

            channel_id = event.get("channel", "")
            jid = f"slack:{channel_id}"
            ts = event.get("ts", "0")
            timestamp = _slack_ts_to_iso(ts)
            is_group = event.get("channel_type") != "im"

            await self._opts.on_chat_metadata(jid, timestamp, None, "slack", is_group)

            groups = self._opts.registered_groups()
            if jid not in groups:
                return

            is_bot = bool(event.get("bot_id")) or event.get("user") == self._bot_user_id

            if is_bot:
                sender_name = ASSISTANT_NAME
            else:
                user_id = event.get("user", "")
                sender_name = await self._resolve_user_name(user_id) or user_id or "unknown"

            content = text
            if self._bot_user_id and not is_bot:
                mention = f"<@{self._bot_user_id}>"
                if mention in content and not TRIGGER_PATTERN.search(content):
                    content = f"@{ASSISTANT_NAME} {content}"

            self._opts.on_message(
                jid,
                NewMessage(
                    id=ts,
                    chat_jid=jid,
                    sender=event.get("user") or event.get("bot_id", ""),
                    sender_name=sender_name,
                    content=content,
                    timestamp=timestamp,
                    is_from_me=is_bot,
                    is_bot_message=is_bot,
                ),
            )

    async def connect(self) -> None:
        self._handler = AsyncSocketModeHandler(self._app, self._app_token)

        # Get bot user ID before starting
        try:
            auth = await self._app.client.auth_test()
            self._bot_user_id = auth.get("user_id")
            logger.info("Connected to Slack", bot_user_id=self._bot_user_id)
        except Exception:  # noqa: BLE001
            logger.warning("Connected to Slack but failed to get bot user ID")

        await self._handler.connect_async()  # type: ignore[no-untyped-call]
        self._connected = True

        await self._flush_outgoing_queue()
        await self._sync_channel_metadata()

    async def send_message(self, jid: str, text: str) -> None:
        channel_id = jid.removeprefix("slack:")

        if not self._connected:
            self._outgoing_queue.append({"jid": jid, "text": text})
            logger.info("Slack disconnected, message queued", jid=jid, queue_size=len(self._outgoing_queue))
            return

        try:
            if len(text) <= _MAX_MESSAGE_LENGTH:
                await self._app.client.chat_postMessage(channel=channel_id, text=text)
            else:
                for i in range(0, len(text), _MAX_MESSAGE_LENGTH):
                    await self._app.client.chat_postMessage(channel=channel_id, text=text[i : i + _MAX_MESSAGE_LENGTH])
            logger.info("Slack message sent", jid=jid, length=len(text))
        except Exception:  # noqa: BLE001
            self._outgoing_queue.append({"jid": jid, "text": text})
            logger.warning("Failed to send Slack message, queued", jid=jid, queue_size=len(self._outgoing_queue))

    def is_connected(self) -> bool:
        return self._connected

    def owns_jid(self, jid: str) -> bool:
        return jid.startswith("slack:")

    async def disconnect(self) -> None:
        self._connected = False
        if self._handler:
            await self._handler.close_async()  # type: ignore[no-untyped-call]
            self._handler = None

    async def set_typing(self, jid: str, is_typing: bool) -> None:
        # Slack Bot API has no typing indicator endpoint — no-op
        pass

    async def sync_groups(self, force: bool) -> None:
        await self._sync_channel_metadata()

    async def _sync_channel_metadata(self) -> None:
        try:
            logger.info("Syncing channel metadata from Slack...")
            cursor: str | None = None
            count = 0

            while True:
                kwargs: dict[str, Any] = {
                    "types": "public_channel,private_channel",
                    "exclude_archived": True,
                    "limit": 200,
                }
                if cursor:
                    kwargs["cursor"] = cursor

                result = await self._app.client.conversations_list(**kwargs)

                channels_list: Any = result.get("channels", [])
                for ch in channels_list:
                    if ch.get("id") and ch.get("name") and ch.get("is_member"):
                        await update_chat_name(f"slack:{ch['id']}", ch["name"])
                        count += 1

                resp_meta: Any = result.get("response_metadata", {})
                cursor = resp_meta.get("next_cursor")
                if not cursor:
                    break

            logger.info("Slack channel metadata synced", count=count)
        except Exception:
            logger.exception("Failed to sync Slack channel metadata")

    async def _resolve_user_name(self, user_id: str) -> str | None:
        if not user_id:
            return None
        cached = self._user_name_cache.get(user_id)
        if cached:
            return cached
        try:
            result = await self._app.client.users_info(user=user_id)
            user_data: Any = result.get("user", {})
            name: str | None = user_data.get("real_name") or user_data.get("name")
            if name:
                self._user_name_cache[user_id] = name
            return name
        except Exception:  # noqa: BLE001
            logger.debug("Failed to resolve Slack user name", user_id=user_id)
            return None

    async def _flush_outgoing_queue(self) -> None:
        if self._flushing or not self._outgoing_queue:
            return
        self._flushing = True
        try:
            logger.info("Flushing Slack outgoing queue", count=len(self._outgoing_queue))
            while self._outgoing_queue:
                item = self._outgoing_queue.pop(0)
                channel_id = item["jid"].removeprefix("slack:")
                await self._app.client.chat_postMessage(channel=channel_id, text=item["text"])
                logger.info("Queued Slack message sent", jid=item["jid"], length=len(item["text"]))
        finally:
            self._flushing = False


def _slack_ts_to_iso(ts: str) -> str:
    """Convert Slack timestamp (e.g. '1234567890.123456') to ISO 8601."""
    from datetime import UTC, datetime

    try:
        return datetime.fromtimestamp(float(ts), tz=UTC).isoformat()
    except (ValueError, OSError):
        return ""


def _slack_factory(opts: ChannelOpts) -> SlackChannel | None:
    env_vars = read_env_file(["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"])
    bot_token = os.environ.get("SLACK_BOT_TOKEN") or env_vars.get("SLACK_BOT_TOKEN", "")
    app_token = os.environ.get("SLACK_APP_TOKEN") or env_vars.get("SLACK_APP_TOKEN", "")
    if not bot_token or not app_token:
        logger.warning("Slack: SLACK_BOT_TOKEN or SLACK_APP_TOKEN not set")
        return None
    return SlackChannel(opts)


register_channel("slack", _slack_factory)
