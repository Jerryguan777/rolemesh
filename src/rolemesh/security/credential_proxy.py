"""HTTP credential proxy for container authentication.

Containers connect here instead of directly to LLM APIs.
The proxy injects real credentials so containers never see them.

Routes:
  /proxy/{provider}/{path}  — Multi-provider proxy (Anthropic, OpenAI, Google, etc.)
  /mcp-proxy/{name}/{path}  — MCP server proxy with per-user token injection
  /{path}                   — Legacy Anthropic-only proxy (Claude backend compat)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlparse

from aiohttp import ClientSession, web

from rolemesh.core.env import read_env_file
from rolemesh.core.logger import get_logger

if TYPE_CHECKING:
    from rolemesh.auth.token_vault import TokenVault

logger = get_logger()

# Module-level TokenVault for per-user IdP token forwarding to MCP servers
_token_vault: TokenVault | None = None

# The only identity header containers send.
_USER_ID_HEADER = "X-RoleMesh-User-Id"


def set_token_vault(vault: TokenVault) -> None:
    """Set the TokenVault instance for per-user MCP token forwarding."""
    global _token_vault
    _token_vault = vault
    logger.info("TokenVault configured for MCP user token forwarding")


AuthMode = Literal["api-key", "oauth"]

# Hop-by-hop headers that must not be forwarded by proxies
_HOP_BY_HOP = frozenset({"connection", "keep-alive", "transfer-encoding"})

# Response headers to strip (encoding is handled by aiohttp auto-decompression)
_SKIP_RESPONSE_HEADERS = frozenset(
    {"content-encoding", "transfer-encoding", "content-length", "connection", "keep-alive"}
)


# ---------------------------------------------------------------------------
# Multi-provider registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ProviderConfig:
    """Configuration for proxying requests to an LLM provider."""

    upstream: str  # e.g. "https://api.openai.com"
    secret_key: str  # the real API key (read at startup)
    header_name: str  # which header to inject: "authorization" or "x-api-key"
    header_format: str  # "Bearer {key}" or "{key}"


# Populated at startup by _build_provider_registry()
_provider_registry: dict[str, _ProviderConfig] = {}


def _build_provider_registry(secrets: dict[str, str], anthropic_auth_mode: AuthMode) -> dict[str, _ProviderConfig]:
    """Build provider configs from host secrets."""
    registry: dict[str, _ProviderConfig] = {}

    # Anthropic — reuse existing auth mode logic
    if anthropic_auth_mode == "api-key":
        ak = secrets.get("ANTHROPIC_API_KEY", "")
        if ak:
            registry["anthropic"] = _ProviderConfig(
                upstream=secrets.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
                secret_key=ak,
                header_name="x-api-key",
                header_format="{key}",
            )
    else:
        oauth = secrets.get("CLAUDE_CODE_OAUTH_TOKEN") or secrets.get("ANTHROPIC_AUTH_TOKEN", "")
        if oauth:
            registry["anthropic"] = _ProviderConfig(
                upstream=secrets.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
                secret_key=oauth,
                header_name="authorization",
                header_format="Bearer {key}",
            )

    # OpenAI (upstream includes /v1 since SDK appends /responses, /chat/completions etc.)
    openai_key = secrets.get("PI_OPENAI_API_KEY", "")
    if openai_key:
        registry["openai"] = _ProviderConfig(
            upstream="https://api.openai.com/v1",
            secret_key=openai_key,
            header_name="authorization",
            header_format="Bearer {key}",
        )

    # Google Generative AI
    google_key = secrets.get("PI_GOOGLE_API_KEY", "")
    if google_key:
        registry["google"] = _ProviderConfig(
            upstream="https://generativelanguage.googleapis.com",
            secret_key=google_key,
            header_name="x-goog-api-key",
            header_format="{key}",
        )

    return registry


# ---------------------------------------------------------------------------
# MCP server registry
# ---------------------------------------------------------------------------

_mcp_registry: dict[str, tuple[str, dict[str, str], str]] = {}


def register_mcp_server(
    name: str,
    url: str,
    headers: dict[str, str] | None = None,
    auth_mode: str = "user",
) -> None:
    """Register an MCP server for proxy forwarding."""
    _mcp_registry[name] = (url, headers or {}, auth_mode)
    logger.info("MCP server registered", name=name, url=url, auth_mode=auth_mode)


def get_mcp_registry() -> dict[str, tuple[str, dict[str, str], str]]:
    """Get the current MCP server registry."""
    return dict(_mcp_registry)


# ---------------------------------------------------------------------------
# Main proxy server
# ---------------------------------------------------------------------------


async def start_credential_proxy(port: int, host: str = "127.0.0.1") -> web.AppRunner:
    """Start the credential proxy HTTP server."""
    secrets = read_env_file(
        [
            "ANTHROPIC_API_KEY",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "PI_OPENAI_API_KEY",
            "PI_GOOGLE_API_KEY",
        ]
    )

    auth_mode: AuthMode = "api-key" if secrets.get("ANTHROPIC_API_KEY") else "oauth"
    oauth_token = secrets.get("CLAUDE_CODE_OAUTH_TOKEN") or secrets.get("ANTHROPIC_AUTH_TOKEN", "")

    # Legacy Anthropic upstream (for /{path} backward compat)
    upstream_url = secrets.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    parsed = urlparse(upstream_url)
    is_https = parsed.scheme == "https"
    upstream_host = parsed.hostname or "api.anthropic.com"
    upstream_port = parsed.port or (443 if is_https else 80)
    upstream_scheme = parsed.scheme or "https"

    # Build multi-provider registry
    global _provider_registry
    _provider_registry = _build_provider_registry(secrets, auth_mode)
    for name in _provider_registry:
        logger.info("LLM provider proxy registered", provider=name)

    session = ClientSession()

    # -- Shared streaming helper --

    async def _stream_upstream(
        request: web.Request,
        method: str,
        target_url: str,
        headers: dict[str, str],
        body: bytes,
    ) -> web.StreamResponse:
        async with session.request(
            method, target_url, headers=headers, data=body or None, allow_redirects=False,
        ) as upstream_resp:
            resp_headers = {k: v for k, v in upstream_resp.headers.items() if k.lower() not in _SKIP_RESPONSE_HEADERS}
            response = web.StreamResponse(status=upstream_resp.status, headers=resp_headers)
            await response.prepare(request)
            async for chunk in upstream_resp.content.iter_any():
                await response.write(chunk)
            await response.write_eof()
            return response

    # -- Shared header builder --

    def _forward_headers(request: web.Request) -> dict[str, str]:
        headers: dict[str, str] = {}
        for key, value in request.headers.items():
            if key.lower() not in _HOP_BY_HOP:
                headers[key] = value
        headers.pop("host", None)
        headers.pop("Host", None)
        return headers

    # -- Multi-provider proxy handler --

    async def handle_provider_proxy(request: web.Request) -> web.StreamResponse:
        """Forward LLM requests to the correct provider with credential injection."""
        provider_name = request.match_info["provider_name"]
        remaining_path = "/" + request.match_info.get("path_info", "")
        if request.query_string:
            remaining_path += "?" + request.query_string

        config = _provider_registry.get(provider_name)
        if not config:
            return web.Response(status=404, text=f"LLM provider not configured: {provider_name}")

        target_url = f"{config.upstream}{remaining_path}"

        fwd_headers = _forward_headers(request)
        body = await request.read()
        fwd_headers["content-length"] = str(len(body))

        # Set host header for upstream
        up = urlparse(config.upstream)
        up_host = up.hostname or ""
        up_port = up.port
        if up_port and up_port not in (80, 443):
            fwd_headers["host"] = f"{up_host}:{up_port}"
        else:
            fwd_headers["host"] = up_host

        # Inject real credential — strip any placeholder first
        for k in list(fwd_headers):
            if k.lower() == config.header_name:
                del fwd_headers[k]
        fwd_headers[config.header_name] = config.header_format.format(key=config.secret_key)

        # Strip identity headers
        fwd_headers.pop(_USER_ID_HEADER, None)

        try:
            return await _stream_upstream(request, request.method, target_url, fwd_headers, body)
        except ConnectionResetError:
            logger.debug("Provider proxy: client disconnected", provider=provider_name)
            return web.Response(status=499, text="Client Disconnected")
        except (OSError, RuntimeError, ValueError) as exc:
            logger.error("Provider proxy upstream error", provider=provider_name, error=str(exc))
            return web.Response(status=502, text="Bad Gateway")

    # -- MCP proxy handler --

    async def handle_mcp_proxy(request: web.Request) -> web.StreamResponse:
        server_name = request.match_info["server_name"]
        remaining_path = "/" + request.match_info.get("path_info", "")
        if request.query_string:
            remaining_path += "?" + request.query_string

        entry = _mcp_registry.get(server_name)
        if not entry:
            return web.Response(status=404, text=f"MCP server not found: {server_name}")

        origin, server_headers, mcp_auth_mode = entry
        target_url = f"{origin}{remaining_path}"

        fwd_headers = _forward_headers(request)
        fwd_headers.update(server_headers)

        user_id = request.headers.get(_USER_ID_HEADER)
        if mcp_auth_mode in ("user", "both") and user_id and _token_vault is not None:
            access_token = await _token_vault.get_fresh_access_token(user_id)
            if access_token:
                if mcp_auth_mode == "user":
                    fwd_headers["Authorization"] = f"Bearer {access_token}"
                else:
                    fwd_headers["X-User-Authorization"] = f"Bearer {access_token}"

        fwd_headers.pop(_USER_ID_HEADER, None)
        body = await request.read()

        try:
            return await _stream_upstream(request, request.method, target_url, fwd_headers, body)
        except (ConnectionResetError, OSError, RuntimeError, ValueError) as exc:
            logger.error("MCP proxy upstream error", server=server_name, error=str(exc))
            return web.Response(status=502, text="MCP proxy: Bad Gateway")

    # -- Legacy Anthropic handler (Claude backend backward compat) --

    async def handle_legacy_anthropic(request: web.Request) -> web.StreamResponse:
        body = await request.read()

        headers: dict[str, str] = {}
        for key, value in request.headers.items():
            if key.lower() not in _HOP_BY_HOP:
                headers[key] = value
        headers["host"] = f"{upstream_host}:{upstream_port}" if upstream_port not in (80, 443) else upstream_host
        headers["content-length"] = str(len(body))

        if auth_mode == "api-key":
            headers.pop("x-api-key", None)
            headers["x-api-key"] = secrets.get("ANTHROPIC_API_KEY", "")
        else:
            if "authorization" in {k.lower() for k in headers}:
                for k in list(headers):
                    if k.lower() == "authorization":
                        del headers[k]
                if oauth_token:
                    headers["authorization"] = f"Bearer {oauth_token}"

        target_url = f"{upstream_scheme}://{upstream_host}:{upstream_port}{request.path_qs}"

        try:
            return await _stream_upstream(request, request.method, target_url, headers, body)
        except ConnectionResetError:
            logger.debug("Credential proxy: client disconnected", url=str(request.url))
            return web.Response(status=499, text="Client Disconnected")
        except (OSError, RuntimeError, ValueError) as exc:
            logger.error("Credential proxy upstream error", url=str(request.url), error=str(exc))
            return web.Response(status=502, text="Bad Gateway")

    # -- Route registration (order matters: specific before wildcard) --

    app = web.Application()
    app.router.add_route("*", "/proxy/{provider_name}/{path_info:.*}", handle_provider_proxy)
    app.router.add_route("*", "/mcp-proxy/{server_name}/{path_info:.*}", handle_mcp_proxy)
    app.router.add_route("*", "/{path_info:.*}", handle_legacy_anthropic)

    app["client_session"] = session

    async def on_cleanup(app: web.Application) -> None:
        await app["client_session"].close()

    app.on_cleanup.append(on_cleanup)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()

    logger.info("Credential proxy started", port=port, host=host, auth_mode=auth_mode,
                providers=list(_provider_registry.keys()))
    return runner


def detect_auth_mode() -> AuthMode:
    """Detect which auth mode the host is configured for."""
    secrets = read_env_file(["ANTHROPIC_API_KEY"])
    return "api-key" if secrets.get("ANTHROPIC_API_KEY") else "oauth"
