"""Slack gateway — manages multiple Slack apps (one per coworker)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from rolemesh.core.logger import get_logger

if TYPE_CHECKING:
    from rolemesh.channels.gateway import MessageCallback
    from rolemesh.core.types import ChannelBinding

logger = get_logger()

_MAX_MESSAGE_LENGTH = 4000


def _slack_ts_to_iso(ts: str) -> str:
    """Convert Slack timestamp to ISO 8601."""
    from datetime import UTC, datetime

    try:
        return datetime.fromtimestamp(float(ts), tz=UTC).isoformat()
    except (ValueError, OSError):
        return ""


class _SlackAppInstance:
    """A single Slack app instance for one channel binding."""

    def __init__(self, binding: ChannelBinding, on_message: MessageCallback) -> None:
        self.binding = binding
        self._on_message = on_message
        self._bot_user_id: str | None = None
        self._user_name_cache: dict[str, str] = {}
        self._handler: AsyncSocketModeHandler | None = None
        self._connected = False

        bot_token = binding.credentials.get("bot_token", "")
        app_token = binding.credentials.get("app_token", "")
        self._app = AsyncApp(token=bot_token)
        self._app_token = app_token

        self._setup_handlers()

    def _setup_handlers(self) -> None:
        binding_id = self.binding.id

        @self._app.event("message")
        async def _on_message(event: dict[str, Any], say: Any) -> None:
            subtype = event.get("subtype")
            if subtype and subtype != "bot_message":
                return

            text = event.get("text")
            if not text:
                return

            channel_id = event.get("channel", "")
            chat_id = channel_id
            ts = event.get("ts", "0")
            timestamp = _slack_ts_to_iso(ts)
            is_group = event.get("channel_type") != "im"
            is_bot = bool(event.get("bot_id")) or event.get("user") == self._bot_user_id

            if is_bot:
                sender_name = self.binding.bot_display_name or "Bot"
            else:
                user_id = event.get("user", "")
                sender_name = await self._resolve_user_name(user_id) or user_id or "unknown"

            content = text
            if self._bot_user_id and not is_bot:
                mention = f"<@{self._bot_user_id}>"
                if mention in content:
                    bot_name = self.binding.bot_display_name or "Bot"
                    content = f"@{bot_name} {content}"

            sender = event.get("user") or event.get("bot_id", "")
            await self._on_message(binding_id, chat_id, sender, sender_name, content, timestamp, ts, is_group)

    async def start(self) -> None:
        """Connect the Slack app."""
        self._handler = AsyncSocketModeHandler(self._app, self._app_token)
        try:
            auth = await self._app.client.auth_test()
            self._bot_user_id = auth.get("user_id")
            logger.info("Slack app connected", bot_user_id=self._bot_user_id, binding_id=self.binding.id)
        except Exception:  # noqa: BLE001
            logger.warning("Connected to Slack but failed to get bot user ID", binding_id=self.binding.id)
        await self._handler.connect_async()  # type: ignore[no-untyped-call]
        self._connected = True

    async def stop(self) -> None:
        """Disconnect the Slack app."""
        self._connected = False
        if self._handler:
            await self._handler.close_async()  # type: ignore[no-untyped-call]
            self._handler = None

    async def send_message(self, chat_id: str, text: str) -> None:
        """Send a message to a Slack channel."""
        if not self._connected:
            return
        try:
            if len(text) <= _MAX_MESSAGE_LENGTH:
                await self._app.client.chat_postMessage(channel=chat_id, text=text)
            else:
                for i in range(0, len(text), _MAX_MESSAGE_LENGTH):
                    await self._app.client.chat_postMessage(channel=chat_id, text=text[i : i + _MAX_MESSAGE_LENGTH])
        except Exception:
            logger.exception("Failed to send Slack message", chat_id=chat_id, binding_id=self.binding.id)

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
            return None


class SlackGateway:
    """Manages multiple Slack apps (one per coworker)."""

    def __init__(self, on_message: MessageCallback) -> None:
        self._on_message = on_message
        self._apps: dict[str, _SlackAppInstance] = {}

    @property
    def channel_type(self) -> str:
        return "slack"

    async def add_binding(self, binding: ChannelBinding) -> None:
        """Start a new Slack app for this binding."""
        if binding.id in self._apps:
            return
        if not binding.credentials.get("bot_token") or not binding.credentials.get("app_token"):
            logger.warning("Slack binding missing tokens", binding_id=binding.id)
            return
        app_instance = _SlackAppInstance(binding, self._on_message)
        await app_instance.start()
        self._apps[binding.id] = app_instance

    async def remove_binding(self, binding_id: str) -> None:
        """Stop and remove a Slack app."""
        app = self._apps.pop(binding_id, None)
        if app:
            await app.stop()

    async def send_message(self, binding_id: str, chat_id: str, text: str) -> None:
        """Send a message via the specified Slack app."""
        app = self._apps.get(binding_id)
        if app:
            await app.send_message(chat_id, text)
        else:
            logger.warning("No Slack app for binding", binding_id=binding_id)

    async def set_typing(self, binding_id: str, chat_id: str, is_typing: bool) -> None:
        """Slack Bot API has no typing indicator — no-op."""
        pass

    async def shutdown(self) -> None:
        """Stop all Slack apps."""
        for app in list(self._apps.values()):
            await app.stop()
        self._apps.clear()
