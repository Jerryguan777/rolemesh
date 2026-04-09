"""OIDC PKCE endpoints for browser-based login flow.

Mounted only when AUTH_MODE=oidc. Provides:
  GET  /api/auth/config        — Frontend reads OIDC config to start login
  POST /api/auth/exchange      — Backend exchanges authorization code for tokens
  POST /api/auth/refresh       — Backend uses httpOnly cookie to refresh id_token
  POST /api/auth/logout        — Clears the refresh cookie
  GET  /oauth2/callback        — Minimal HTML page that captures code+state
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from rolemesh.core.logger import get_logger
from webui import auth
from webui.config import (
    OIDC_AUDIENCE,
    OIDC_CLIENT_ID,
    OIDC_CLIENT_SECRET,
    OIDC_COOKIE_SAMESITE,
    OIDC_COOKIE_SECURE,
    OIDC_REDIRECT_URI,
    OIDC_REFRESH_COOKIE_TTL,
    OIDC_SCOPES,
)

if TYPE_CHECKING:
    from rolemesh.auth.oidc.provider import OIDCAuthProvider

logger = get_logger()
router = APIRouter(tags=["auth"])

REFRESH_COOKIE_NAME = "rm_refresh"
REFRESH_COOKIE_PATH = "/api/auth"

# Module-level TokenVault, set by main.py at startup
_token_vault = None  # type: ignore[var-annotated]


def set_token_vault(vault) -> None:  # type: ignore[no-untyped-def]
    """Inject the TokenVault used to mirror IdP tokens into server-side storage."""
    global _token_vault
    _token_vault = vault


def _get_oidc_provider() -> OIDCAuthProvider:
    """Return the active OIDCAuthProvider, raising if not configured."""
    from rolemesh.auth.oidc.provider import OIDCAuthProvider

    provider = auth.get_provider()
    if not isinstance(provider, OIDCAuthProvider):
        raise HTTPException(status_code=500, detail="OIDC provider not configured")
    return provider


def _set_refresh_cookie(response: Response, refresh_token: str, max_age: int | None = None) -> None:
    """Set the httpOnly refresh_token cookie with secure defaults."""
    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=refresh_token,
        max_age=max_age or OIDC_REFRESH_COOKIE_TTL,
        path=REFRESH_COOKIE_PATH,
        httponly=True,
        secure=OIDC_COOKIE_SECURE,
        samesite=OIDC_COOKIE_SAMESITE,  # type: ignore[arg-type]
    )


def _clear_refresh_cookie(response: Response) -> None:
    """Remove the refresh_token cookie. Match attributes of the original cookie."""
    response.delete_cookie(
        key=REFRESH_COOKIE_NAME,
        path=REFRESH_COOKIE_PATH,
        secure=OIDC_COOKIE_SECURE,
        samesite=OIDC_COOKIE_SAMESITE,  # type: ignore[arg-type]
        httponly=True,
    )


def _user_payload(user) -> dict[str, str | None]:
    return {
        "id": user.user_id,
        "tenant_id": user.tenant_id,
        "name": user.name,
        "email": user.email,
        "role": user.role,
    }


async def _post_token_endpoint(token_endpoint: str, data: dict[str, str]) -> tuple[int, dict]:
    """POST to the IdP's token endpoint. Returns (status_code, json_payload).

    Raises 502 if the endpoint is unreachable.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                token_endpoint,
                data=data,
                headers={"Accept": "application/json"},
            )
    except httpx.HTTPError as exc:
        logger.error("OIDC token endpoint network error", error=str(exc))
        raise HTTPException(status_code=502, detail="Token endpoint unreachable") from exc
    if resp.status_code != 200:
        return resp.status_code, {"_raw_text": resp.text[:200]}
    return 200, resp.json()


@router.get("/api/auth/config")
async def get_auth_config() -> JSONResponse:
    """Return OIDC configuration for the frontend login flow."""
    provider = _get_oidc_provider()
    try:
        disc = await provider.get_discovery()
    except (httpx.HTTPError, KeyError) as exc:
        logger.error("OIDC discovery failed", error=str(exc))
        raise HTTPException(status_code=503, detail="OIDC discovery unavailable") from exc

    return JSONResponse(
        {
            "provider": "oidc",
            "issuer": disc.issuer,
            "authorization_endpoint": disc.authorization_endpoint,
            "client_id": OIDC_CLIENT_ID,
            "redirect_uri": OIDC_REDIRECT_URI,
            "scope": OIDC_SCOPES,
            "audience": OIDC_AUDIENCE or OIDC_CLIENT_ID,
        }
    )


class CodeExchangeRequest(BaseModel):
    code: str
    code_verifier: str
    redirect_uri: str | None = None


