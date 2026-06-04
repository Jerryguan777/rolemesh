"""Anthropic OAuth flow (Claude Pro/Max).

Ported from packages/ai/src/utils/oauth/anthropic.ts.
"""

from __future__ import annotations

import base64
import time

import httpx

from pi.ai.oauth.pkce import generate_pkce
from pi.ai.oauth.types import OAuthAuthInfo, OAuthCredentials, OAuthLoginCallbacks
from pi.ai.types import Model

_CLIENT_ID = base64.b64decode("OWQxYzI1MGEtZTYxYi00NGQ5LTg4ZWQtNTk0NGQxOTYyZjVl").decode()
_AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
_TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
_REDIRECT_URI = "https://console.anthropic.com/oauth/code/callback"
_SCOPES = "org:create_api_key user:profile user:inference"


async def login_anthropic(
    on_auth_url: object,  # Callable[[str], None]
    on_prompt_code: object,  # Callable[[], Awaitable[str]]
) -> OAuthCredentials:
    """Login with Anthropic OAuth (manual code paste flow)."""
    from collections.abc import Callable, Coroutine
    from typing import Any

    _on_auth_url: Callable[[str], None] = on_auth_url  # type: ignore[assignment]
    _on_prompt_code: Callable[[], Coroutine[Any, Any, str]] = on_prompt_code  # type: ignore[assignment]

    verifier, challenge = await generate_pkce()

    params = {
        "code": "true",
        "client_id": _CLIENT_ID,
        "response_type": "code",
        "redirect_uri": _REDIRECT_URI,
        "scope": _SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": verifier,
    }
    query = "&".join(f"{k}={v}" for k, v in params.items())
    auth_url = f"{_AUTHORIZE_URL}?{query}"

    _on_auth_url(auth_url)

    # Wait for user to paste authorization code (format: code#state)
    auth_code = await _on_prompt_code()
    splits = auth_code.split("#")
    code = splits[0]
    state = splits[1] if len(splits) > 1 else ""

    # Exchange code for tokens
    async with httpx.AsyncClient() as client:
        response = await client.post(
            _TOKEN_URL,
            json={
                "grant_type": "authorization_code",
                "client_id": _CLIENT_ID,
                "code": code,
                "state": state,
                "redirect_uri": _REDIRECT_URI,
                "code_verifier": verifier,
            },
            headers={"Content-Type": "application/json"},
        )

    if response.status_code != 200:
        raise RuntimeError(f"Token exchange failed: {response.text}")

    token_data = response.json()
    expires_at = int(time.time() * 1000) + token_data["expires_in"] * 1000 - 5 * 60 * 1000

    return OAuthCredentials(
        refresh=token_data["refresh_token"],
        access=token_data["access_token"],
        expires=expires_at,
    )


async def refresh_anthropic_token(refresh_token: str) -> OAuthCredentials:
    """Refresh Anthropic OAuth token."""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            _TOKEN_URL,
            json={
                "grant_type": "refresh_token",
                "client_id": _CLIENT_ID,
                "refresh_token": refresh_token,
            },
            headers={"Content-Type": "application/json"},
        )

    if response.status_code != 200:
        raise RuntimeError(f"Anthropic token refresh failed: {response.text}")

    data = response.json()
    return OAuthCredentials(
        refresh=data["refresh_token"],
        access=data["access_token"],
        expires=int(time.time() * 1000) + data["expires_in"] * 1000 - 5 * 60 * 1000,
    )


class AnthropicOAuthProvider:
    """Anthropic OAuth provider (Claude Pro/Max)."""

    @property
    def id(self) -> str:
        return "anthropic"

    @property
    def name(self) -> str:
        return "Anthropic (Claude Pro/Max)"

    @property
    def uses_callback_server(self) -> bool:
        return False

    async def login(self, callbacks: OAuthLoginCallbacks) -> OAuthCredentials:
        return await login_anthropic(
            lambda url: callbacks.on_auth(OAuthAuthInfo(url=url)),
            lambda: callbacks.on_prompt({"message": "Paste the authorization code:"}),
        )

    async def refresh_token(self, credentials: OAuthCredentials) -> OAuthCredentials:
        return await refresh_anthropic_token(credentials.refresh)

    def get_api_key(self, credentials: OAuthCredentials) -> str:
        return credentials.access

    def modify_models(self, models: list[Model], credentials: OAuthCredentials) -> list[Model]:
        return models


anthropic_oauth_provider = AnthropicOAuthProvider()
