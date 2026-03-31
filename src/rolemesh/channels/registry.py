"""Channel factory registry.

Channels self-register at startup by calling register_channel().
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from rolemesh.core.types import Channel, OnChatMetadata, OnInboundMessage, RegisteredGroup


@dataclass
class ChannelOpts:
    """Options passed to channel factories during initialization."""

    on_message: OnInboundMessage
    on_chat_metadata: OnChatMetadata
    registered_groups: Callable[[], dict[str, RegisteredGroup]]


ChannelFactory = "Callable[[ChannelOpts], Channel | None]"

_registry: dict[str, Callable[[ChannelOpts], Channel | None]] = {}


def register_channel(name: str, factory: Callable[[ChannelOpts], Channel | None]) -> None:
    """Register a channel factory by name."""
    _registry[name] = factory


def get_channel_factory(name: str) -> Callable[[ChannelOpts], Channel | None] | None:
    """Look up a channel factory by name."""
    return _registry.get(name)


def get_registered_channel_names() -> list[str]:
    """List all registered channel names."""
    return list(_registry.keys())
