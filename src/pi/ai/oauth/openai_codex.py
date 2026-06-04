"""OpenAI Codex (ChatGPT OAuth) flow.

Ported from packages/ai/src/utils/oauth/openai-codex.ts.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import secrets
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from pi.ai.oauth.pkce import generate_pkce
from pi.ai.oauth.types import OAuthAuthInfo, OAuthCredentials, OAuthLoginCallbacks, OAuthPrompt
from pi.ai.types import Model

_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
_AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
_TOKEN_URL = "https://auth.openai.com/oauth/token"
_REDIRECT_URI = "http://localhost:1455/auth/callback"
_SCOPE = "openid profile email offline_access"
_JWT_CLAIM_PATH = "https://api.openai.com/auth"


def _create_state() -> str:
    return secrets.token_hex(16)


def _parse_authorization_input(input_: str) -> dict[str, str | None]:
    value = input_.strip()
    if not value:
        return {}

    try:
        parsed = urlparse(value)
        if parsed.scheme:
            qs = parse_qs(parsed.query)
            return {
                "code": qs.get("code", [None])[0],
                "state": qs.get("state", [None])[0],
            }
    except Exception:
        pass

    if "#" in value:
        parts = value.split("#", 1)
        return {"code": parts[0], "state": parts[1] if len(parts) > 1 else None}

    if "code=" in value:
        qs = parse_qs(value)
        return {
            "code": qs.get("code", [None])[0],
            "state": qs.get("state", [None])[0],
        }

    return {"code": value}


def _decode_jwt(token: str) -> dict[str, Any] | None:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload = parts[1]
        # Add padding
        padding = 4 - len(payload) % 4
        if padding != 4:
            payload += "=" * padding
        decoded = base64.b64decode(payload).decode("utf-8")
        return json.loads(decoded)  # type: ignore[no-any-return]
    except Exception:
        return None


def _get_account_id(access_token: str) -> str | None:
    payload = _decode_jwt(access_token)
    if not payload:
        return None
    auth = payload.get(_JWT_CLAIM_PATH)
    if not isinstance(auth, dict):
        return None
    account_id = auth.get("chatgpt_account_id")
    return account_id if isinstance(account_id, str) and account_id else None


async def _exchange_authorization_code(
    code: str,
    verifier: str,
    redirect_uri: str = _REDIRECT_URI,
) -> dict[str, Any] | None:
    async with httpx.AsyncClient() as client:
        response = await client.post(
            _TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "client_id": _CLIENT_ID,
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": redirect_uri,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    if response.status_code != 200:
        return None

    data = response.json()
    if not data.get("access_token") or not data.get("refresh_token") or not isinstance(data.get("expires_in"), int):
        return None

    return {
        "access": data["access_token"],
        "refresh": data["refresh_token"],
        "expires": int(time.time() * 1000) + data["expires_in"] * 1000,
    }


async def _refresh_access_token(refresh_token: str) -> dict[str, Any] | None:
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                _TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": _CLIENT_ID,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

        if response.status_code != 200:
            return None

        data = response.json()
        if not data.get("access_token") or not data.get("refresh_token") or not isinstance(data.get("expires_in"), int):
            return None

        return {
            "access": data["access_token"],
            "refresh": data["refresh_token"],
            "expires": int(time.time() * 1000) + data["expires_in"] * 1000,
        }
    except Exception:
        return None


async def _start_local_oauth_server(state: str) -> tuple[asyncio.AbstractServer | None, asyncio.Future[str | None]]:
    """Start local server for OAuth callback. Returns (server, code_future)."""
    code_future: asyncio.Future[str | None] = asyncio.get_event_loop().create_future()

    async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await reader.readline()
            request_str = request_line.decode("utf-8", errors="replace")
            parts = request_str.split(" ")
            if len(parts) < 2:
                writer.close()
                return

            path = parts[1]
            parsed = urlparse(path)

            if parsed.path != "/auth/callback":
                writer.write(b"HTTP/1.1 404 Not Found\r\n\r\nNot found")
                await writer.drain()
                writer.close()
                return

            qs = parse_qs(parsed.query)
            req_state = qs.get("state", [None])[0]
            code = qs.get("code", [None])[0]

            if req_state != state:
                writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\nState mismatch")
                await writer.drain()
                writer.close()
                return

            if not code:
                writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\nMissing authorization code")
                await writer.drain()
                writer.close()
                return

            body = "<p>Authentication successful. Return to your terminal to continue.</p>"
            response = f"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n\r\n{body}"
            writer.write(response.encode())
            await writer.drain()
            writer.close()

            if not code_future.done():
                code_future.set_result(code)
        except Exception:
            with contextlib.suppress(Exception):
                writer.close()

    try:
        server = await asyncio.start_server(handle_client, "127.0.0.1", 1455)
        return server, code_future
    except OSError:
        # Port in use - fall back to manual paste
        if not code_future.done():
            code_future.set_result(None)
        return None, code_future


async def login_openai_codex(
    on_auth: Any,
    on_prompt: Any,
    on_progress: Any = None,
    on_manual_code_input: Any = None,
    originator: str = "pi",
) -> OAuthCredentials:
    """Login with OpenAI Codex OAuth."""
    verifier, challenge = await generate_pkce()
    state = _create_state()

    auth_url = (
        f"{_AUTHORIZE_URL}?"
        f"response_type=code&client_id={_CLIENT_ID}&redirect_uri={_REDIRECT_URI}"
        f"&scope={_SCOPE}&code_challenge={challenge}&code_challenge_method=S256"
        f"&state={state}&id_token_add_organizations=true"
        f"&codex_cli_simplified_flow=true&originator={originator}"
    )

    server, code_future = await _start_local_oauth_server(state)
    on_auth(OAuthAuthInfo(url=auth_url, instructions="A browser window should open. Complete login to finish."))

    code: str | None = None
    try:
        if on_manual_code_input and server:
            manual_task = asyncio.create_task(on_manual_code_input())
            try:
                done, pending = await asyncio.wait(
                    [code_future, manual_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()

                for task in done:
                    result = task.result()
                    if isinstance(result, str) and result:
                        if len(result) > 20:
                            # Likely a URL or code#state - parse it
                            parsed = _parse_authorization_input(result)
                            if parsed.get("state") and parsed["state"] != state:
                                raise RuntimeError("State mismatch")
                            code = parsed.get("code")
                        else:
                            code = result
            except asyncio.CancelledError:
                pass
        elif server:
            result = await code_future
            code = result
        else:
            # No server - use manual prompt
            pass

        # Fallback to on_prompt if still no code
        if not code:
            input_ = await on_prompt(OAuthPrompt(message="Paste the authorization code (or full redirect URL):"))
            parsed = _parse_authorization_input(input_)
            if parsed.get("state") and parsed["state"] != state:
                raise RuntimeError("State mismatch")
            code = parsed.get("code")

        if not code:
            raise RuntimeError("Missing authorization code")

        token_result = await _exchange_authorization_code(code, verifier)
        if not token_result:
            raise RuntimeError("Token exchange failed")

        account_id = _get_account_id(token_result["access"])
        if not account_id:
            raise RuntimeError("Failed to extract accountId from token")

        return OAuthCredentials(
            access=token_result["access"],
            refresh=token_result["refresh"],
            expires=token_result["expires"],
            extra={"account_id": account_id},
        )
    finally:
        if server:
            server.close()
            await server.wait_closed()


async def refresh_openai_codex_token(refresh_token: str) -> OAuthCredentials:
    """Refresh OpenAI Codex OAuth token."""
    result = await _refresh_access_token(refresh_token)
    if not result:
        raise RuntimeError("Failed to refresh OpenAI Codex token")

    account_id = _get_account_id(result["access"])
    if not account_id:
        raise RuntimeError("Failed to extract accountId from token")

    return OAuthCredentials(
        access=result["access"],
        refresh=result["refresh"],
        expires=result["expires"],
        extra={"account_id": account_id},
    )


class OpenAICodexOAuthProvider:
    """ChatGPT Plus/Pro (Codex) OAuth provider."""

    @property
    def id(self) -> str:
        return "openai-codex"

    @property
    def name(self) -> str:
        return "ChatGPT Plus/Pro (Codex Subscription)"

    @property
    def uses_callback_server(self) -> bool:
        return True

    async def login(self, callbacks: OAuthLoginCallbacks) -> OAuthCredentials:
        return await login_openai_codex(
            on_auth=callbacks.on_auth,
            on_prompt=callbacks.on_prompt,
            on_progress=callbacks.on_progress,
            on_manual_code_input=callbacks.on_manual_code_input,
        )

    async def refresh_token(self, credentials: OAuthCredentials) -> OAuthCredentials:
        return await refresh_openai_codex_token(credentials.refresh)

    def get_api_key(self, credentials: OAuthCredentials) -> str:
        return credentials.access

    def modify_models(self, models: list[Model], credentials: OAuthCredentials) -> list[Model]:
        return models


openai_codex_oauth_provider = OpenAICodexOAuthProvider()
