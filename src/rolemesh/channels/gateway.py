"""ChannelGateway protocol for managing multiple bot instances per channel type."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from rolemesh.core.types import ChannelBinding

# Callback: (binding_id, chat_id, sender_id, sender_name, text, timestamp, msg_id, is_group) -> None
MessageCallback = Callable[
    [str, str, str, str, str, str, str, bool],
    Awaitable[None],
]


class ChannelGateway(Protocol):
    """Manages multiple bot instances for one channel type.

    One gateway per channel TYPE (e.g. TelegramGateway, SlackGateway),
    managing potentially many bots (one per coworker).
    """

    @property
    def channel_type(self) -> str: ...

    async def add_binding(self, binding: ChannelBinding) -> None: ...

    async def remove_binding(self, binding_id: str) -> None: ...

    async def send_message(self, binding_id: str, chat_id: str, text: str) -> None: ...

    async def set_typing(self, binding_id: str, chat_id: str, is_typing: bool) -> None: ...

    async def shutdown(self) -> None: ...