@router.post("/api/auth/exchange")
async def exchange_code(body: CodeExchangeRequest) -> Response:
    """Exchange an authorization code for tokens (backend-to-backend).

    Validates the returned id_token via OIDCAuthProvider, JIT-provisions
    the tenant/user, and stores the refresh_token in an httpOnly cookie.
    """
    provider = _get_oidc_provider()
    try:
        disc = await provider.get_discovery()
    except (httpx.HTTPError, KeyError) as exc:
        raise HTTPException(status_code=503, detail="OIDC discovery unavailable") from exc

    redirect_uri = body.redirect_uri or OIDC_REDIRECT_URI
    if not redirect_uri:
        raise HTTPException(status_code=400, detail="redirect_uri is required")

    token_request: dict[str, str] = {
        "grant_type": "authorization_code",
        "code": body.code,
        "redirect_uri": redirect_uri,
        "client_id": OIDC_CLIENT_ID,
        "code_verifier": body.code_verifier,
    }
    if OIDC_CLIENT_SECRET:
        token_request["client_secret"] = OIDC_CLIENT_SECRET

    status, payload = await _post_token_endpoint(disc.token_endpoint, token_request)
    if status != 200:
        logger.warning("OIDC token exchange rejected", status=status)
        raise HTTPException(status_code=400, detail=f"Token exchange failed: {payload.get('_raw_text', '')}")

    id_token = payload.get("id_token")
    if not id_token:
        raise HTTPException(status_code=400, detail="No id_token in response")

    user = await provider.authenticate(id_token)
    if user is None:
        raise HTTPException(status_code=401, detail="id_token validation failed")

    response = JSONResponse(
        {
            "id_token": id_token,
            "expires_in": payload.get("expires_in"),
            "user": _user_payload(user),
        }
    )
    refresh_token = payload.get("refresh_token")
    if refresh_token:
        _set_refresh_cookie(response, refresh_token)
        # Mirror tokens to server-side vault for MCP forwarding
        if _token_vault is not None:
            await _token_vault.store_initial(
                user_id=user.user_id,
                refresh_token=refresh_token,
                access_token=payload.get("access_token"),
                expires_in=payload.get("expires_in"),
            )
    elif _token_vault is not None:
        # Vault is configured but IdP did not return a refresh_token. This
        # almost always means the OIDC client is missing the 'offline_access'
        # scope. MCP token forwarding will not work for this user.
        logger.warning(
            "OIDC exchange returned no refresh_token; vault not populated. "
            "Ensure the OIDC client requests 'offline_access' scope.",
            user_id=user.user_id,
        )
    return response


@router.post("/api/auth/refresh")
async def refresh_token_endpoint(request: Request) -> Response:
    """Refresh the id_token using the refresh_token stored in httpOnly cookie."""
    refresh_token = request.cookies.get(REFRESH_COOKIE_NAME)
    if not refresh_token:
        raise HTTPException(status_code=401, detail="No refresh token")

    provider = _get_oidc_provider()
    try:
        disc = await provider.get_discovery()
    except (httpx.HTTPError, KeyError) as exc:
        raise HTTPException(status_code=503, detail="OIDC discovery unavailable") from exc

    token_request: dict[str, str] = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": OIDC_CLIENT_ID,
    }
    if OIDC_CLIENT_SECRET:
        token_request["client_secret"] = OIDC_CLIENT_SECRET

    status, payload = await _post_token_endpoint(disc.token_endpoint, token_request)
    if status != 200:
        # IdP rejected refresh_token (revoked, expired, etc.) — clear cookie
        logger.warning("OIDC refresh rejected", status=status)
        response = JSONResponse({"error": "Refresh failed"}, status_code=401)
        _clear_refresh_cookie(response)
        return response

    new_id_token = payload.get("id_token")
    if not new_id_token:
        raise HTTPException(status_code=400, detail="No id_token in refresh response")

    user = await provider.authenticate(new_id_token)
    if user is None:
        response = JSONResponse({"error": "New id_token invalid"}, status_code=401)
        _clear_refresh_cookie(response)
        return response

    response = JSONResponse(
        {
            "id_token": new_id_token,
            "expires_in": payload.get("expires_in"),
            "user": _user_payload(user),
        }
    )

    # Rotation: if IdP returned a new refresh_token, replace the cookie
    new_refresh = payload.get("refresh_token")
    if new_refresh and new_refresh != refresh_token:
        _set_refresh_cookie(response, new_refresh)

    # Mirror updated tokens to server-side vault
    if _token_vault is not None:
        await _token_vault.store_initial(
            user_id=user.user_id,
            refresh_token=new_refresh or refresh_token,
            access_token=payload.get("access_token"),
            expires_in=payload.get("expires_in"),
        )
    return response


@router.post("/api/auth/logout")
async def logout(request: Request) -> Response:
    """Clear the refresh_token cookie and revoke server-side token vault.

    Requires a valid id_token in the Authorization header to identify the
    user. This prevents CSRF-style logout attacks: even though SameSite=lax
    cookies travel with cross-site POST, the attacker cannot read the user's
    id_token from sessionStorage to forge the Authorization header.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    id_token = auth_header[7:]

    provider = _get_oidc_provider()
    user = await provider.authenticate(id_token)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid id_token")

    response = JSONResponse({"ok": True})
    _clear_refresh_cookie(response)
    if _token_vault is not None:
        await _token_vault.revoke(user.user_id)
    return response


@router.get("/oauth2/callback")
async def oauth_callback() -> HTMLResponse:
    """Serve a minimal HTML page that hands code+state back to the SPA.

    The frontend SPA at the root URL handles the actual code exchange via
    /api/auth/exchange. This page just bridges the IdP redirect.
    """
    html = """<!doctype html>
<html><head><meta charset="utf-8"><title>Signing in...</title></head>
<body>
<p>Signing you in...</p>
<script>
(function () {
    const params = new URLSearchParams(location.search);
    const code = params.get('code');
    const state = params.get('state');
    const error = params.get('error');
    if (error) {
        sessionStorage.setItem('oidc_error', error);
    } else if (code) {
        sessionStorage.setItem('oidc_code', code);
        if (state) sessionStorage.setItem('oidc_state', state);
    }
    location.replace('/');
})();
</script>
</body></html>"""
    return HTMLResponse(html)
