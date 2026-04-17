"""GitHub Copilot OAuth flow (device code flow).

Ported from packages/ai/src/utils/oauth/github-copilot.ts.
"""

from __future__ import annotations

import asyncio
import base64
import re
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from pi.ai.oauth.types import OAuthAuthInfo, OAuthCredentials, OAuthLoginCallbacks
from pi.ai.types import Model

_CLIENT_ID = base64.b64decode("SXYxLmI1MDdhMDhjODdlY2ZlOTg=").decode()

_COPILOT_HEADERS = {
    "User-Agent": "GitHubCopilotChat/0.35.0",
    "Editor-Version": "vscode/1.107.0",
    "Editor-Plugin-Version": "copilot-chat/0.35.0",
    "Copilot-Integration-Id": "vscode-chat",
}


def normalize_domain(input_: str) -> str | None:
    """Normalize a GitHub Enterprise domain input to just the hostname."""
    trimmed = input_.strip()
    if not trimmed:
        return None
    try:
        url_str = trimmed if "://" in trimmed else f"https://{trimmed}"
        parsed = urlparse(url_str)
        return parsed.hostname
    except Exception:
        return None


def _get_urls(domain: str) -> dict[str, str]:
    return {
        "device_code_url": f"https://{domain}/login/device/code",
        "access_token_url": f"https://{domain}/login/oauth/access_token",
        "copilot_token_url": f"https://api.{domain}/copilot_internal/v2/token",
    }


def _get_base_url_from_token(token: str) -> str | None:
    """Parse proxy-ep from a Copilot token and convert to API base URL."""
    match = re.search(r"proxy-ep=([^;]+)", token)
    if not match:
        return None
    proxy_host = match.group(1)
    api_host = re.sub(r"^proxy\.", "api.", proxy_host)
    return f"https://{api_host}"


def get_github_copilot_base_url(token: str | None = None, enterprise_domain: str | None = None) -> str:
    """Get the GitHub Copilot API base URL."""
    if token:
        url_from_token = _get_base_url_from_token(token)
        if url_from_token:
            return url_from_token
    if enterprise_domain:
        return f"https://copilot-api.{enterprise_domain}"
    return "https://api.individual.githubcopilot.com"


async def _fetch_json(url: str, **kwargs: Any) -> Any:
    async with httpx.AsyncClient() as client:
        response = await client.request(**kwargs, url=url)
    if response.status_code >= 400:
        raise RuntimeError(f"{response.status_code} {response.reason_phrase}: {response.text}")
    return response.json()


async def _start_device_flow(domain: str) -> dict[str, Any]:
    urls = _get_urls(domain)
    data = await _fetch_json(
        urls["device_code_url"],
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "GitHubCopilotChat/0.35.0",
        },
        json={"client_id": _CLIENT_ID, "scope": "read:user"},
    )

    if not isinstance(data, dict):
        raise RuntimeError("Invalid device code response")

    for field in ("device_code", "user_code", "verification_uri", "interval", "expires_in"):
        if field not in data:
            raise RuntimeError(f"Invalid device code response: missing {field}")

    return data


async def _poll_for_github_access_token(
    domain: str,
    device_code: str,
    interval_seconds: int,
    expires_in: int,
    signal: asyncio.Event | None = None,
) -> str:
    urls = _get_urls(domain)
    deadline = time.time() + expires_in
    interval_ms = max(1000, interval_seconds * 1000)

    while time.time() < deadline:
        if signal and signal.is_set():
            raise RuntimeError("Login cancelled")

        raw = await _fetch_json(
            urls["access_token_url"],
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "GitHubCopilotChat/0.35.0",
            },
            json={
                "client_id": _CLIENT_ID,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
        )

        if isinstance(raw, dict) and isinstance(raw.get("access_token"), str):
            return raw["access_token"]  # type: ignore[no-any-return]

        if isinstance(raw, dict) and isinstance(raw.get("error"), str):
            err = raw["error"]
            if err == "authorization_pending":
                await asyncio.sleep(interval_ms / 1000)
                continue
            if err == "slow_down":
                interval_ms += 5000
                await asyncio.sleep(interval_ms / 1000)
                continue
            raise RuntimeError(f"Device flow failed: {err}")

        await asyncio.sleep(interval_ms / 1000)

    raise RuntimeError("Device flow timed out")


