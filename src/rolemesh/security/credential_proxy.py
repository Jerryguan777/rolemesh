"""HTTP credential proxy for container authentication.

Containers connect here instead of directly to the Anthropic API.
The proxy injects real credentials so containers never see them.

Two auth modes:
  API key:  Proxy injects x-api-key on every request.
  OAuth:    Container CLI exchanges its placeholder token for a temp
            API key via /api/oauth/claude_cli/create_api_key.
            Proxy injects real OAuth token on that exchange request;
            subsequent requests carry the temp key which is valid as-is.
"""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlparse

from aiohttp import ClientSession, web

from rolemesh.core.env import read_env_file
from rolemesh.core.logger import get_logger

logger = get_logger()

AuthMode = Literal["api-key", "oauth"]

# Hop-by-hop headers that must not be forwarded by proxies
_HOP_BY_HOP = frozenset({"connection", "keep-alive", "transfer-encoding"})

# Response headers to strip (encoding is handled by aiohttp auto-decompression)
_SKIP_RESPONSE_HEADERS = frozenset(
    {
        "content-encoding",
        "transfer-encoding",
        "content-length",
        "connection",
        "keep-alive",
    }
)

# Module-level MCP server registry: server_name → (origin URL, headers to inject)
_mcp_registry: dict[str, tuple[str, dict[str, str]]] = {}


def register_mcp_server(name: str, url: str, headers: dict[str, str] | None = None) -> None:
    """Register an MCP server for proxy forwarding."""
    _mcp_registry[name] = (url, headers or {})
    logger.info("MCP server registered", name=name, url=url)


def get_mcp_registry() -> dict[str, tuple[str, dict[str, str]]]:
    """Get the current MCP server registry."""
    return dict(_mcp_registry)


async def start_credential_proxy(port: int, host: str = "127.0.0.1") -> web.AppRunner:
    """Start the credential proxy HTTP server.

    Returns the AppRunner so the caller can shut it down later.
    """
    secrets = read_env_file(
        [
            "ANTHROPIC_API_KEY",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
        ]
    )

    auth_mode: AuthMode = "api-key" if secrets.get("ANTHROPIC_API_KEY") else "oauth"
    oauth_token = secrets.get("CLAUDE_CODE_OAUTH_TOKEN") or secrets.get("ANTHROPIC_AUTH_TOKEN", "")

    upstream_url = secrets.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    parsed = urlparse(upstream_url)
    is_https = parsed.scheme == "https"
    upstream_host = parsed.hostname or "api.anthropic.com"
    upstream_port = parsed.port or (443 if is_https else 80)
    upstream_scheme = parsed.scheme or "https"

    session = ClientSession()

    async def _stream_upstream(
        request: web.Request,
        method: str,
        target_url: str,
        headers: dict[str, str],
        body: bytes,
    ) -> web.StreamResponse:
        """Forward a request to an upstream server and stream the response back."""
        async with session.request(
            method,
            target_url,
            headers=headers,
            data=body or None,
            allow_redirects=False,
        ) as upstream_resp:
            resp_headers = {k: v for k, v in upstream_resp.headers.items() if k.lower() not in _SKIP_RESPONSE_HEADERS}
            response = web.StreamResponse(
                status=upstream_resp.status,
                headers=resp_headers,
            )
            await response.prepare(request)
            async for chunk in upstream_resp.content.iter_any():
                await response.write(chunk)
            await response.write_eof()
            return response

    async def handle_mcp_proxy(request: web.Request) -> web.StreamResponse:
        """Forward MCP requests to the actual MCP server with per-server header injection."""
        server_name = request.match_info["server_name"]
        remaining_path = "/" + request.match_info.get("path_info", "")
        if request.query_string:
            remaining_path += "?" + request.query_string

        entry = _mcp_registry.get(server_name)
        if not entry:
            return web.Response(status=404, text=f"MCP server not found: {server_name}")

        origin, server_headers = entry
        target_url = f"{origin}{remaining_path}"

        fwd_headers: dict[str, str] = {}
        for key, value in request.headers.items():
            if key.lower() not in _HOP_BY_HOP:
                fwd_headers[key] = value
        fwd_headers.pop("host", None)
        fwd_headers.pop("Host", None)
        # Inject per-server headers (overrides any forwarded headers with same key)
        fwd_headers.update(server_headers)

        body = await request.read()

        logger.debug("MCP proxy forwarding", server=server_name, target=target_url, method=request.method)

        try:
            return await _stream_upstream(request, request.method, target_url, fwd_headers, body)
        except (ConnectionResetError, OSError, RuntimeError, ValueError) as exc:
            logger.error("MCP proxy upstream error", server=server_name, error=str(exc))
            return web.Response(status=502, text="MCP proxy: Bad Gateway")

    async def handle_request(request: web.Request) -> web.StreamResponse:
        body = await request.read()

        # Build forwarded headers
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
                # Remove existing authorization header (case-insensitive)
                for k in list(headers):
                    if k.lower() == "authorization":
                        del headers[k]
                if oauth_token:
                    headers["authorization"] = f"Bearer {oauth_token}"

        target_url = f"{upstream_scheme}://{upstream_host}:{upstream_port}{request.path_qs}"

        try:
            return await _stream_upstream(request, request.method, target_url, headers, body)
        except ConnectionResetError:
            # Container disconnected mid-stream (normal during shutdown/timeout)
            logger.debug("Credential proxy: client disconnected", url=str(request.url))
            return web.Response(status=499, text="Client Disconnected")
        except (OSError, RuntimeError, ValueError) as exc:
            logger.error("Credential proxy upstream error", url=str(request.url), error=str(exc))
            return web.Response(status=502, text="Bad Gateway")

    app = web.Application()
    app.router.add_route("*", "/mcp-proxy/{server_name}/{path_info:.*}", handle_mcp_proxy)
    app.router.add_route("*", "/{path_info:.*}", handle_request)

    # Store session for cleanup
    app["client_session"] = session

    async def on_cleanup(app: web.Application) -> None:
        await app["client_session"].close()

    app.on_cleanup.append(on_cleanup)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()

    logger.info("Credential proxy started", port=port, host=host, auth_mode=auth_mode)
    return runner


def detect_auth_mode() -> AuthMode:
    """Detect which auth mode the host is configured for."""
    secrets = read_env_file(["ANTHROPIC_API_KEY"])
    return "api-key" if secrets.get("ANTHROPIC_API_KEY") else "oauth"
