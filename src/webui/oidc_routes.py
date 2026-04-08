"""OIDC PKCE endpoints for browser-based login flow.

Mounted only when AUTH_MODE=oidc. Provides:
  GET  /api/auth/config        — Frontend reads OIDC config to start login
  POST /api/auth/exchange      — Backend exchanges authorization code for tokens
  GET  /oauth2/callback        — Minimal HTML page that captures code+state
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from rolemesh.core.logger import get_logger
from webui import auth
from webui.config import (
    OIDC_AUDIENCE,
    OIDC_CLIENT_ID,
    OIDC_CLIENT_SECRET,
    OIDC_REDIRECT_URI,
    OIDC_SCOPES,
)

if TYPE_CHECKING:
    from rolemesh.auth.oidc_provider import OIDCAuthProvider

logger = get_logger()
router = APIRouter(tags=["auth"])


def _get_oidc_provider() -> OIDCAuthProvider:
    """Return the active OIDCAuthProvider, raising if not configured."""
    from rolemesh.auth.oidc_provider import OIDCAuthProvider

    provider = auth.get_provider()
    if not isinstance(provider, OIDCAuthProvider):
        raise HTTPException(status_code=500, detail="OIDC provider not configured")
    return provider


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
async def exchange_code(body: CodeExchangeRequest) -> JSONResponse:
    """Exchange an authorization code for tokens (backend-to-backend).

    Validates the returned id_token via the configured OIDCAuthProvider
    and JIT-provisions the tenant/user.
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

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                disc.token_endpoint,
                data=token_request,
                headers={"Accept": "application/json"},
            )
    except httpx.HTTPError as exc:
        logger.error("OIDC token exchange network error", error=str(exc))
        raise HTTPException(status_code=502, detail="Token endpoint unreachable") from exc

    if resp.status_code != 200:
        logger.warning("OIDC token exchange rejected", status=resp.status_code, body=resp.text[:200])
        raise HTTPException(status_code=400, detail=f"Token exchange failed: {resp.text[:200]}")

    payload = resp.json()
    id_token = payload.get("id_token")
    if not id_token:
        raise HTTPException(status_code=400, detail="No id_token in response")

    # Validate id_token + JIT provision via the provider
    user = await provider.authenticate(id_token)
    if user is None:
        raise HTTPException(status_code=401, detail="id_token validation failed")

    return JSONResponse(
        {
            "id_token": id_token,
            "access_token": payload.get("access_token"),
            "expires_in": payload.get("expires_in"),
            "user": {
                "id": user.user_id,
                "tenant_id": user.tenant_id,
                "name": user.name,
                "email": user.email,
                "role": user.role,
            },
        }
    )


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