async def refresh_github_copilot_token(
    refresh_token: str,
    enterprise_domain: str | None = None,
) -> OAuthCredentials:
    """Refresh GitHub Copilot token."""
    domain = enterprise_domain or "github.com"
    urls = _get_urls(domain)

    raw = await _fetch_json(
        urls["copilot_token_url"],
        method="GET",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {refresh_token}",
            **_COPILOT_HEADERS,
        },
    )

    if not isinstance(raw, dict):
        raise RuntimeError("Invalid Copilot token response")

    token = raw.get("token")
    expires_at = raw.get("expires_at")

    if not isinstance(token, str) or not isinstance(expires_at, (int, float)):
        raise RuntimeError("Invalid Copilot token response fields")

    return OAuthCredentials(
        refresh=refresh_token,
        access=token,
        expires=int(expires_at * 1000 - 5 * 60 * 1000),
        extra={"enterprise_url": enterprise_domain} if enterprise_domain else {},
    )


async def login_github_copilot(
    on_auth: object,
    on_prompt: object,
    on_progress: object = None,
    signal: asyncio.Event | None = None,
) -> OAuthCredentials:
    """Login with GitHub Copilot OAuth (device code flow)."""
    from collections.abc import Callable, Coroutine

    _on_auth: Callable[[str, str | None], None] = on_auth  # type: ignore[assignment]
    _on_prompt: Callable[[dict[str, Any]], Coroutine[Any, Any, str]] = on_prompt  # type: ignore[assignment]
    _on_progress: Callable[[str], None] | None = on_progress  # type: ignore[assignment]

    input_ = await _on_prompt(
        {
            "message": "GitHub Enterprise URL/domain (blank for github.com)",
            "placeholder": "company.ghe.com",
            "allow_empty": True,
        }
    )

    if signal and signal.is_set():
        raise RuntimeError("Login cancelled")

    trimmed = input_.strip()
    enterprise_domain = normalize_domain(input_)
    if trimmed and not enterprise_domain:
        raise RuntimeError("Invalid GitHub Enterprise URL/domain")
    domain = enterprise_domain or "github.com"

    device = await _start_device_flow(domain)
    _on_auth(device["verification_uri"], f"Enter code: {device['user_code']}")

    github_access_token = await _poll_for_github_access_token(
        domain,
        device["device_code"],
        device["interval"],
        device["expires_in"],
        signal,
    )
    credentials = await refresh_github_copilot_token(github_access_token, enterprise_domain)

    if _on_progress:
        _on_progress("Enabling models...")

    return credentials


class GitHubCopilotOAuthProvider:
    """GitHub Copilot OAuth provider."""

    @property
    def id(self) -> str:
        return "github-copilot"

    @property
    def name(self) -> str:
        return "GitHub Copilot"

    @property
    def uses_callback_server(self) -> bool:
        return False

    async def login(self, callbacks: OAuthLoginCallbacks) -> OAuthCredentials:
        return await login_github_copilot(
            on_auth=lambda url, instructions=None: callbacks.on_auth(OAuthAuthInfo(url=url, instructions=instructions)),
            on_prompt=callbacks.on_prompt,
            on_progress=callbacks.on_progress,
            signal=callbacks.signal,
        )

    async def refresh_token(self, credentials: OAuthCredentials) -> OAuthCredentials:
        return await refresh_github_copilot_token(
            credentials.refresh,
            credentials.extra.get("enterprise_url"),
        )

    def get_api_key(self, credentials: OAuthCredentials) -> str:
        return credentials.access

    def modify_models(self, models: list[Model], credentials: OAuthCredentials) -> list[Model]:
        domain = credentials.extra.get("enterprise_url")
        if domain:
            domain = normalize_domain(domain)
        base_url = get_github_copilot_base_url(credentials.access, domain)
        result: list[Model] = []
        for m in models:
            if m.provider == "github-copilot":
                from dataclasses import replace

                result.append(replace(m, base_url=base_url))
            else:
                result.append(m)
        return result


github_copilot_oauth_provider = GitHubCopilotOAuthProvider()
