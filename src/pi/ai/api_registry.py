"""Provider registry for API backends — Python port of packages/ai/src/api-registry.ts."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

from pi.ai.types import (
    AssistantMessageEvent,
    Context,
    Model,
    SimpleStreamOptions,
    StreamOptions,
)

# Stream function type: (model, context, options?) -> AsyncIterator[AssistantMessageEvent]
StreamFunction = Callable[
    [Model, Context, StreamOptions | None],
    AsyncIterator[AssistantMessageEvent],
]

StreamSimpleFunction = Callable[
    [Model, Context, SimpleStreamOptions | None],
    AsyncIterator[AssistantMessageEvent],
]

# ApiStreamFunction / ApiStreamSimpleFunction are internal wrappers that
# erase the generic TApi/TOptions parameters.  In TS they are distinct
# types; in Python, StreamFunction already uses a concrete Model (no
# generic), so the aliases are identical but kept for parity.
ApiStreamFunction = StreamFunction
ApiStreamSimpleFunction = StreamSimpleFunction


@dataclass
class ApiProvider:
    api: str
    stream: StreamFunction
    stream_simple: StreamSimpleFunction


@dataclass
class _RegisteredApiProvider:
    provider: ApiProvider
    source_id: str | None = None


# Note: module-level mutable state. Safe for single-threaded asyncio use.
# Not thread-safe — do not use with pytest-xdist or multi-threaded runtimes.
_api_provider_registry: dict[str, _RegisteredApiProvider] = {}


def register_api_provider(provider: ApiProvider, source_id: str | None = None) -> None:
    """Register an API provider."""

    def _wrap_stream(api: str, stream_fn: StreamFunction) -> StreamFunction:
        def wrapper(
            model: Model, context: Context, options: StreamOptions | None = None
        ) -> AsyncIterator[AssistantMessageEvent]:
            if model.api != api:
                raise ValueError(f"Mismatched api: {model.api} expected {api}")
            return stream_fn(model, context, options)

        return wrapper

    def _wrap_stream_simple(api: str, stream_fn: StreamSimpleFunction) -> StreamSimpleFunction:
        def wrapper(
            model: Model, context: Context, options: SimpleStreamOptions | None = None
        ) -> AsyncIterator[AssistantMessageEvent]:
            if model.api != api:
                raise ValueError(f"Mismatched api: {model.api} expected {api}")
            return stream_fn(model, context, options)

        return wrapper

    _api_provider_registry[provider.api] = _RegisteredApiProvider(
        provider=ApiProvider(
            api=provider.api,
            stream=_wrap_stream(provider.api, provider.stream),
            stream_simple=_wrap_stream_simple(provider.api, provider.stream_simple),
        ),
        source_id=source_id,
    )


def get_api_provider(api: str) -> ApiProvider | None:
    """Get a registered API provider by API name."""
    entry = _api_provider_registry.get(api)
    return entry.provider if entry else None


def get_api_providers() -> list[ApiProvider]:
    """Get all registered API providers."""
    return [entry.provider for entry in _api_provider_registry.values()]


def unregister_api_providers(source_id: str) -> None:
    """Remove all API providers registered with the given source ID."""
    to_remove = [api for api, entry in _api_provider_registry.items() if entry.source_id == source_id]
    for api in to_remove:
        del _api_provider_registry[api]


def clear_api_providers() -> None:
    """Remove all registered API providers."""
    _api_provider_registry.clear()
