"""HTTP reverse proxy with credential injection + optional egress safety hook.

Migrated from ``rolemesh.security.credential_proxy`` as part of EC-2.
The old module becomes a thin re-export (public API unchanged) so that
every ``from rolemesh.security.credential_proxy import …`` call site
continues to resolve without code churn.

Business-logic surface is identical to the pre-EC-2 credential proxy:

    /proxy/{provider}/{path}  Multi-provider LLM proxy (Anthropic, OpenAI, Google, …)
    /mcp-proxy/{name}/{path}  MCP server proxy with per-user token forwarding
    /healthz                  Liveness probe (returns 200 "ok")
    /{path}                   Legacy Anthropic-only catch-all

What EC-2 added:

    * Optional ``safety_caller`` argument. When provided, every request
      first runs through the gateway's Safety pipeline
      (stage='egress_request', mode='reverse'); a block verdict returns
      403 before credentials are injected and before any upstream
      request is made.
    * Optional ``identity_resolver`` argument. Needed because the
      safety pipeline is keyed on tenant_id + coworker_id, which we
      recover from the source IP (agents on the internal bridge are
      always identified by IP via ``IdentityResolver``).

Both arguments are optional so the host-side legacy path (which still
runs during the PR-1 → PR-2 transition) can call ``start_credential_proxy``
with positional args identical to pre-EC-2 behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol
from urllib.parse import urlparse

from aiohttp import ClientSession, web

from rolemesh.core.logger import get_logger

from .safety_call import EgressRequest

if TYPE_CHECKING:
    from .identity import IdentityResolver
    from .safety_call import EgressSafetyCaller

logger = get_logger()


class TokenVaultProtocol(Protocol):
    """Minimal vault interface the reverse-proxy actually consumes.

    Both ``rolemesh.auth.token_vault.TokenVault`` (DB-backed, used by
    orchestrator + webui processes) and ``rolemesh.egress.remote_token_vault.
    RemoteTokenVault`` (NATS-RPC-backed, used by the gateway container)
    satisfy this. Keeping the protocol here rather than importing
    ``TokenVault`` lets the gateway image stay free of ``rolemesh.db``
    transitively — the gateway never persists or decrypts tokens
    locally.
    """

    async def get_fresh_access_token(self, user_id: str) -> str | None: ...


# Module-level TokenVault for per-user IdP token forwarding to MCP servers
_token_vault: TokenVaultProtocol | None = None

# The only identity header containers send.
_USER_ID_HEADER = "X-RoleMesh-User-Id"


def set_token_vault(vault: TokenVaultProtocol) -> None:
    """Set the TokenVault instance for per-user MCP token forwarding.

    Accepts any object satisfying ``TokenVaultProtocol`` — the
    DB-backed ``TokenVault`` in orchestrator/webui or the
    NATS-RPC ``RemoteTokenVault`` in the egress gateway.
    """
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
    upstream: str
    secret_key: str
    header_name: str
    header_format: str


_provider_registry: dict[str, _ProviderConfig] = {}


def _build_provider_registry(
    secrets: dict[str, str], anthropic_auth_mode: AuthMode
) -> dict[str, _ProviderConfig]:
    registry: dict[str, _ProviderConfig] = {}

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

    openai_key = secrets.get("PI_OPENAI_API_KEY", "")
    if openai_key:
        registry["openai"] = _ProviderConfig(
            upstream=secrets.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            secret_key=openai_key,
            header_name="authorization",
            header_format="Bearer {key}",
        )

    google_key = secrets.get("PI_GOOGLE_API_KEY", "")
    if google_key:
        registry["google"] = _ProviderConfig(
            upstream=secrets.get(
                "GOOGLE_BASE_URL", "https://generativelanguage.googleapis.com"
            ),
            secret_key=google_key,
            header_name="x-goog-api-key",
            header_format="{key}",
        )

    # AWS Bedrock — long-term API key (Bearer ABSK...). The token is
    # held on the host; agent containers see only a placeholder env
    # var and route their boto3 client at this proxy via
    # ``BEDROCK_BASE_URL``. Whatever Authorization header boto3
    # synthesises (SigV4 signature in older releases, Bearer in
    # boto3 1.42+) gets overwritten by ``handle_provider_proxy`` —
    # the proxy is the authoritative authn injector.
    #
    # Region is locked at registry-build time. Multi-region deploys
    # would need to encode region in the proxy path
    # (``/proxy/bedrock/{region}/...``); not in scope for the
    # initial Sonnet/Opus 4.6 use case.
    bedrock_token = secrets.get("AWS_BEARER_TOKEN_BEDROCK", "")
    if bedrock_token:
        bedrock_region = secrets.get("AWS_REGION", "") or "us-east-1"
        registry["bedrock"] = _ProviderConfig(
            upstream=f"https://bedrock-runtime.{bedrock_region}.amazonaws.com",
            secret_key=bedrock_token,
            header_name="authorization",
            header_format="Bearer {key}",
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


def unregister_mcp_server(name: str) -> bool:
    """Remove an MCP server from the registry. Returns True if it was present.

    Used by the gateway-side hot-reload subscriber when the orchestrator
    publishes ``egress.mcp.changed action=deleted``. Idempotent: a
    redundant delete (e.g. the same broadcast arriving twice) returns
    False rather than raising.
    """
    removed = _mcp_registry.pop(name, None)
    if removed is None:
        return False
    logger.info("MCP server unregistered", name=name)
    return True


def get_mcp_registry() -> dict[str, tuple[str, dict[str, str], str]]:
    return dict(_mcp_registry)


# ---------------------------------------------------------------------------
# Safety hook
# ---------------------------------------------------------------------------


async def _safety_gate(
    request: web.Request,
    *,
    identity_resolver: IdentityResolver | None,
    safety_caller: EgressSafetyCaller | None,
    upstream_host: str,
    upstream_port: int,
) -> web.Response | None:
    """Run the egress safety pipeline. Returns 403 response if blocked, None on allow.

    Hook is a no-op when ``safety_caller`` is not wired — preserves
    host-side backward compatibility during the PR-1 → PR-2 rollout
    where the legacy ``start_credential_proxy`` call still binds on
    the orchestrator host.
    """
    if safety_caller is None or identity_resolver is None:
        return None

    peer = request.transport.get_extra_info("peername") if request.transport else None  # type: ignore[union-attr]
    source_ip = peer[0] if peer else ""
    identity = identity_resolver.resolve(source_ip)
    decision = await safety_caller.decide(
        identity=identity,
        request=EgressRequest(
            host=upstream_host,
            port=upstream_port,
            mode="reverse",
            method=request.method,
        ),
    )
    if decision.action == "block":
        return web.Response(
            status=403,
            text=f"Blocked by egress policy: {decision.reason}",
            headers={"X-Egress-Reason": decision.reason[:200]},
        )
    return None


# ---------------------------------------------------------------------------
# Main proxy server
# ---------------------------------------------------------------------------


async def start_credential_proxy(
    port: int,
    host: str = "127.0.0.1",
    *,
    identity_resolver: IdentityResolver | None = None,
    safety_caller: EgressSafetyCaller | None = None,
) -> web.AppRunner:
    """Start the reverse-proxy HTTP server on (host, port).

    The trailing keyword-only arguments enable the EC-2 gateway
    deployment (both set). The host-side legacy path leaves them at
    their defaults and gets the pre-EC-2 behaviour.
    """
    # .env is loaded into os.environ by rolemesh.bootstrap at
    # process entry; host-side callers (orchestrator) and the gateway
    # container (env vars forwarded explicitly in launcher._gateway_env)
    # both reach this via os.environ.
    import os as _os

    secrets: dict[str, str] = {
        k: v
        for k, v in (
            (k, _os.environ.get(k, ""))
            for k in (
                "ANTHROPIC_API_KEY",
                "CLAUDE_CODE_OAUTH_TOKEN",
                "ANTHROPIC_AUTH_TOKEN",
                "ANTHROPIC_BASE_URL",
                "PI_OPENAI_API_KEY",
                "OPENAI_BASE_URL",
                "PI_GOOGLE_API_KEY",
                "GOOGLE_BASE_URL",
                # Bedrock — long-term API key (Bearer ABSK...). Held
                # only on the host; agents see a placeholder
                # (see ``rolemesh.agent.executor._pi_extra_env``).
                # Region picks the regional endpoint upstream URL.
                "AWS_BEARER_TOKEN_BEDROCK",
                "AWS_REGION",
            )
        )
        if v
    }

    auth_mode: AuthMode = "api-key" if secrets.get("ANTHROPIC_API_KEY") else "oauth"
    oauth_token = secrets.get("CLAUDE_CODE_OAUTH_TOKEN") or secrets.get("ANTHROPIC_AUTH_TOKEN", "")

    upstream_url = secrets.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    parsed = urlparse(upstream_url)
    is_https = parsed.scheme == "https"
    upstream_host = parsed.hostname or "api.anthropic.com"
    upstream_port = parsed.port or (443 if is_https else 80)
    upstream_scheme = parsed.scheme or "https"

    global _provider_registry
    _provider_registry = _build_provider_registry(secrets, auth_mode)
    for name in _provider_registry:
        logger.info("LLM provider proxy registered", provider=name)

    session = ClientSession()

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

    def _forward_headers(request: web.Request) -> dict[str, str]:
        headers: dict[str, str] = {}
        for key, value in request.headers.items():
            if key.lower() not in _HOP_BY_HOP:
                headers[key] = value
        headers.pop("host", None)
        headers.pop("Host", None)
        return headers

    async def handle_provider_proxy(request: web.Request) -> web.StreamResponse:
        provider_name = request.match_info["provider_name"]
        remaining_path = "/" + request.match_info.get("path_info", "")
        if request.query_string:
            remaining_path += "?" + request.query_string

        config = _provider_registry.get(provider_name)
        if not config:
            return web.Response(status=404, text=f"LLM provider not configured: {provider_name}")

        up = urlparse(config.upstream)
        up_host = up.hostname or ""
        up_port = up.port or (443 if up.scheme == "https" else 80)

        blocked = await _safety_gate(
            request,
            identity_resolver=identity_resolver,
            safety_caller=safety_caller,
            upstream_host=up_host,
            upstream_port=up_port,
        )
        if blocked is not None:
            return blocked

        target_url = f"{config.upstream}{remaining_path}"

        fwd_headers = _forward_headers(request)
        body = await request.read()
        fwd_headers["content-length"] = str(len(body))

        if up_port and up_port not in (80, 443):
            fwd_headers["host"] = f"{up_host}:{up_port}"
        else:
            fwd_headers["host"] = up_host

        for k in list(fwd_headers):
            if k.lower() == config.header_name:
                del fwd_headers[k]
        fwd_headers[config.header_name] = config.header_format.format(key=config.secret_key)

        fwd_headers.pop(_USER_ID_HEADER, None)

        try:
            return await _stream_upstream(request, request.method, target_url, fwd_headers, body)
        except ConnectionResetError:
            logger.debug("Provider proxy: client disconnected", provider=provider_name)
            return web.Response(status=499, text="Client Disconnected")
        except (OSError, RuntimeError, ValueError) as exc:
            logger.error("Provider proxy upstream error", provider=provider_name, error=str(exc))
            return web.Response(status=502, text="Bad Gateway")

    async def handle_mcp_proxy(request: web.Request) -> web.StreamResponse:
        server_name = request.match_info["server_name"]
        remaining_path = "/" + request.match_info.get("path_info", "")
        if request.query_string:
            remaining_path += "?" + request.query_string

        entry = _mcp_registry.get(server_name)
        if not entry:
            return web.Response(status=404, text=f"MCP server not found: {server_name}")

        origin, server_headers, mcp_auth_mode = entry

        up = urlparse(origin)
        up_host = up.hostname or ""
        up_port = up.port or (443 if up.scheme == "https" else 80)

        blocked = await _safety_gate(
            request,
            identity_resolver=identity_resolver,
            safety_caller=safety_caller,
            upstream_host=up_host,
            upstream_port=up_port,
        )
        if blocked is not None:
            return blocked

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

    async def handle_legacy_anthropic(request: web.Request) -> web.StreamResponse:
        blocked = await _safety_gate(
            request,
            identity_resolver=identity_resolver,
            safety_caller=safety_caller,
            upstream_host=upstream_host,
            upstream_port=upstream_port,
        )
        if blocked is not None:
            return blocked

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

    async def handle_healthz(_request: web.Request) -> web.Response:
        return web.Response(status=200, text="ok")

    app = web.Application()
    app.router.add_get("/healthz", handle_healthz)
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

    logger.info(
        "Credential proxy started",
        port=port,
        host=host,
        auth_mode=auth_mode,
        providers=list(_provider_registry.keys()),
        safety_gated=safety_caller is not None,
    )
    return runner


def detect_auth_mode() -> AuthMode:
    """Detect which auth mode the host is configured for."""
    import os as _os

    return "api-key" if _os.environ.get("ANTHROPIC_API_KEY") else "oauth"


__all__ = [
    "AuthMode",
    "detect_auth_mode",
    "get_mcp_registry",
    "register_mcp_server",
    "set_token_vault",
    "start_credential_proxy",
    "unregister_mcp_server",
]


# Silence F401 on the TYPE_CHECKING re-export aliases below — the
# _token_vault hint uses the name at module scope.
_ = Any
