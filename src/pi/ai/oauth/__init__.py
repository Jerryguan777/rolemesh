"""OAuth credential management for AI providers.

Ported from packages/ai/src/utils/oauth/index.ts.
"""

from __future__ import annotations

import time
from typing import Any

from pi.ai.oauth.anthropic_oauth import anthropic_oauth_provider, login_anthropic, refresh_anthropic_token
from pi.ai.oauth.github_copilot import (
    get_github_copilot_base_url,
    github_copilot_oauth_provider,
    login_github_copilot,
    normalize_domain,
    refresh_github_copilot_token,
)
from pi.ai.oauth.openai_codex import (
    login_openai_codex,
    openai_codex_oauth_provider,
    refresh_openai_codex_token,
)
from pi.ai.oauth.pkce import generate_pkce
from pi.ai.oauth.types import (
    OAuthAuthInfo,
    OAuthCredentials,
    OAuthLoginCallbacks,
    OAuthPrompt,
    OAuthProvider,
    OAuthProviderId,
    OAuthProviderInfo,
    OAuthProviderInterface,
)

# Google OAuth modules removed from RoleMesh (contained embedded client credentials).
# Google Vertex/Gemini CLI providers still work via API keys or gcloud auth.

__all__ = [
    "OAuthAuthInfo",
    "OAuthCredentials",
    "OAuthLoginCallbacks",
    "OAuthPrompt",
    "OAuthProvider",
    "OAuthProviderId",
    "OAuthProviderInfo",
    "OAuthProviderInterface",
    "anthropic_oauth_provider",
    "generate_pkce",
    "get_github_copilot_base_url",
    "get_oauth_api_key",
    "get_oauth_provider",
    "get_oauth_provider_info_list",
    "get_oauth_providers",
    "github_copilot_oauth_provider",
    "login_anthropic",
    "login_github_copilot",
    "login_openai_codex",
    "normalize_domain",
    "openai_codex_oauth_provider",
    "refresh_anthropic_token",
    "refresh_github_copilot_token",
    "refresh_oauth_token",
    "refresh_openai_codex_token",
    "register_oauth_provider",
]

# Provider Registry
_oauth_provider_registry: dict[str, Any] = {
    anthropic_oauth_provider.id: anthropic_oauth_provider,
    github_copilot_oauth_provider.id: github_copilot_oauth_provider,
    openai_codex_oauth_provider.id: openai_codex_oauth_provider,
}


def get_oauth_provider(id_: str) -> Any | None:
    """Get an OAuth provider by ID."""
    return _oauth_provider_registry.get(id_)


def register_oauth_provider(provider: Any) -> None:
    """Register a custom OAuth provider."""
    _oauth_provider_registry[provider.id] = provider


def get_oauth_providers() -> list[Any]:
    """Get all registered OAuth providers."""
    return list(_oauth_provider_registry.values())


def get_oauth_provider_info_list() -> list[OAuthProviderInfo]:
    """Deprecated: Use get_oauth_providers() instead."""
    return [OAuthProviderInfo(id=p.id, name=p.name, available=True) for p in _oauth_provider_registry.values()]


async def refresh_oauth_token(
    provider_id: str,
    credentials: OAuthCredentials,
) -> OAuthCredentials:
    """Refresh token for any OAuth provider.

    Deprecated: Use get_oauth_provider(id).refresh_token() instead.
    """
    provider = get_oauth_provider(provider_id)
    if not provider:
        raise RuntimeError(f"Unknown OAuth provider: {provider_id}")
    return await provider.refresh_token(credentials)  # type: ignore[no-any-return]


async def get_oauth_api_key(
    provider_id: str,
    credentials: dict[str, OAuthCredentials],
) -> tuple[OAuthCredentials, str] | None:
    """Get API key for a provider from OAuth credentials.

    Automatically refreshes expired tokens.
    Returns (new_credentials, api_key) or None if no credentials.
    """
    provider = get_oauth_provider(provider_id)
    if not provider:
        raise RuntimeError(f"Unknown OAuth provider: {provider_id}")

    creds = credentials.get(provider_id)
    if not creds:
        return None

    # Refresh if expired
    if int(time.time() * 1000) >= creds.expires:
        try:
            creds = await provider.refresh_token(creds)
        except Exception:
            raise RuntimeError(f"Failed to refresh OAuth token for {provider_id}") from None

    api_key: str = provider.get_api_key(creds)
    return creds, api_key
