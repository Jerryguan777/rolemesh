"""HTTP reverse proxy with per-tenant credential injection + egress safety.

Routes:

    /proxy/{provider}/{path}  Multi-provider LLM proxy (Anthropic, OpenAI,
                              Google, Bedrock). Credentials resolved
                              per-request from the source IP -> Identity
                              -> tenant_model_credentials lookup.
    /mcp-proxy/{name}/{path}  MCP server proxy with per-user OIDC token
                              forwarding.
    /healthz                  Liveness probe (returns 200 "ok").

Credential resolution is fail-closed by design:

    * Unknown source IP (no Identity in the resolver) -> 401 UNKNOWN_SOURCE.
    * No credential row for (tenant_id, provider)     -> 401 MISSING_CREDENTIAL.

There is no host-env fallback for LLM keys. The boot-time secrets dict
that earlier versions read from ``os.environ`` is gone; only the
non-secret ``*_BASE_URL`` deployment overrides remain on os.environ.

``credential_resolver`` is a required keyword argument to
``start_credential_proxy``: every caller must wire a real
:class:`rolemesh.egress.credentials.CredentialResolver` so there is no
hidden code path where credentials come from somewhere else.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol
from urllib.parse import urlparse

from aiohttp import ClientSession, web

from rolemesh.core.logger import get_logger

from .credentials import CredentialResolverProtocol, MissingCredentialError
from .safety_call import EgressRequest

if TYPE_CHECKING:
    from .safety_call import EgressSafetyCaller
    from .token_identity import Identity, TokenAuthority

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
# Multi-provider routing (static templates + per-provider helpers)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ProviderTemplate:
    """Non-secret routing config for a provider.

    Static at process start: holds the default upstream URL, an optional
    host env override name (deployment-level, not per-tenant), and the
    convention for translating ``cred[key_field]`` into the upstream
    auth header. Anthropic and Bedrock have provider-specific logic
    and are dispatched directly in the handler, not via this table.
    """

    upstream_default: str
    base_url_env: str | None
    key_field: str
    header_name: str
    header_format: str


_PROVIDER_TEMPLATES: dict[str, _ProviderTemplate] = {
    "openai": _ProviderTemplate(
        upstream_default="https://api.openai.com/v1",
        base_url_env="OPENAI_BASE_URL",
        key_field="api_key",
        header_name="authorization",
        header_format="Bearer {key}",
    ),
    "google": _ProviderTemplate(
        upstream_default="https://generativelanguage.googleapis.com",
        base_url_env="GOOGLE_BASE_URL",
        key_field="api_key",
        header_name="x-goog-api-key",
        header_format="{key}",
    ),
}


def _provider_upstream(template: _ProviderTemplate) -> str:
    import os
    if template.base_url_env:
        override = os.environ.get(template.base_url_env, "")
        if override:
            return override
    return template.upstream_default


def _anthropic_upstream() -> str:
    """Deployment-level Anthropic upstream override (non-secret)."""
    import os
    return os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")


def _build_anthropic_header(
    cred: dict[str, Any],
) -> tuple[str, str] | None:
    """Pick the upstream auth header from a tenant's Anthropic credential.

    The wizard writes ``{"api_key": "..."}`` today. A future UI may
    also write ``{"extras": {"oauth_token": "..."}}`` for Claude
    Pro/Max OAuth users; the proxy consumes both shapes already so
    that UI change is purely a frontend chore.

    Returns ``None`` if neither field is populated — the row exists
    but is unusable; the handler treats this identically to a missing
    row (fail-closed).
    """
    api_key = cred.get("api_key") or ""
    if api_key:
        return ("x-api-key", str(api_key))
    extras = cred.get("extras") or {}
    if isinstance(extras, dict):
        oauth = extras.get("oauth_token") or ""
        if oauth:
            return ("authorization", f"Bearer {oauth}")
    return None


def _bedrock_upstream(cred: dict[str, Any]) -> str:
    """Bedrock upstream URL is region-locked; region travels with the cred.

    Falls back to :data:`BEDROCK_DEFAULT_REGION` when the credential
    doesn't carry one (older rows written before the wizard learned
    the region field).
    """
    from rolemesh.core.config import BEDROCK_DEFAULT_REGION
    extras = cred.get("extras") or {}
    region = ""
    if isinstance(extras, dict):
        region = str(extras.get("region") or "")
    region = region or BEDROCK_DEFAULT_REGION
    return f"https://bedrock-runtime.{region}.amazonaws.com"


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
    identity: Identity | None,
    safety_caller: EgressSafetyCaller | None,
    upstream_host: str,
    upstream_port: int,
) -> web.Response | None:
    """Run the egress safety pipeline. Returns 403 response if blocked, None on allow.

    Hook is a no-op when ``safety_caller`` is not wired — preserves
    host-side backward compatibility during the PR-1 → PR-2 rollout
    where the legacy ``start_credential_proxy`` call still binds on
    the orchestrator host.

    ``identity`` is resolved by the caller (token-first, IP fallback)
    so the gate and the credential lookup agree on who the request is.
    """
    if safety_caller is None:
        return None

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
    credential_resolver: CredentialResolverProtocol,
    safety_caller: EgressSafetyCaller | None = None,
    token_authority: TokenAuthority | None = None,
) -> web.AppRunner:
    """Start the reverse-proxy HTTP server on ``(host, port)``.

    ``credential_resolver`` is required: every caller must supply a
    real :class:`CredentialResolver` so credential injection has a
    single, auditable source. There is no fallback to host env LLM
    keys — the boot-time secrets dict that earlier versions read is
    gone.

    Identity comes from a signed token in the request path: the leading
    segment after ``/proxy/`` (or ``/mcp-proxy/``) is verified by
    ``token_authority``; on success the identity is read from the token
    and the provider/server is the *next* segment. An absent or invalid
    token, or a ``None`` ``token_authority`` (host-side unit-test
    configuration), yields 401 UNKNOWN_SOURCE — fail-closed.
    """
    session = ClientSession()

    def _resolve(
        request: web.Request, first_seg: str, rest: str
    ) -> tuple[Identity | None, str, str]:
        """Verify the leading path segment as an identity token.

        Returns ``(identity, provider_or_server, upstream_path)``: the
        token segment is stripped and the provider/server is the head of
        *rest*. On a missing/invalid token (or no authority wired) the
        identity is ``None`` and the caller fails closed; the route
        split is still returned for logging symmetry.
        """
        identity = (
            token_authority.verify(first_seg) if token_authority is not None else None
        )
        head, _, tail = rest.partition("/")
        return identity, head, tail

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
        # Route captured the FIRST segment after /proxy/ as
        # ``provider_name``, but that segment is the identity TOKEN; the
        # real provider is the head of ``path_info``. ``_resolve``
        # verifies the token and returns the effective provider + the
        # remaining upstream path.
        first_seg = request.match_info["provider_name"]
        rest = request.match_info.get("path_info", "")
        identity, provider_name, upstream_path = _resolve(request, first_seg, rest)

        # Identity must resolve. UNKNOWN_SOURCE before credential
        # lookup so a leaked / off-network client gets a clear 401
        # rather than a misleading MISSING_CREDENTIAL.
        if identity is None:
            return web.Response(status=401, text="UNKNOWN_SOURCE")

        remaining_path = "/" + upstream_path
        if request.query_string:
            remaining_path += "?" + request.query_string

        # Per-tenant credential lookup — fail-closed if absent.
        # 401 vs 502 distinction matters: MISSING is "operator should
        # configure this tenant's credential"; RuntimeError is
        # "orchestrator-side fault" (RPC timeout, vault decrypt error,
        # etc.) and is not the requester's problem to fix.
        try:
            cred = await credential_resolver.resolve(
                identity.tenant_id, provider_name,
            )
        except MissingCredentialError:
            return web.Response(status=401, text="MISSING_CREDENTIAL")
        except RuntimeError as exc:
            logger.error(
                "credential resolver fault",
                tenant_id=identity.tenant_id,
                provider=provider_name,
                error=str(exc),
            )
            return web.Response(status=502, text="CREDENTIAL_LOOKUP_FAILED")

        # Dispatch by provider — Anthropic and Bedrock have provider-
        # specific routing; everything else flows through the static
        # _PROVIDER_TEMPLATES table.
        upstream: str
        header_name: str
        header_value: str
        if provider_name == "anthropic":
            upstream = _anthropic_upstream()
            anth = _build_anthropic_header(cred)
            if anth is None:
                return web.Response(status=401, text="MISSING_CREDENTIAL")
            header_name, header_value = anth
        elif provider_name == "bedrock":
            upstream = _bedrock_upstream(cred)
            api_key = str(cred.get("api_key") or "")
            if not api_key:
                return web.Response(status=401, text="MISSING_CREDENTIAL")
            header_name = "authorization"
            header_value = f"Bearer {api_key}"
        else:
            template = _PROVIDER_TEMPLATES.get(provider_name)
            if template is None:
                return web.Response(
                    status=404,
                    text=f"LLM provider not configured: {provider_name}",
                )
            upstream = _provider_upstream(template)
            key_value = str(cred.get(template.key_field) or "")
            if not key_value:
                return web.Response(status=401, text="MISSING_CREDENTIAL")
            header_name = template.header_name
            header_value = template.header_format.format(key=key_value)

        up = urlparse(upstream)
        up_host = up.hostname or ""
        up_port = up.port or (443 if up.scheme == "https" else 80)

        blocked = await _safety_gate(
            request,
            identity=identity,
            safety_caller=safety_caller,
            upstream_host=up_host,
            upstream_port=up_port,
        )
        if blocked is not None:
            return blocked

        target_url = f"{upstream}{remaining_path}"

        fwd_headers = _forward_headers(request)
        body = await request.read()
        fwd_headers["content-length"] = str(len(body))

        if up_port and up_port not in (80, 443):
            fwd_headers["host"] = f"{up_host}:{up_port}"
        else:
            fwd_headers["host"] = up_host

        for k in list(fwd_headers):
            if k.lower() == header_name:
                del fwd_headers[k]
        fwd_headers[header_name] = header_value

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
        # Same token split as the provider route: the first segment
        # after /mcp-proxy/ is the identity token; the server name is
        # the head of the remaining path.
        first_seg = request.match_info["server_name"]
        rest = request.match_info.get("path_info", "")
        identity, server_name, upstream_path = _resolve(request, first_seg, rest)

        remaining_path = "/" + upstream_path
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
            identity=identity,
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

    async def handle_healthz(_request: web.Request) -> web.Response:
        return web.Response(status=200, text="ok")

    app = web.Application()
    app.router.add_get("/healthz", handle_healthz)
    app.router.add_route("*", "/proxy/{provider_name}/{path_info:.*}", handle_provider_proxy)
    app.router.add_route("*", "/mcp-proxy/{server_name}/{path_info:.*}", handle_mcp_proxy)

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
        providers=[*_PROVIDER_TEMPLATES, "anthropic", "bedrock"],
        token_wired=token_authority is not None,
        safety_gated=safety_caller is not None,
    )
    return runner


def detect_auth_mode() -> AuthMode:
    """Detect which auth mode the host is configured for.

    Used by ``container/runner.py`` at agent spawn time to pick the
    Pi SDK's auth-mode env var inside the container. This is NOT a
    request-time credential read — only the existence of the env var
    is consulted; the value itself is never extracted, forwarded, or
    used to authenticate an upstream call. Per-tenant auth-mode
    selection at spawn time is the natural next step; deferred until
    the wizard learns OAuth (see docs/config-drift-fix-plan §3 D1).
    """
    import os as _os

    # inv-cred-ok: existence check for Pi spawn auth-mode; value not read.
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
