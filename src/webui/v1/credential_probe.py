"""Live credential validation probes for ``/api/v1/credentials``.

The egress reverse proxy injects a tenant's real API key into outbound
LLM calls so a misconfigured credential only surfaces *at agent runtime*
as an opaque 401. This module lets the control plane verify a credential
the moment the operator enters it, by issuing one cheap, read-only,
zero-cost request to the provider.

Why this lives on the control plane and not behind the egress proxy
(see the validation design): the proxy exists to keep secrets OUT of
agent containers — but the webui is trusted code that already holds the
plaintext key. The probe therefore dials the provider directly. To stay
faithful to the agent path it resolves the upstream URL and auth header
from the SAME helpers the reverse proxy uses
(:mod:`rolemesh.egress.reverse_proxy`), so a ``verified`` pass means the
proxied agent call resolves to a working credential too. The four
provider upstreams are platform-allowlisted egress targets, so there is
no extra reachability surprise between here and the gateway.

Bedrock has no cheap unauthenticated read verb on ``bedrock-runtime``;
its probe is a region-shape + endpoint-reachability check (``reachable``)
rather than a live auth check (``verified``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from rolemesh.core.logger import get_logger

logger = get_logger()

# Keep the probe snappy: an interactive "Test" button must not hang on a
# black-holed endpoint. Connect + read are bounded together.
_PROBE_TIMEOUT_S = 8.0

# Anthropic's ``GET /v1/models`` requires a version header like every
# other Anthropic call; without it the API answers 400 regardless of key.
_ANTHROPIC_VERSION = "2023-06-01"


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of one credential probe (maps onto ``CredentialValidationResult``)."""

    ok: bool
    level: str  # "verified" | "reachable" | "unsupported"
    detail: str


def _classify_http_status(status: int) -> ProbeResult:
    """Turn a provider HTTP status into a verified probe verdict.

    A key is "good" when the provider *accepts* it: 2xx, or 429 (the key
    authenticated but we are rate-limited — still proof the key is live).
    401/403 are the unambiguous "bad key / not entitled" signals. Any
    other status means the endpoint answered but not in a way that proves
    the key one way or the other, so we report it as a non-fatal failure
    carrying the status for the operator.
    """
    if status < 300 or status == 429:
        return ProbeResult(True, "verified", "Credential accepted by the provider.")
    if status == 401:
        return ProbeResult(
            False, "verified", "Provider rejected the API key (401 Unauthorized).",
        )
    if status == 403:
        return ProbeResult(
            False,
            "verified",
            "API key accepted but lacks permission for this provider (403 Forbidden).",
        )
    return ProbeResult(
        False, "verified", f"Provider returned an unexpected status ({status}).",
    )


async def _get(url: str, headers: dict[str, str]) -> ProbeResult:
    """Issue the read-only probe GET and classify the result.

    Network-level failures (DNS, connect, TLS, timeout) become a
    ``reachable=false`` verdict — we could not even talk to the upstream,
    which the operator reads as "endpoint/region wrong or blocked" rather
    than "key wrong".
    """
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_S) as client:
            resp = await client.get(url, headers=headers)
    except httpx.TimeoutException:
        return ProbeResult(
            False, "reachable", "Timed out contacting the provider endpoint.",
        )
    except httpx.HTTPError as exc:
        # str(exc) is httpx's own message (host/connect/TLS), never the key.
        return ProbeResult(
            False, "reachable", f"Could not reach the provider endpoint: {exc}.",
        )
    return _classify_http_status(resp.status_code)


async def _probe_anthropic(cred: dict[str, Any]) -> ProbeResult:
    # Reuse the proxy's upstream + auth-header resolution so we hit exactly
    # what an agent would. ``_build_anthropic_header`` handles both api_key
    # and the oauth_token extras shape.
    from rolemesh.egress.reverse_proxy import (
        _anthropic_upstream,
        _build_anthropic_header,
    )

    header = _build_anthropic_header(cred)
    if header is None:
        return ProbeResult(
            False, "verified", "No API key or OAuth token present on the credential.",
        )
    name, value = header
    url = f"{_anthropic_upstream().rstrip('/')}/v1/models"
    return await _get(url, {name: value, "anthropic-version": _ANTHROPIC_VERSION})


async def _probe_templated(provider: str, cred: dict[str, Any]) -> ProbeResult:
    """OpenAI / Google: header + upstream come from the proxy's template table."""
    from rolemesh.egress.reverse_proxy import _PROVIDER_TEMPLATES, _provider_upstream

    template = _PROVIDER_TEMPLATES[provider]
    key = str(cred.get(template.key_field) or "")
    if not key:
        return ProbeResult(False, "verified", "No API key present on the credential.")
    header = {template.header_name: template.header_format.format(key=key)}
    base = _provider_upstream(template).rstrip("/")
    # OpenAI's base already ends in /v1, so /models lands on /v1/models.
    # Google's base is the bare host, so the version segment is explicit.
    path = "/models" if provider == "openai" else "/v1beta/models"
    return await _get(f"{base}{path}", header)


async def _probe_bedrock(cred: dict[str, Any]) -> ProbeResult:
    """Bedrock: validate region shape + endpoint reachability only.

    ``bedrock-runtime`` exposes no cheap unauthenticated read, so we
    cannot turn a live call into a key verdict without invoking a model
    (which costs tokens). We confirm the region-templated upstream is
    well-formed and reachable, and report ``reachable`` so the SPA can
    tell the operator the key itself was not exercised.
    """
    from rolemesh.egress.reverse_proxy import _bedrock_upstream

    if not str(cred.get("api_key") or ""):
        return ProbeResult(False, "verified", "No API key present on the credential.")
    upstream = _bedrock_upstream(cred)
    host = urlparse(upstream).hostname or ""
    if not host:
        return ProbeResult(
            False, "reachable", "Could not derive a Bedrock endpoint from the region.",
        )
    # A bare GET to the runtime host returns an AWS error (400/403) — any
    # HTTP answer proves the region resolves and is reachable. Only a
    # transport failure (bad region → NXDOMAIN, network block) is fatal.
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_S) as client:
            await client.get(upstream)
    except httpx.HTTPError as exc:
        return ProbeResult(
            False,
            "reachable",
            f"Bedrock endpoint unreachable (check the region): {exc}.",
        )
    return ProbeResult(
        True,
        "reachable",
        f"Bedrock endpoint for region '{host}' is reachable. The API key "
        "itself is not exercised by validation; a coworker run confirms it.",
    )


async def probe_credential(provider: str, cred: dict[str, Any]) -> ProbeResult:
    """Validate a credential by exercising the provider's read path.

    ``cred`` is the resolver-shaped dict ``{"api_key": ..., "extras":
    {...}}``. Returns a :class:`ProbeResult`; never raises for a bad key
    or an unreachable endpoint (those are encoded in the result), only
    for genuinely unexpected programming errors.
    """
    if provider == "anthropic":
        return await _probe_anthropic(cred)
    if provider == "bedrock":
        return await _probe_bedrock(cred)
    if provider in ("openai", "google"):
        return await _probe_templated(provider, cred)
    return ProbeResult(
        False, "unsupported", f"No validation probe is available for '{provider}'.",
    )
