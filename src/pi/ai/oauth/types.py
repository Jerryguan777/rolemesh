"""OAuth type definitions.

Ported from packages/ai/src/utils/oauth/types.ts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from pi.ai.types import Model


@dataclass
class OAuthCredentials:
    refresh: str = ""
    access: str = ""
    expires: int = 0  # Unix timestamp in milliseconds
    extra: dict[str, Any] = field(default_factory=dict)

    def __getitem__(self, key: str) -> Any:
        if key in ("refresh", "access", "expires"):
            return getattr(self, key)
        return self.extra[key]

    def __setitem__(self, key: str, value: Any) -> None:
        if key in ("refresh", "access", "expires"):
            setattr(self, key, value)
        else:
            self.extra[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        if key in ("refresh", "access", "expires"):
            return getattr(self, key)
        return self.extra.get(key, default)


OAuthProviderId = str

# Deprecated: Use OAuthProviderId instead
OAuthProvider = OAuthProviderId


@dataclass
class OAuthPrompt:
    message: str = ""
    placeholder: str | None = None
    allow_empty: bool = False


@dataclass
class OAuthAuthInfo:
    url: str = ""
    instructions: str | None = None


@dataclass
class OAuthLoginCallbacks:
    on_auth: Any = None  # Callable[[OAuthAuthInfo], None]
    on_prompt: Any = None  # Callable[[OAuthPrompt], Awaitable[str]]
    on_progress: Any = None  # Callable[[str], None] | None
    on_manual_code_input: Any = None  # Callable[[], Awaitable[str]] | None
    signal: Any = None  # asyncio.Event | None


class OAuthProviderInterface(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def name(self) -> str: ...

    @property
    def uses_callback_server(self) -> bool: ...

    async def login(self, callbacks: OAuthLoginCallbacks) -> OAuthCredentials: ...

    async def refresh_token(self, credentials: OAuthCredentials) -> OAuthCredentials: ...

    def get_api_key(self, credentials: OAuthCredentials) -> str: ...

    def modify_models(self, models: list[Model], credentials: OAuthCredentials) -> list[Model]: ...


@dataclass
class OAuthProviderInfo:
    """Deprecated: Use OAuthProviderInterface instead."""

    id: str = ""
    name: str = ""
    available: bool = True
