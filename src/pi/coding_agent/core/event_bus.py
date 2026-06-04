"""Event bus — Python port of packages/coding-agent/src/core/event-bus.ts."""

from __future__ import annotations

import contextlib
import logging
from collections import defaultdict
from collections.abc import Callable
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class EventBus(Protocol):
    """Protocol for the event bus — pub/sub by channel name."""

    def emit(self, channel: str, data: Any) -> None:
        """Emit an event on a channel."""
        ...

    def on(self, channel: str, handler: Callable[[Any], None]) -> Callable[[], None]:
        """Subscribe a handler to a channel. Returns an unsubscribe function."""
        ...


class EventBusController(EventBus, Protocol):
    """Extended event bus with clear() for lifecycle management."""

    def clear(self) -> None:
        """Remove all handlers from all channels."""
        ...


class _EventBusImpl:
    """Concrete implementation of EventBus using a handler dict."""

    def __init__(self) -> None:
        # Maps channel name to list of handlers
        self._handlers: dict[str, list[Callable[[Any], None]]] = defaultdict(list)

    def emit(self, channel: str, data: Any) -> None:
        """Emit an event to all handlers subscribed to the channel."""
        for handler in list(self._handlers.get(channel, [])):
            try:
                handler(data)
            except Exception:
                logger.exception("Event handler error (%s)", channel)

    def on(self, channel: str, handler: Callable[[Any], None]) -> Callable[[], None]:
        """Subscribe handler to channel. Returns unsubscribe function."""
        self._handlers[channel].append(handler)

        def unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._handlers[channel].remove(handler)

        return unsubscribe

    def clear(self) -> None:
        """Remove all handlers from all channels."""
        self._handlers.clear()


def create_event_bus() -> EventBusController:
    """Create and return a new EventBus instance."""
    return _EventBusImpl()
