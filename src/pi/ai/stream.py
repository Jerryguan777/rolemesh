"""Stream dispatch functions — Python port of packages/ai/src/stream.ts.

Unlike the TS version, we use native AsyncIterator instead of EventStream.
The result is collected by iterating the stream to completion.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from pi.ai.api_registry import ApiProvider, get_api_provider
from pi.ai.types import (
    AssistantMessage,
    AssistantMessageEvent,
    Context,
    DoneEvent,
    ErrorEvent,
    Model,
    SimpleStreamOptions,
    StreamOptions,
)


def _resolve_api_provider(api: str) -> ApiProvider:
    provider = get_api_provider(api)
    if provider is None:
        raise ValueError(f"No API provider registered for api: {api}")
    return provider


def stream(
    model: Model,
    context: Context,
    options: StreamOptions | None = None,
) -> AsyncIterator[AssistantMessageEvent]:
    """Stream events from an API provider."""
    provider = _resolve_api_provider(model.api)
    return provider.stream(model, context, options)


async def complete(
    model: Model,
    context: Context,
    options: StreamOptions | None = None,
) -> AssistantMessage:
    """Stream and collect the final AssistantMessage."""
    result: AssistantMessage | None = None
    async for event in stream(model, context, options):
        if isinstance(event, DoneEvent):
            result = event.message
        elif isinstance(event, ErrorEvent):
            result = event.error
    if result is None:
        raise RuntimeError("Stream ended without a done or error event")
    return result


def stream_simple(
    model: Model,
    context: Context,
    options: SimpleStreamOptions | None = None,
) -> AsyncIterator[AssistantMessageEvent]:
    """Stream events using the simple (reasoning-aware) API."""
    provider = _resolve_api_provider(model.api)
    return provider.stream_simple(model, context, options)


async def complete_simple(
    model: Model,
    context: Context,
    options: SimpleStreamOptions | None = None,
) -> AssistantMessage:
    """Stream simple and collect the final AssistantMessage."""
    result: AssistantMessage | None = None
    async for event in stream_simple(model, context, options):
        if isinstance(event, DoneEvent):
            result = event.message
        elif isinstance(event, ErrorEvent):
            result = event.error
    if result is None:
        raise RuntimeError("Stream ended without a done or error event")
    return result
